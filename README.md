# Employee Attendance & Analytics API

FastAPI + MongoDB service for punching in/out, correcting records and answering attendance questions.
All application code is in `app/main.py`; the contract it implements is the supplied `openapi.yaml` (rules R1-R10).

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # optional; real environment variables win over it
docker run -p 27017:27017 mongo:7                      # or any MongoDB 6.0+ / Atlas M0
uvicorn app.main:app --port 8000
```

`MONGO_URI` (default `mongodb://localhost:27017`) and `MONGO_DB` (default `attendance_db`) are read from the environment.
Indexes are created at startup and the call is idempotent. `GET /health` returns 200 only when MongoDB answers a ping.
Interactive docs are at `/docs`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest                      # rules R1-R5, rounding, validation, serialisation (no database needed)
```

For an end-to-end check against a real MongoDB, use the `check_live.py` script that came with the assignment
(run it from this folder, against a throw-away database):

```bash
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_check
python ../check_live.py            # correctness
BIG=1 python ../check_live.py      # plus 100k-record speed and explain check
```

## What is where

| Endpoint | Notes |
|---|---|
| `POST/GET /employees` | unique index on `emp_code`, sorted by `emp_code` |
| `POST /attendance/punch-in` | race-safe through a unique `(emp_code, date)` index |
| `POST /attendance/punch-out` | compare-and-set, works across midnight |
| `PATCH /attendance/{emp_code}/{date}` | recomputes derived fields, appends to `history`, retries on concurrent edits |
| `GET /attendance` | index-backed, paginated |
| `GET /analytics/...` | all four are MongoDB aggregations (`$lookup`, `$facet`, `$rank`, `$setWindowFields`) |
| `GET /admin/explain/{endpoint}` | `executionStats` explain of the query built by the same function the endpoint uses |

`REVIEW.md` lists the defects found in the starter code; `DECISIONS.md` answers the five design questions.

## Notes

* Requires MongoDB 6.0+ (`$setWindowFields`, `$rank`, `$dateAdd`, `$lookup` with `localField` + `pipeline`).
* Rounding is half-up everywhere. In the trend pipeline rates are carried as integer 1/10000 units so that rounding and the
  moving average (taken over the reported, rounded rates) are exact.
* Instants are epoch milliseconds in the API and BSON dates (UTC) in MongoDB; calendar dates and shift times stay strings.
