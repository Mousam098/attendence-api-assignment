"""Integration tests. They need a MongoDB 6+ reachable through MONGO_URI and are skipped otherwise."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from app import main

IST = main.IST


def ist_ms(year, month, day, hour, minute, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=IST).timestamp() * 1000)


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def api():
    try:
        main.client.admin.command("ping")
    except PyMongoError:
        pytest.skip("MongoDB is not reachable")
    main.client.drop_database(main.MONGO_DB)
    main.ensure_indexes()
    yield TestClient(main.app)
    main.client.drop_database(main.MONGO_DB)


def add_employee(code, department, joined_on, shift_start="09:30", shift_end="18:30"):
    main.employees.insert_one(
        {
            "emp_code": code,
            "name": f"Employee {code}",
            "email": f"{code.lower()}@example.com",
            "department": department,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "joined_on": joined_on,
            "created_at": main.now_millis(),
        }
    )


def add_log(code, day, status="PRESENT", **fields):
    doc = {
        "emp_code": code,
        "date": day,
        "status": status,
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    doc.update(fields)
    main.logs.insert_one(doc)


@pytest.fixture(scope="module")
def dataset(api):
    # QA: one mid-month joiner, one employee without logs, one who has not joined yet.
    add_employee("EMP1001", "QA", "2026-06-01")
    add_employee("EMP1002", "QA", "2026-07-15")
    add_employee("EMP1003", "QA", "2026-06-01")
    add_employee("EMP1004", "QA", "2026-08-01")
    add_log("EMP1001", "2026-07-06", work_hours=9.0)
    add_log("EMP1001", "2026-07-07", work_hours=4.0, half_day=True, late_minutes=15)
    add_log("EMP1001", "2026-07-08", status="LEAVE")
    add_log("EMP1001", "2026-07-11", work_hours=8.0, late_minutes=20, overtime_minutes=45)  # Saturday
    add_log("EMP1002", "2026-07-16", work_hours=6.0)
    # Ops: ties on 30, 30, 20, 10 minutes.
    for code, minutes in (("EMP2001", 30), ("EMP2002", 30), ("EMP2003", 20), ("EMP2004", 10)):
        add_employee(code, "Ops", "2026-01-05")
        add_log(code, "2026-07-06", late_minutes=minutes)
    add_log("GHOST01", "2026-07-06", late_minutes=500)  # no such employee: must be ignored


def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_employee_monthly(api, dataset):
    body = api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"}).json()
    assert body == {
        "emp_code": "EMP1001",
        "month": "2026-07",
        "working_days": 23,
        "present_days": 1.5,
        "leave_days": 1,
        "late_count": 2,
        "total_late_minutes": 35,
        "total_overtime_minutes": 45,
        "attendance_pct": 6.52,
    }
    joiner = api.get("/analytics/employees/EMP1002/monthly", params={"month": "2026-07"}).json()
    assert joiner["working_days"] == 13
    assert api.get("/analytics/employees/NOPE1234/monthly", params={"month": "2026-07"}).status_code == 404


def test_department_summary(api, dataset):
    items = api.get(
        "/analytics/departments/summary", params={"month": "2026-07", "department": "QA"}
    ).json()["items"]
    assert items == [
        {
            "department": "QA",
            "headcount": 3,
            "present_days": 2.5,
            "avg_work_hours": 6.75,
            "late_count": 2,
            "total_late_minutes": 35,
            "leave_count": 1,
            "on_duty_count": 0,
        }
    ]


def test_leaderboard_ties_share_rank_and_limit_applies_after_ranking(api, dataset):
    def rows(limit):
        body = api.get(
            "/analytics/leaderboard/late",
            params={"month": "2026-07", "department": "Ops", "limit": limit},
        ).json()
        return [(r["rank"], r["emp_code"]) for r in body["items"]]

    assert rows(1) == [(1, "EMP2001"), (1, "EMP2002")]
    assert rows(2) == [(1, "EMP2001"), (1, "EMP2002")]
    assert rows(3) == [(1, "EMP2001"), (1, "EMP2002"), (3, "EMP2003")]
    everyone = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 50}).json()
    assert "GHOST01" not in [r["emp_code"] for r in everyone["items"]]


def test_department_trend_fills_gaps_and_averages(api, dataset):
    body = api.get(
        "/analytics/departments/QA/trend", params={"from": "2026-07-04", "to": "2026-07-10"}
    ).json()
    rows = body["items"]
    assert [r["date"] for r in rows] == [f"2026-07-{d:02d}" for d in range(4, 11)]
    assert [r["is_working_day"] for r in rows] == [False, False, True, True, True, True, True]
    assert {r["headcount"] for r in rows} == {2}
    assert [r["present_count"] for r in rows] == [0, 0, 1, 0.5, 0, 0, 0]
    assert [r["late_count"] for r in rows] == [0, 0, 0, 1, 0, 0, 0]
    assert [r["attendance_rate"] for r in rows] == [None, None, 0.5, 0.25, 0.0, 0.0, 0.0]
    assert [r["moving_avg_7d"] for r in rows] == [None, None, 0.5, 0.375, 0.25, 0.1875, 0.15]


def test_trend_validation(api, dataset):
    base = "/analytics/departments/QA/trend"
    assert api.get(base, params={"from": "2026-07-10", "to": "2026-07-01"}).status_code == 422
    assert api.get(base, params={"from": "2026-01-01", "to": "2026-12-31"}).status_code == 422
    assert api.get("/analytics/departments/Nowhere/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404


def test_punch_in_race_yields_one_created(api, dataset):
    add_employee("EMP3001", "Race", "2026-01-05")
    payload = {"emp_code": "EMP3001", "punched_at": ist_ms(2026, 7, 20, 9, 31)}

    def hit(_):
        return TestClient(main.app).post("/attendance/punch-in", json=payload).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = sorted(pool.map(hit, range(8)))
    assert codes == [201] + [409] * 7


def test_punch_out_race_yields_one_success(api, dataset):
    payload_in = {"emp_code": "EMP3001", "punched_at": ist_ms(2026, 7, 21, 9, 0)}
    assert api.post("/attendance/punch-in", json=payload_in).status_code == 201
    payload_out = {"emp_code": "EMP3001", "punched_at": ist_ms(2026, 7, 21, 18, 0)}

    def hit(_):
        return TestClient(main.app).post("/attendance/punch-out", json=payload_out).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = sorted(pool.map(hit, range(8)))
    assert codes == [200] + [409] * 7


def test_overnight_punch_across_midnight(api, dataset):
    add_employee("EMP3002", "Night", "2026-01-05", shift_start="22:00", shift_end="06:00")
    record = api.post(
        "/attendance/punch-in", json={"emp_code": "EMP3002", "punched_at": ist_ms(2026, 7, 6, 21, 55)}
    ).json()
    assert record["date"] == "2026-07-06"
    closed = api.post(
        "/attendance/punch-out", json={"emp_code": "EMP3002", "punched_at": ist_ms(2026, 7, 7, 6, 40)}
    ).json()
    assert (closed["work_hours"], closed["overtime_minutes"], closed["late_minutes"]) == (8.75, 40, 0)


def test_concurrent_corrections_keep_every_history_entry(api, dataset):
    add_employee("EMP3003", "Fix", "2026-01-05")
    first = ist_ms(2026, 7, 22, 10, 30)
    api.post("/attendance/punch-in", json={"emp_code": "EMP3003", "punched_at": first})

    def correct(minute):
        body = {
            "punch_in": ist_ms(2026, 7, 22, 9, minute),
            "reason": f"correction {minute}",
            "regularized_by": "hr.admin",
        }
        return TestClient(main.app).patch("/attendance/EMP3003/2026-07-22", json=body).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(correct, [20, 21, 22, 23]))
    assert set(codes) <= {200, 409}
    stored = main.logs.find_one({"emp_code": "EMP3003", "date": "2026-07-22"})
    assert len(stored["history"]) == codes.count(200)


@pytest.mark.parametrize(
    "endpoint, params",
    [
        ("attendance_list", {}),
        ("attendance_list", {"emp_code": "EMP1001", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("attendance_list", {"status": "LEAVE", "date_from": "2026-07-01"}),
        ("employee_monthly", {"emp_code": "EMP1001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("department_summary", {"month": "2026-07", "department": "QA"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "QA", "from": "2026-07-01", "to": "2026-07-31"}),
    ],
)
def test_explain_uses_indexes(api, dataset, endpoint, params):
    response = api.get(f"/admin/explain/{endpoint}", params=params)
    assert response.status_code == 200
    plan = json.dumps(response.json()["explain"])
    assert "IXSCAN" in plan
    assert "COLLSCAN" not in plan


def test_explain_requires_parameters(api):
    assert api.get("/admin/explain/employee_monthly").status_code == 422
    assert api.get("/admin/explain/unknown").status_code == 422
