# Employee Attendance & Analytics API

A backend service for a small HR attendance system. Employees punch in and out, managers correct records with a full
audit trail, and the business can ask questions of the data: who is late, which department is slipping, and what the
trend looks like over time.

Built with **FastAPI** and **MongoDB**. All application code lives in a single module, `app/main.py`.

## Features

- **Attendance capture**: punch-in and punch-out with late, overtime, work-hour and half-day calculation, including
  overnight shifts that cross midnight.
- **Manual corrections**: regularize a day's record; derived fields are recomputed and every change is appended to an
  immutable history.
- **Analytics** computed by MongoDB aggregation pipelines:
  - monthly summary for one employee
  - department summary across both collections
  - late-comers leaderboard with competition ranking
  - daily department trend with gap filling and a 7-day moving average
- **Query plans on demand**: an admin endpoint returns MongoDB `explain` output for each endpoint's main query.
- **Concurrency safe**: duplicate punch-ins, duplicate punch-outs and concurrent corrections are resolved by the
  database, not by application-level checks.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Web framework | FastAPI, Uvicorn |
| Database | MongoDB 6.0+ via PyMongo |
| Validation | Pydantic v2 |
| Tests | pytest, httpx |

## API overview

Full contract: [`openapi.yaml`](openapi.yaml). Interactive documentation is served at `/docs` when the app is running.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Readiness probe; 200 only when MongoDB answers |
| POST | `/employees` | Create an employee (409 on duplicate `emp_code`) |
| GET | `/employees` | List employees, filter by department, paginated |
| POST | `/attendance/punch-in` | Open the day's record |
| POST | `/attendance/punch-out` | Close the record and compute work hours, overtime, half day |
| GET | `/attendance` | List records with filters and pagination |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize a record and append to its history |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| GET | `/analytics/departments/summary` | Department summary for a month |
| GET | `/analytics/leaderboard/late` | Late-comers ranking for a month |
| GET | `/analytics/departments/{department}/trend` | Daily series with moving average |
| GET | `/admin/explain/{endpoint}` | MongoDB `explain` output for an endpoint's main query |

## Getting started

### Prerequisites

- Python 3.11 or newer
- A MongoDB 6.0+ deployment: a local instance, a Docker container, or a free MongoDB Atlas (M0) cluster

### Installation

```bash
git clone https://github.com/Mousam098/attendence-api-assignment.git
cd attendence-api-assignment

python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (cmd)
.venv\Scripts\activate

pip install -r requirements.txt
```

### Configuration

Configuration is read from environment variables. A local `.env` file is supported as a fallback; real environment
variables always take priority.

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

| Variable | Description | Example |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

For a local MongoDB container: `docker run -p 27017:27017 mongo:7`.

### Run

```bash
uvicorn app.main:app --port 8000
```

Indexes are created idempotently at startup; no manual setup is needed. Then open:

- `http://localhost:8000/health`: readiness check
- `http://localhost:8000/docs`: interactive API documentation

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

- `tests/test_rules.py`: unit tests for the business rules (lateness, overtime, rounding, overnight shifts, working
  days). No database required.
- `tests/test_api.py`: integration tests covering analytics edge cases (ties, employees without logs, weekend records,
  mid-month joiners), concurrency races and index usage in the explain output. They run against the database in
  `MONGO_URI`, using a separate `attendance_test` database that is dropped afterwards, and are skipped when MongoDB is
  not reachable.

Continuous integration runs the full suite against MongoDB 7 on every push (`.github/workflows/ci.yml`).

## Data model and business rules

- Employees are identified everywhere by `emp_code`; MongoDB's `_id` never appears in the API.
- Instants are epoch **milliseconds** in the API and BSON dates in MongoDB. Calendar dates (`YYYY-MM-DD`), months
  (`YYYY-MM`) and shift times (`HH:MM`) are IST strings.
- Late, overtime, work hours and half day follow the rules R1 to R10 in [`openapi.yaml`](openapi.yaml). Derived values
  are stored with the record, and analytics read the stored values.
- Collections and field types are described in [`DATA_MODEL.md`](DATA_MODEL.md).

### Indexes

| Collection | Index | Serves |
|---|---|---|
| `employees` | unique `emp_code` | Identity and duplicate protection |
| `employees` | `department, emp_code` | Filtered, ordered listing |
| `employees` | `department, joined_on` and `joined_on` | Headcount and trend queries |
| `attendance_logs` | unique `emp_code, date` | One record per day; punch-in race guard |
| `attendance_logs` | `date desc, emp_code` | Default listing order, month scans |
| `attendance_logs` | `status, date desc, emp_code` | Status-filtered listing |
| `attendance_logs` | `emp_code, punch_in desc` | Locating the open record at punch-out |

## Project structure

```
.
├── app/
│   └── main.py              # application: configuration, rules, models, pipelines, endpoints
├── tests/
│   ├── test_rules.py        # unit tests, no database
│   └── test_api.py          # integration tests, require MongoDB
├── .github/workflows/ci.yml # CI pipeline
├── openapi.yaml             # API contract and business rules
├── DATA_MODEL.md            # stored data layout
├── DECISIONS.md             # design decisions
├── REVIEW.md                # review of the original starter code
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

## Documentation

- [`DECISIONS.md`](DECISIONS.md): indexing, concurrency, ranking and scaling decisions
- [`REVIEW.md`](REVIEW.md): defects found in the original starter code and how they were fixed
