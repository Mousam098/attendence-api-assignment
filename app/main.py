"""Employee Attendance & Analytics API.

A FastAPI + MongoDB service for recording attendance, correcting records and
answering analytical questions over them. The business rules (R1-R10) are
specified in ``openapi.yaml`` and the stored data layout in ``DATA_MODEL.md``.

Layout of this module:
    1. configuration and database handles
    2. time and rule helpers (R1-R5)
    3. request / response models
    4. serialisation and validation helpers
    5. query and pipeline builders
    6. endpoints
"""

import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from pymongo import IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration and database handles
# --------------------------------------------------------------------------- #

load_dotenv()  # never overrides variables that are already set in the environment

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

logger = logging.getLogger("attendance")

# tz_aware=True makes PyMongo return UTC-aware datetimes instead of naive ones.
client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
    maxPoolSize=100,
)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

_indexes_ready = False


def ensure_indexes() -> None:
    """Create every index the service relies on. Safe to call repeatedly."""
    global _indexes_ready
    employees.create_indexes(
        [
            # Identity and the duplicate guard for POST /employees.
            IndexModel([("emp_code", 1)], name="uniq_emp_code", unique=True),
            # Filtered employee listing, already ordered by emp_code.
            IndexModel([("department", 1), ("emp_code", 1)], name="dept_emp_code"),
            # Headcount (R9) and the department trend.
            IndexModel([("department", 1), ("joined_on", 1)], name="dept_joined_on"),
            IndexModel([("joined_on", 1)], name="joined_on"),
        ]
    )
    logs.create_indexes(
        [
            # Natural key: one record per employee and day. Also the punch-in race guard.
            IndexModel([("emp_code", 1), ("date", 1)], name="uniq_emp_date", unique=True),
            # Default listing order and every month / range scan across employees.
            IndexModel([("date", -1), ("emp_code", 1)], name="date_desc_emp"),
            # Listing filtered by status.
            IndexModel([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp"),
            # Punch-out: latest record of an employee at or before an instant.
            IndexModel([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in"),
        ]
    )
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # The service stays up and /health reports 503 until MongoDB answers.
        logger.exception("could not create indexes at startup")
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="Attendance capture, manual corrections and analytics backed by MongoDB.",
    lifespan=lifespan,
)


@app.exception_handler(PyMongoError)
async def database_error_handler(_: Request, exc: PyMongoError) -> JSONResponse:
    logger.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time and rule helpers
# --------------------------------------------------------------------------- #

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

LATE_GRACE_SECONDS = 10 * 60
OVERTIME_THRESHOLD_MINUTES = 30
HALF_DAY_BELOW = Decimal("4.50")
MAX_SHIFT_SPAN = timedelta(hours=24)
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
MAX_TREND_DAYS = 92


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(value: datetime) -> int:
    """UTC datetime (aware or naive-UTC) to epoch milliseconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    current = datetime.now(UTC)
    return current.replace(microsecond=current.microsecond // 1000 * 1000)


def _hhmm(value: str) -> time_cls:
    hour, minute = value.split(":")
    return time_cls(int(hour), int(minute))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _hhmm(shift_end) <= _hhmm(shift_start)


def ist_instant(day: date_cls, hhmm: str) -> datetime:
    return datetime.combine(day, _hhmm(hhmm), tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: the IST calendar date a punch-in belongs to."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < _hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def late_minutes_for(punch_in: datetime, day: date_cls, shift_start: str) -> int:
    """R2: late only when strictly more than the grace period after shift start."""
    elapsed = int((punch_in - ist_instant(day, shift_start)).total_seconds())
    return elapsed // 60 if elapsed > LATE_GRACE_SECONDS else 0


def shift_end_instant(day: date_cls, shift_start: str, shift_end: str) -> datetime:
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    return ist_instant(end_day, shift_end)


def overtime_minutes_for(punch_out: datetime, end: datetime) -> int:
    """R3: whole minutes past shift end, counted only from the threshold up."""
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_THRESHOLD_MINUTES else 0


def round_half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def work_hours_for(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: hours between the punches, two decimals, half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def derive_fields(
    employee: dict, day: date_cls, punch_in: Optional[datetime], punch_out: Optional[datetime]
) -> dict:
    """Derived values (R2-R5) for a record with a presence status."""
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    derived = {
        "work_hours": None,
        "late_minutes": late_minutes_for(punch_in, day, employee["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = work_hours_for(punch_in, punch_out)
        end = shift_end_instant(day, employee["shift_start"], employee["shift_end"])
        derived["work_hours"] = float(hours)
        derived["overtime_minutes"] = overtime_minutes_for(punch_out, end)
        derived["half_day"] = hours < HALF_DAY_BELOW
    return derived


def weekdays_between(first: date_cls, last: date_cls) -> int:
    """Number of Monday-Friday days from ``first`` to ``last`` inclusive (R7)."""
    count = 0
    day = first
    while day <= last:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# 3. Request and response models
# --------------------------------------------------------------------------- #


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


# Strict: floats, strings and booleans are rejected, as are second-resolution values.
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]

DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: Annotated[str, StringConstraints(pattern=HHMM_PATTERN)] = "09:30"
    shift_end: Annotated[str, StringConstraints(pattern=HHMM_PATTERN)] = "18:30"
    joined_on: Annotated[str, StringConstraints(pattern=DATE_PATTERN)]

    @field_validator("joined_on")
    @classmethod
    def _real_calendar_date(cls, value: str) -> str:
        try:
            date_cls.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("must be a valid calendar date") from exc
        return value

    @model_validator(mode="after")
    def _shift_has_length(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: Optional[int]


class EmployeePage(BaseModel):
    items: list[Employee]
    total: int
    page: int
    page_size: int


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict[str, dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntry]


class AttendancePage(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentRow(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: list[DepartmentRow]


class LeaderboardRow(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: list[LeaderboardRow]


class TrendRow(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(BaseModel):
    department: str
    items: list[TrendRow]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


# --------------------------------------------------------------------------- #
# 4. Serialisation and validation helpers
# --------------------------------------------------------------------------- #


def reject(loc: tuple, message: str, kind: str = "value_error") -> None:
    """Raise a 422 shaped like FastAPI's own validation errors."""
    raise RequestValidationError([{"type": kind, "loc": loc, "msg": message}])


def parse_day(value: str, loc: tuple) -> date_cls:
    if not re.fullmatch(DATE_PATTERN, value):
        reject(loc, "expected a date formatted as YYYY-MM-DD")
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        reject(loc, "not a valid calendar date")
    raise AssertionError("unreachable")


def month_bounds(value: str, loc: tuple = ("query", "month")) -> tuple[date_cls, date_cls]:
    if not re.fullmatch(MONTH_PATTERN, value):
        reject(loc, "expected a month formatted as YYYY-MM")
    year, month = int(value[:4]), int(value[5:])
    try:
        first = date_cls(year, month, 1)
    except ValueError:
        reject(loc, "not a valid month")
    return first, date_cls(year, month, calendar.monthrange(year, month)[1])


def require(value: Any, name: str) -> Any:
    if value is None:
        reject(("query", name), "Field required", kind="missing")
    return value


def optional_ms(value: Optional[datetime]) -> Optional[int]:
    return None if value is None else dt_to_ms(value)


def _api_value(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": optional_ms(doc.get("created_at")),
    }


def history_to_api(entries: Optional[list]) -> list:
    result = []
    for entry in entries or []:
        changes = {
            field: {side: _api_value(val) for side, val in change.items()}
            for field, change in (entry.get("changes") or {}).items()
        }
        result.append(
            {
                "at": dt_to_ms(entry["at"]),
                "by": entry.get("by", ""),
                "reason": entry.get("reason", ""),
                "changes": changes,
            }
        )
    return result


def record_to_api(doc: dict) -> dict:
    """Stored attendance document to its API shape. Missing legacy fields get defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": optional_ms(doc.get("punch_in")),
        "punch_out": optional_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": history_to_api(doc.get("history")),
    }


def get_employee_or_404(emp_code: str) -> dict:
    doc = employees.find_one({"emp_code": emp_code})
    if doc is None:
        raise HTTPException(status_code=404, detail=f"employee {emp_code} not found")
    return doc


def count_matching(collection, query: dict) -> int:
    # An empty filter can be answered from collection metadata instead of a scan.
    if not query:
        return collection.estimated_document_count()
    return collection.count_documents(query)


# --------------------------------------------------------------------------- #
# 5. Query and pipeline builders
#
# Each builder is shared by the real endpoint and by /admin/explain, so the
# explained plan is always the plan the endpoint really runs.
# --------------------------------------------------------------------------- #


def attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[Status],
    page: int,
    page_size: int,
) -> tuple[dict, list, int, int]:
    first = parse_day(date_from, ("query", "date_from")) if date_from else None
    last = parse_day(date_to, ("query", "date_to")) if date_to else None
    if first and last and first > last:
        reject(("query", "date_from"), "date_from must not be after date_to")

    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status.value
    if first or last:
        query["date"] = {}
        if first:
            query["date"]["$gte"] = first.isoformat()
        if last:
            query["date"]["$lte"] = last.isoformat()
    sort = [("date", -1), ("emp_code", 1)]
    return query, sort, (page - 1) * page_size, page_size


def _f(ref: str, name: str) -> str:
    """Field path inside ``ref`` (an embedded document) or at the document root when ref is empty."""
    return f"${ref}.{name}" if ref else f"${name}"


def _is_present(ref: str) -> dict:
    return {"$in": [_f(ref, "status"), list(PRESENCE_STATUSES)]}


def _is_weekday(ref: str) -> dict:
    day = {"$dateFromString": {"dateString": _f(ref, "date")}}
    return {"$lte": [{"$isoDayOfWeek": day}, 5]}


def _present_halves(ref: str, weekdays_only: bool) -> dict:
    """Half-day units of presence: 2 for a full day, 1 for a half day, 0 otherwise."""
    condition: Any = _is_present(ref)
    if weekdays_only:
        condition = {"$and": [condition, _is_weekday(ref)]}
    value = {"$cond": [{"$ifNull": [_f(ref, "half_day"), False]}, 1, 2]}
    return {"$cond": [condition, value, 0]}


def _late_flag(ref: str) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [_f(ref, "late_minutes"), 0]}, 0]}, 1, 0]}


def _logs_lookup(first: str, last: str, fields: list[str]) -> dict:
    """Join one employee's logs for a date range through the (emp_code, date) index."""
    projection = {"_id": 0, **{name: 1 for name in fields}}
    return {
        "$lookup": {
            "from": logs.name,
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": projection},
            ],
            "as": "logs",
        }
    }


def employee_monthly_pipeline(emp_code: str, first: date_cls, last: date_cls) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_halves": {"$sum": _present_halves("", True)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _late_flag("")},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def department_summary_pipeline(first: date_cls, last: date_cls, department: Optional[str]) -> list:
    """Starts from employees so that people without any log still count (R9)."""
    match: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    ref = "logs"
    hours_present = {"$and": [_is_present(ref), {"$isNumber": f"${ref}.work_hours"}]}
    return [
        {"$match": match},
        _logs_lookup(
            first.isoformat(),
            last.isoformat(),
            ["date", "status", "half_day", "late_minutes", "work_hours"],
        ),
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_halves": {"$sum": _present_halves(ref, True)},
                # Hours are summed in hundredths so the later average is exact.
                "hours_cents": {
                    "$sum": {
                        "$cond": [hours_present, {"$round": [{"$multiply": [f"${ref}.work_hours", 100]}, 0]}, 0]
                    }
                },
                "hours_count": {"$sum": {"$cond": [hours_present, 1, 0]}},
                "late_count": {"$sum": _late_flag(ref)},
                "total_late_minutes": {"$sum": f"${ref}.late_minutes"},
                "leave_count": {"$sum": {"$cond": [{"$eq": [f"${ref}.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": [f"${ref}.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_halves": 1,
                "hours_cents": 1,
                "hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(
    first: date_cls, last: date_cls, limit: int, department: Optional[str]
) -> list:
    employee_filter = [{"$match": {"department": department}}] if department is not None else []
    return [
        {
            "$match": {
                "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": employees.name,
                "localField": "_id",
                "foreignField": "emp_code",
                "pipeline": employee_filter + [{"$project": {"_id": 0, "name": 1, "department": 1}}],
                "as": "employee",
            }
        },
        # Logs without a matching employee (or outside the department) drop out here.
        {"$unwind": "$employee"},
        # $rank is standard competition ranking: ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # The limit is applied to the rank, not to the row count.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]


def department_trend_pipeline(department: str, first: date_cls, last: date_cls) -> list:
    """One row per calendar day with gap filling and a 7-row moving average, all in MongoDB.

    The pipeline runs on employees. One branch aggregates the department's logs per
    day; the other (``$unionWith``) generates the full calendar with the day's headcount,
    so days without records still appear. Rates are carried as integer units of 1/10000
    and rounded half-up with integer arithmetic, which keeps the rounding exact.
    """
    day_count = (last - first).days + 1
    start = datetime.combine(first, time_cls.min, tzinfo=UTC)
    first_s, last_s = first.isoformat(), last.isoformat()

    calendar_branch = [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "joined": {"$push": "$joined_on"}}},
        {
            "$project": {
                "_id": 0,
                "days": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$let": {
                                "vars": {
                                    "day": {
                                        "$dateToString": {
                                            "format": "%Y-%m-%d",
                                            "date": {
                                                "$dateAdd": {
                                                    "startDate": start,
                                                    "unit": "day",
                                                    "amount": "$$offset",
                                                }
                                            },
                                        }
                                    }
                                },
                                "in": {
                                    "date": "$$day",
                                    "headcount": {
                                        "$size": {
                                            "$filter": {
                                                "input": "$joined",
                                                "as": "joined_on",
                                                "cond": {"$lte": ["$$joined_on", "$$day"]},
                                            }
                                        }
                                    },
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$days"},
        {
            "$project": {
                "_id": "$days.date",
                "headcount": "$days.headcount",
                "present_halves": {"$literal": 0},
                "late_count": {"$literal": 0},
            }
        },
    ]

    window = {"documents": [-6, 0]}
    return [
        {"$match": {"department": department, "joined_on": {"$lte": last_s}}},
        _logs_lookup(first_s, last_s, ["date", "status", "half_day", "late_minutes"]),
        {"$unwind": "$logs"},
        {
            "$group": {
                "_id": "$logs.date",
                "present_halves": {"$sum": _present_halves("logs", False)},
                "late_count": {"$sum": _late_flag("logs")},
            }
        },
        {"$unionWith": {"coll": employees.name, "pipeline": calendar_branch}},
        {
            "$group": {
                "_id": "$_id",
                "headcount": {"$max": "$headcount"},
                "present_halves": {"$sum": "$present_halves"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$set": {
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, 5]
                }
            }
        },
        {
            # rate * 10000, rounded half-up: floor((10000 * halves + headcount) / (2 * headcount))
            "$set": {
                "rate_units": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$present_halves", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "window_sum": {"$sum": "$rate_units", "window": window},
                    "window_n": {
                        "$sum": {"$cond": [{"$isNumber": "$rate_units"}, 1, 0]},
                        "window": window,
                    },
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$present_halves", 2]},
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [{"$isNumber": "$rate_units"}, {"$divide": ["$rate_units", 10000]}, None]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$window_n", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": ["$window_sum", 2]}, "$window_n"]},
                                            {"$multiply": [2, "$window_n"]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


def validate_trend_range(from_: str, to: str, loc_prefix: str = "query") -> tuple[date_cls, date_cls]:
    first = parse_day(from_, (loc_prefix, "from"))
    last = parse_day(to, (loc_prefix, "to"))
    if last < first:
        reject((loc_prefix, "to"), "'to' must not be before 'from'")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        reject((loc_prefix, "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    return first, last


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #

PageParam = Annotated[int, Query(ge=1, le=1_000_000_000)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]
MonthParam = Annotated[str, Query(pattern=MONTH_PATTERN)]


@app.get("/health", tags=["system"])
def health() -> dict:
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------- #


@app.post("/employees", status_code=201, response_model=Employee, tags=["employees"])
def create_employee(body: EmployeeCreate) -> dict:
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_millis(),
    }
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"employee {body.emp_code} already exists")
    return employee_to_api(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(
    department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20
) -> dict:
    query = {"department": department} if department is not None else {}
    cursor = (
        employees.find(query)
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_to_api(doc) for doc in cursor],
        "total": count_matching(employees, query),
        "page": page,
        "page_size": page_size,
    }


# ---- attendance ------------------------------------------------------------ #


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord, tags=["attendance"])
def punch_in(body: PunchInRequest) -> dict:
    employee = get_employee_or_404(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(punched_at, employee["shift_start"], employee["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status.value,
        "punch_in": punched_at,
        "punch_out": None,
        **derive_fields(employee, day, punched_at, None),
        "history": [],
    }
    try:
        # The unique (emp_code, date) index decides races: one insert wins, the rest conflict.
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail=f"{body.emp_code} already has a record for {day.isoformat()}"
        )
    return record_to_api(doc)


@app.post("/attendance/punch-out", response_model=AttendanceRecord, tags=["attendance"])
def punch_out(body: PunchOutRequest) -> dict:
    employee = get_employee_or_404(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()

    record = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found for this employee")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    opened_at = record["punch_in"]
    if punched_at <= opened_at:
        reject(("body", "punched_at"), "punched_at must be after the punch-in")
    if punched_at - opened_at > MAX_SHIFT_SPAN:
        reject(("body", "punched_at"), "punch-out is more than 24 hours after the punch-in")

    day = date_cls.fromisoformat(record["date"])
    derived = derive_fields(employee, day, opened_at, punched_at)
    # Matching on punch_out being empty makes a concurrent second punch-out fail cleanly.
    updated = logs.find_one_and_update(
        {"_id": record["_id"], "punch_in": opened_at, "punch_out": None},
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed or already punched out")
    return record_to_api(updated)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    date_to: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    status: Optional[Status] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
) -> dict:
    query, sort, skip, limit = attendance_list_query(
        emp_code, date_from, date_to, status, page, page_size
    )
    cursor = logs.find(query).sort(sort).skip(skip).limit(limit)
    return {
        "items": [record_to_api(doc) for doc in cursor],
        "total": count_matching(logs, query),
        "page": page,
        "page_size": page_size,
    }


MAX_REGULARIZE_ATTEMPTS = 4
CORRECTABLE_FIELDS = (
    "status",
    "punch_in",
    "punch_out",
    "work_hours",
    "late_minutes",
    "overtime_minutes",
    "half_day",
)


def build_correction(record: dict, employee: dict, body: RegularizeRequest) -> dict:
    """Final values of every correctable field after applying ``body`` (R1-R5)."""
    status = body.status.value if body.status is not None else record["status"]
    supplied_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    supplied_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None
    day = date_cls.fromisoformat(record["date"])

    if status not in PRESENCE_STATUSES:
        if supplied_in is not None or supplied_out is not None:
            reject(("body", "status"), f"{status} records cannot carry punch times")
        return {
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    opened_at = supplied_in if supplied_in is not None else record.get("punch_in")
    closed_at = supplied_out if supplied_out is not None else record.get("punch_out")
    if opened_at is None:
        reject(("body", "punch_in"), f"{status} requires a punch_in")
    if attendance_date(opened_at, employee["shift_start"], employee["shift_end"]) != day:
        reject(("body", "punch_in"), "punch_in must stay on the record's attendance date")
    if closed_at is not None:
        if closed_at <= opened_at:
            reject(("body", "punch_out"), "punch_out must be after punch_in")
        if closed_at - opened_at > MAX_SHIFT_SPAN:
            reject(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")

    return {
        "status": status,
        "punch_in": opened_at,
        "punch_out": closed_at,
        **derive_fields(employee, day, opened_at, closed_at),
    }


def stored_snapshot(record: dict) -> dict:
    return {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day")),
    }


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest) -> dict:
    parse_day(date, ("path", "date"))
    employee = get_employee_or_404(emp_code)

    for _ in range(MAX_REGULARIZE_ATTEMPTS):
        record = logs.find_one({"emp_code": emp_code, "date": date})
        if record is None:
            raise HTTPException(status_code=404, detail=f"no record for {emp_code} on {date}")

        before = stored_snapshot(record)
        after = build_correction(record, employee, body)
        changes = {
            field: {"from": before[field], "to": after[field]}
            for field in CORRECTABLE_FIELDS
            if before[field] != after[field]
        }
        if not changes:
            reject(("body",), "the request does not change the record")

        entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        history_length = len(record.get("history") or [])
        # Optimistic concurrency: apply only if the record is exactly as it was read, so two
        # corrections can never overwrite each other's history entry.
        applied = logs.update_one(
            {
                "_id": record["_id"],
                "status": before["status"],
                "punch_in": before["punch_in"],
                "punch_out": before["punch_out"],
                "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_length]},
            },
            {"$set": after, "$push": {"history": entry}},
        )
        if applied.matched_count == 1:
            record.update(after)
            record["history"] = list(record.get("history") or []) + [entry]
            return record_to_api(record)

    raise HTTPException(status_code=409, detail="the record is being modified, retry the request")


# ---- analytics ------------------------------------------------------------- #


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthly,
    tags=["analytics"],
)
def employee_monthly(emp_code: str, month: MonthParam) -> dict:
    first, last = month_bounds(month)
    employee = get_employee_or_404(emp_code)

    joined = date_cls.fromisoformat(employee["joined_on"])
    start = max(first, joined)
    working_days = weekdays_between(start, last) if start <= last else 0

    stats = next(logs.aggregate(employee_monthly_pipeline(emp_code, first, last)), {})
    halves = int(stats.get("present_halves", 0))
    percentage = None
    if working_days:
        ratio = Decimal(halves) * 100 / (Decimal(2) * working_days)
        percentage = float(round_half_up(ratio, 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": percentage,
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary, tags=["analytics"])
def department_summary(month: MonthParam, department: Optional[str] = None) -> dict:
    first, last = month_bounds(month)
    rows = []
    for row in employees.aggregate(
        department_summary_pipeline(first, last, department), allowDiskUse=True
    ):
        average = None
        if row["hours_count"]:
            exact = Decimal(int(row["hours_cents"])) / Decimal(row["hours_count"]) / 100
            average = float(round_half_up(exact, 2))
        rows.append(
            {
                "department": row["department"],
                "headcount": row["headcount"],
                "present_days": int(row["present_halves"]) / 2,
                "avg_work_hours": average,
                "late_count": int(row["late_count"]),
                "total_late_minutes": int(row["total_late_minutes"]),
                "leave_count": int(row["leave_count"]),
                "on_duty_count": int(row["on_duty_count"]),
            }
        )
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard, tags=["analytics"])
def late_leaderboard(
    month: MonthParam,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
) -> dict:
    first, last = month_bounds(month)
    items = list(
        logs.aggregate(late_leaderboard_pipeline(first, last, limit, department), allowDiskUse=True)
    )
    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=Trend,
    tags=["analytics"],
)
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=DATE_PATTERN)],
    to: Annotated[str, Query(pattern=DATE_PATTERN)],
) -> dict:
    first, last = validate_trend_range(from_, to)
    if employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(status_code=404, detail=f"unknown department {department}")
    items = list(employees.aggregate(department_trend_pipeline(department, first, last), allowDiskUse=True))
    return {"department": department, "items": items}


# ---- admin ----------------------------------------------------------------- #


def run_explain(command: dict) -> dict:
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}", response_model=ExplainResponse, tags=["admin"])
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    date_to: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from", pattern=DATE_PATTERN)] = None,
    to: Annotated[Optional[str], Query(pattern=DATE_PATTERN)] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
) -> dict:
    """Run MongoDB's explain (executionStats) on the endpoint's main query, built the same way."""
    if endpoint is ExplainTarget.attendance_list:
        query, sort, skip, size = attendance_list_query(
            emp_code, date_from, date_to, status, page, page_size
        )
        command = {
            "find": logs.name,
            "filter": query,
            "sort": dict(sort),
            "skip": skip,
            "limit": size,
        }
        collection = logs.name
    else:
        if endpoint is ExplainTarget.department_trend:
            first, last = validate_trend_range(
                require(from_, "from"), require(to, "to")
            )
            pipeline = department_trend_pipeline(require(department, "department"), first, last)
            collection = employees.name
        else:
            first, last = month_bounds(require(month, "month"))
            if endpoint is ExplainTarget.employee_monthly:
                pipeline = employee_monthly_pipeline(require(emp_code, "emp_code"), first, last)
                collection = logs.name
            elif endpoint is ExplainTarget.department_summary:
                pipeline = department_summary_pipeline(first, last, department)
                collection = employees.name
            else:
                pipeline = late_leaderboard_pipeline(first, last, limit, department)
                collection = logs.name
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
    return {"endpoint": endpoint.value, "collection": collection, "explain": run_explain(command)}
