"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:   uvicorn app.main:app --port 8000
Env:   MONGO_URI, MONGO_DB   (real environment variables win over a local .env file)

Layout of this single file
    1. configuration and database handles
    2. validation types and request / response models
    3. business rules R1 - R5 (pure functions, no I/O)
    4. serialisation helpers (BSON <-> API)
    5. index management
    6. query / pipeline builders (shared by the endpoints and by /admin/explain)
    7. endpoints
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, OperationFailure, PyMongoError
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("attendance")

# =========================================================================== #
# 1. Configuration and database handles
# =========================================================================== #
load_dotenv()  # does not override variables that are already set in the environment

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

# tz_aware=True: PyMongo hands back UTC-aware datetimes, so naive/aware arithmetic can never mix.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000, appname="attendance-api")
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = frozenset({"PRESENT", "WFH", "ON_DUTY"})  # R6
NON_PRESENCE = frozenset({"ABSENT", "LEAVE"})

GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW = Decimal("4.50")  # R5
MAX_SHIFT_SPAN = timedelta(hours=24)
MAX_TREND_DAYS = 92
MAX_PATCH_ATTEMPTS = 5
MAX_SKIP = 2**62  # keeps `skip` inside what MongoDB can encode

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


# =========================================================================== #
# 2. Validation types and models
# =========================================================================== #
def _calendar_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a real calendar date (YYYY-MM-DD)") from exc
    return value


def _month_string(value: str) -> str:
    if int(value[:4]) < 1:
        raise ValueError("year must be >= 0001")
    return value


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


# StringConstraints is plain pydantic metadata, so these aliases work in models and as query/path parameters alike.
IsoDate = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_calendar_date)]
Month = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(_month_string)]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# Instants: strict integers (no floats, strings or bools) in epoch milliseconds. Anything below
# 100_000_000_000 looks like seconds and is rejected.
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_not_blank)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50), AfterValidator(_not_blank)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: IsoDate

    @model_validator(mode="after")
    def _shift_must_have_length(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="before")
    @classmethod
    def _explicit_null_is_invalid(cls, data):
        # Omitted means "leave unchanged"; an explicit null is not a valid value for these fields.
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must not be null; omit it to leave it unchanged")
        return data


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: Optional[int]


class EmployeePage(BaseModel):
    items: List[EmployeeOut]
    total: int
    page: int
    page_size: int


class AttendanceOut(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[Dict[str, Any]]


class AttendancePage(BaseModel):
    items: List[AttendanceOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class TrendOut(BaseModel):
    department: str
    items: List[TrendItem]


def invalid(loc: Tuple[str, ...], message: str, value: Any = None):
    """Raise a 422 with FastAPI's standard error body, for rules a type annotation cannot express."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": message, "input": value}])


# =========================================================================== #
# 3. Business rules R1 - R5 (pure functions)
# =========================================================================== #
def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a stored datetime to aware UTC (a naive value is taken to be UTC, as MongoDB stores it)."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def trunc_seconds(value: datetime) -> datetime:
    """R1: instants are cut to whole seconds before any calculation."""
    return value.replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    return trunc_seconds(EPOCH + timedelta(milliseconds=ms))


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return (as_utc(value) - EPOCH) // timedelta(milliseconds=1)


def half_up(value: Any, places: int) -> Decimal:
    """Round half-up on the decimal representation (never banker's rounding, never binary float rounding)."""
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _minutes(shift_end) <= _minutes(shift_start)


def _ist_wall_clock(day: date, hhmm: str, plus_days: int = 0) -> datetime:
    midnight = datetime(day.year, day.month, day.day, tzinfo=IST)
    return (midnight + timedelta(days=plus_days, minutes=_minutes(hhmm))).astimezone(UTC)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in; an overnight shift's early-morning punch-in belongs to the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_into_day < _minutes(shift_end) * 60:
            day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then whole minutes since shift start."""
    start = _ist_wall_clock(date.fromisoformat(date_str), shift_start)
    elapsed = int((punch_in - start).total_seconds())
    return elapsed // 60 if elapsed > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, counted only when at least 30."""
    end = _ist_wall_clock(date.fromisoformat(date_str), shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float(half_up(Decimal(seconds) / Decimal(3600), 2))


def compute_half_day(work_hours: float) -> bool:
    """R5: based on the already rounded work_hours."""
    return Decimal(str(work_hours)) < HALF_DAY_BELOW


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], date_str: str,
                  shift_start: str, shift_end: str) -> Dict[str, Any]:
    """The four API-computed fields for a record in its final state."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, date_str, shift_start, shift_end),
        "half_day": compute_half_day(hours),
    }


def month_bounds(month: str) -> Tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def weekdays_between(start: date, end: date) -> int:
    """Number of Mon-Fri days in [start, end] (R7). Plain calendar arithmetic, no data involved."""
    if end < start:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def weekend_dates(first: date, last: date) -> List[str]:
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)
            if (first + timedelta(days=i)).weekday() >= 5]


# =========================================================================== #
# 4. Serialisation (BSON <-> API)
# =========================================================================== #
def _api_value(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def history_out(entry: Dict[str, Any]) -> Dict[str, Any]:
    changes = {field: {"from": _api_value(pair.get("from")), "to": _api_value(pair.get("to"))}
               for field, pair in (entry.get("changes") or {}).items()}
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: Dict[str, Any]) -> Dict[str, Any]:
    """An attendance document as the API shows it. Tolerates legacy documents (missing derived fields)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def employee_out(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"], "department": doc["department"],
        "shift_start": doc["shift_start"], "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value or 0))


def jsonable(document: Any) -> Any:
    """Turn a raw MongoDB reply (ObjectId, datetime, Decimal128 ...) into plain JSON types."""
    return json.loads(json_util.dumps(document, json_options=json_util.RELAXED_JSON_OPTIONS))


# =========================================================================== #
# 5. Index management
# =========================================================================== #
# (collection, keys, options). Names are explicit so that re-running is a no-op.
INDEX_SPECS = [
    # Business key and the guarantee behind concurrent employee creation.
    (employees, [("emp_code", ASCENDING)], {"name": "uq_emp_code", "unique": True}),
    # Filter by department, sorted by emp_code; also the department lookup for trend / summary.
    (employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "ix_dept_emp"}),
    # Headcount: joined_on <= end of period.
    (employees, [("joined_on", ASCENDING)], {"name": "ix_joined_on"}),
    # One record per employee per day: the punch-in race guarantee, the natural key for PATCH,
    # and the access path for per-employee ranges (monthly summary, $lookup from employees).
    (attendance, [("emp_code", ASCENDING), ("date", ASCENDING)], {"name": "uq_emp_date", "unique": True}),
    # GET /attendance ordering (date desc, emp_code asc), date-range scans and the leaderboard month scan.
    (attendance, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_date_emp"}),
]

_indexes_ready = False


def _create_index(collection, keys, options: Dict[str, Any]) -> None:
    try:
        collection.create_index(keys, **options)
    except OperationFailure as exc:
        if exc.code in (85, 86):  # same keys / same name already exist with other options: keep what is there
            log.warning("index %s already exists with different options; leaving it", options["name"])
        elif options.get("unique"):
            # e.g. pre-existing duplicates in the data. Stay available, but say loudly that the guarantee is weaker.
            log.error("could not build unique index %s (%s); falling back to a non-unique index", options["name"], exc)
            collection.create_index(keys, name=options["name"] + "_nonunique")
        else:
            raise


def ensure_indexes() -> None:
    """Idempotent; safe to call from several workers at once."""
    global _indexes_ready
    for collection, keys, options in INDEX_SPECS:
        _create_index(collection, keys, options)
    _indexes_ready = True


# =========================================================================== #
# 6. Query and pipeline builders (shared by the endpoints and /admin/explain)
# =========================================================================== #
@dataclass
class Plan:
    """A find or an aggregate against one collection."""
    collection: str
    pipeline: Optional[List[Dict[str, Any]]] = None  # aggregate when set
    filter: Optional[Dict[str, Any]] = None  # find otherwise
    sort: Optional[List[Tuple[str, int]]] = None
    skip: int = 0
    limit: int = 0


def _skip(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def plan_attendance_list(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                         status: Optional[str], page: int, page_size: int) -> Plan:
    if date_from and date_to and date_from > date_to:
        invalid(("query", "date_from"), "date_from must not be after date_to", date_from)
    query: Dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    window: Dict[str, str] = {}
    if date_from:
        window["$gte"] = date_from
    if date_to:
        window["$lte"] = date_to
    if window:
        query["date"] = window
    if status:
        query["status"] = status
    return Plan("attendance_logs", filter=query, sort=[("date", DESCENDING), ("emp_code", ASCENDING)],
                skip=_skip(page, page_size), limit=page_size)


# Expression fragments shared by the aggregations. R6 + R5: a presence record is worth 1, or 0.5 on a half day.
_IS_PRESENT = {"$in": ["$status", sorted(PRESENCE)]}
_PRESENT_VALUE = {"$cond": [_IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}
_IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def _count_if(condition: Dict[str, Any]) -> Dict[str, Any]:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def _month_totals_stage(weekend: List[str]) -> Dict[str, Any]:
    """One $group over a month of records. present_days only counts Mon-Fri (R7); everything else counts all days."""
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [{"$in": ["$date", weekend]}, 0, _PRESENT_VALUE]}},
        "late_count": _count_if(_IS_LATE),
        "total_late": {"$sum": "$late_minutes"},
        "total_overtime": {"$sum": "$overtime_minutes"},
        "leave_count": _count_if({"$eq": ["$status", "LEAVE"]}),
        "on_duty_count": _count_if({"$eq": ["$status", "ON_DUTY"]}),
        # Exact decimal sum so the mean can be rounded half-up without binary float noise.
        "hours_sum": {"$sum": {"$cond": [_HAS_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
        "hours_n": _count_if(_HAS_HOURS),
    }}


def plan_employee_monthly(emp_code: str, month: str) -> Plan:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        _month_totals_stage(weekend_dates(first, last)),
    ]
    return Plan("attendance_logs", pipeline=pipeline)


def plan_department_summary(month: str, department: Optional[str]) -> Plan:
    """Starts from employees (so people with no logs still count), joins each employee's month of logs,
    then folds per employee totals into per department totals. Headcount = one row per employee."""
    first, last = month_bounds(month)
    who: Dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        who["department"] = department
    pipeline = [
        {"$match": who},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "s",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                _month_totals_stage(weekend_dates(first, last)),
            ],
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$s.present_days"},
            "late_count": {"$sum": "$s.late_count"},
            "total_late_minutes": {"$sum": "$s.total_late"},
            "leave_count": {"$sum": "$s.leave_count"},
            "on_duty_count": {"$sum": "$s.on_duty_count"},
            "hours_sum": {"$sum": "$s.hours_sum"},
            "hours_n": {"$sum": "$s.hours_n"},
        }},
        {"$sort": {"_id": 1}},
    ]
    return Plan("employees", pipeline=pipeline)


def plan_late_leaderboard(month: str, limit: int, department: Optional[str]) -> Plan:
    """Total late minutes per employee, then standard competition ranking with $rank
    (ties share a rank, the next rank is skipped). `limit` is applied to the rank, not to the row count."""
    first, last = month_bounds(month)
    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of employees that do not exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return Plan("attendance_logs", pipeline=pipeline)


def _round_div_half_up(numerator: Any, denominator: Any) -> Dict[str, Any]:
    """floor((2n + d) / 2d) written with integer-valued arithmetic only: exact half-up division, no float noise."""
    top = {"$add": [{"$multiply": [2, numerator]}, denominator]}
    bottom = {"$multiply": [2, denominator]}
    return {"$divide": [{"$subtract": [top, {"$mod": [top, bottom]}]}, bottom]}


def plan_department_trend(department: str, d_from: str, d_to: str) -> Plan:
    """One row per calendar day, built inside the database:
    employees -> $lookup their logs in range -> $facet (join dates | per day totals) -> $range of days
    -> per day headcount / present / late -> rate in 1/10000 units -> $setWindowFields moving average.
    Rates are carried as integer 1/10000 ticks so half-up rounding is exact and the moving average is
    taken over the *reported* (rounded) rates."""
    start = datetime.fromisoformat(d_from).replace(tzinfo=UTC)
    days = (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs",
            "pipeline": [
                {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                {"$project": {"_id": 0, "date": 1, "v": _PRESENT_VALUE, "late": {"$cond": [_IS_LATE, 1, 0]}}},
            ],
        }},
        {"$facet": {
            "joins": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
            "daily": [
                {"$unwind": "$logs"},
                {"$group": {"_id": "$logs.date", "present": {"$sum": "$logs.v"}, "late": {"$sum": "$logs.late"}}},
            ],
        }},
        {"$project": {"_id": 0, "rows": {"$map": {
            "input": {"$range": [0, days]}, "as": "i",
            "in": {"$let": {
                "vars": {"day": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}}},
                "in": {"$let": {
                    "vars": {"ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$day"}},
                             "dow": {"$isoDayOfWeek": "$$day"}},
                    "in": {
                        "date": "$$ds",
                        "is_working_day": {"$lte": ["$$dow", 5]},
                        "headcount": {"$sum": {"$map": {
                            "input": "$joins", "as": "j",
                            "in": {"$cond": [{"$lte": ["$$j._id", "$$ds"]}, "$$j.n", 0]}}}},
                        "present_count": {"$sum": {"$map": {
                            "input": {"$filter": {"input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$$ds"]}}},
                            "as": "x", "in": "$$x.present"}}},
                        "late_count": {"$sum": {"$map": {
                            "input": {"$filter": {"input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$$ds"]}}},
                            "as": "x", "in": "$$x.late"}}},
                    },
                }},
            }},
        }}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$addFields": {"ticks": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _round_div_half_up({"$multiply": ["$present_count", 10000]}, "$headcount"),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "window_sum": {"$sum": "$ticks", "window": {"documents": [-6, 0]}},
                "window_n": {"$sum": {"$cond": [{"$isNumber": "$ticks"}, 1, 0]}, "window": {"documents": [-6, 0]}},
            },
        }},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$isNumber": "$ticks"}, {"$divide": ["$ticks", 10000]}, None]},
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$window_n", 0]},
                {"$divide": [_round_div_half_up("$window_sum", "$window_n"), 10000]},
                None,
            ]},
        }},
    ]
    return Plan("employees", pipeline=pipeline)


def run_find(plan: Plan) -> List[Dict[str, Any]]:
    skip = plan.skip if plan.skip < MAX_SKIP else MAX_SKIP
    cursor = db[plan.collection].find(plan.filter, {"_id": 0}).sort(plan.sort).skip(skip).limit(plan.limit)
    return list(cursor)


def run_aggregate(plan: Plan) -> List[Dict[str, Any]]:
    return list(db[plan.collection].aggregate(plan.pipeline, allowDiskUse=True))


def explain(plan: Plan) -> Dict[str, Any]:
    if plan.pipeline is not None:
        inner = {"aggregate": plan.collection, "pipeline": plan.pipeline, "cursor": {}, "allowDiskUse": True}
    else:
        inner = {"find": plan.collection, "filter": plan.filter or {}}
        if plan.sort:
            inner["sort"] = {key: direction for key, direction in plan.sort}
        if plan.skip:
            inner["skip"] = min(plan.skip, MAX_SKIP)
        if plan.limit:
            inner["limit"] = plan.limit
    return jsonable(db.command({"explain": inner, "verbosity": "executionStats"}))


# =========================================================================== #
# 7. Application and endpoints
# =========================================================================== #
@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await run_in_threadpool(ensure_indexes)
    except PyMongoError:
        # Stay up: /health reports 503 while the database is unreachable and retries index creation.
        log.exception("index creation failed at startup; will retry from /health")
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def database_unavailable(_: Request, __: ConnectionFailure):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def get_employee(emp_code: str) -> Dict[str, Any]:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def now_utc() -> datetime:
    return trunc_seconds(datetime.now(UTC))


# ---------------------------------------------------------------- health
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "database unavailable") from exc
    if not _indexes_ready:
        try:
            ensure_indexes()
        except PyMongoError:
            log.exception("index creation failed")
    return {"status": "ok"}


# ---------------------------------------------------------------- employees
@app.post("/employees", status_code=201, response_model=EmployeeOut, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    total = employees.count_documents(query)
    skip = min(_skip(page, page_size), MAX_SKIP)
    rows = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    return {"items": [employee_out(r) for r in rows], "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------- attendance
@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceOut, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    moment = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(moment, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": moment,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(moment, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance.insert_one(doc)  # unique (emp_code, date): of N simultaneous requests exactly one inserts
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out", response_model=AttendanceOut, tags=["attendance"])
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    moment = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record that started at or before the punch-out; this naturally spans midnight.
    rec = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": moment}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    started = trunc_seconds(as_utc(rec["punch_in"]))
    if moment <= started:
        invalid(("body", "punched_at"), "punched_at must be after the punch-in", body.punched_at)
    if moment - started > MAX_SHIFT_SPAN:
        invalid(("body", "punched_at"), "punched_at is more than 24 hours after the punch-in", body.punched_at)
    derived = derive_fields(rec["status"], started, moment, rec["date"], emp["shift_start"], emp["shift_end"])
    # The filter makes this a compare-and-set: only one of several simultaneous punch-outs can match.
    updated = attendance.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": {"punch_out": moment, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[IsoDate] = None,
                    date_to: Optional[IsoDate] = None, status: Optional[Status] = None,
                    page: Page = 1, page_size: PageSize = 20):
    plan = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    total = attendance.count_documents(plan.filter)
    return {"items": [record_out(r) for r in run_find(plan)], "total": total, "page": page, "page_size": page_size}


REGULARIZED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def _stored_state(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The seven correctable / derived fields of a stored record, with legacy gaps filled in."""
    return {
        "status": rec["status"],
        "punch_in": trunc_seconds(as_utc(rec["punch_in"])) if rec.get("punch_in") else None,
        "punch_out": trunc_seconds(as_utc(rec["punch_out"])) if rec.get("punch_out") else None,
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
    }


def build_regularization(rec: Dict[str, Any], emp: Dict[str, Any], body: RegularizeIn) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate the request against the stored record; return (new field values, changes) or raise 422."""
    old = _stored_state(rec)
    status = body.status or old["status"]
    supplied_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    supplied_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

    if status in NON_PRESENCE:
        if supplied_in is not None or supplied_out is not None:
            invalid(("body", "status"), f"{status} clears the punch times; do not send punch_in or punch_out with it")
        punch_in_value = punch_out_value = None
    else:
        punch_in_value = supplied_in or old["punch_in"]
        punch_out_value = supplied_out or old["punch_out"]
        if punch_in_value is None:
            invalid(("body", "punch_in"), f"status {status} requires a punch_in")
        if supplied_in is not None and attendance_date(supplied_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            invalid(("body", "punch_in"), "punch_in must stay on the record's attendance date", body.punch_in)
        if punch_out_value is not None and (supplied_in or supplied_out):
            if punch_out_value <= punch_in_value:
                invalid(("body", "punch_out"), "punch_out must be after punch_in", body.punch_out)
            if punch_out_value - punch_in_value > MAX_SHIFT_SPAN:
                invalid(("body", "punch_out"), "punch_out must be within 24 hours of punch_in", body.punch_out)

    new = {"status": status, "punch_in": punch_in_value, "punch_out": punch_out_value,
           **derive_fields(status, punch_in_value, punch_out_value, rec["date"], emp["shift_start"], emp["shift_end"])}
    changes = {field: {"from": old[field], "to": new[field]} for field in REGULARIZED_FIELDS if old[field] != new[field]}
    if not changes:
        invalid(("body",), "the request changes nothing")
    return new, changes


def _history_guard(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Match only while the audit trail still has the length we read (optimistic concurrency)."""
    size = len(rec.get("history") or [])
    if size:
        return {"history": {"$size": size}}
    return {"$or": [{"history": {"$exists": False}}, {"history": None}, {"history": {"$size": 0}}]}


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceOut, tags=["attendance"])
def regularize(emp_code: str, date: IsoDate, body: RegularizeIn):
    emp = get_employee(emp_code)
    for _ in range(MAX_PATCH_ATTEMPTS):
        rec = attendance.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = build_regularization(rec, emp, body)
        entry = {"at": datetime.now(UTC), "by": body.regularized_by,
                 "reason": body.reason, "changes": changes}
        guard = {"_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"),
                 "punch_out": rec.get("punch_out"), **_history_guard(rec)}
        updated = attendance.find_one_and_update(guard, {"$set": new, "$push": {"history": entry}},
                                                 return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_out(updated)
        # Somebody else changed the record between our read and write: re-read and re-apply.
    raise HTTPException(409, "record is being modified concurrently; retry")


# ---------------------------------------------------------------- analytics
@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyOut, tags=["analytics"])
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    rows = run_aggregate(plan_employee_monthly(emp_code, month))
    totals = rows[0] if rows else {}
    try:
        joined = date.fromisoformat(emp["joined_on"])
    except ValueError:
        joined = first
    working_days = weekdays_between(max(first, joined), last)  # R7: counted from joined_on
    present = to_decimal(totals.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(half_up(present, 2)),
        "leave_days": int(totals.get("leave_count", 0)),
        "late_count": int(totals.get("late_count", 0)),
        "total_late_minutes": int(totals.get("total_late", 0)),
        "total_overtime_minutes": int(totals.get("total_overtime", 0)),
        "attendance_pct": float(half_up(present * 100 / working_days, 2)) if working_days else None,
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryOut, tags=["analytics"])
def department_summary(month: Month, department: Optional[str] = None):
    items = []
    for row in run_aggregate(plan_department_summary(month, department)):
        hours_n = int(row["hours_n"])
        mean_hours = to_decimal(row["hours_sum"]) / hours_n if hours_n else None
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": float(half_up(to_decimal(row["present_days"]), 2)),
            "avg_work_hours": float(half_up(mean_hours, 2)) if mean_hours is not None else None,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=LeaderboardOut, tags=["analytics"])
def late_leaderboard(month: Month, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    return {"month": month, "items": run_aggregate(plan_late_leaderboard(month, limit, department))}


def _trend_range(d_from: str, d_to: str) -> None:
    span = (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1
    if span < 1:
        invalid(("query", "to"), "to must not be before from", d_to)
    if span > MAX_TREND_DAYS:
        invalid(("query", "to"), f"the range may not exceed {MAX_TREND_DAYS} days", d_to)


@app.get("/analytics/departments/{department}/trend", response_model=TrendOut, tags=["analytics"])
def department_trend(department: str, from_: Annotated[IsoDate, Query(alias="from")], to: IsoDate):
    _trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": run_aggregate(plan_department_trend(department, from_, to))}


# ---------------------------------------------------------------- admin
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _required(**values: Any) -> None:
    for name, value in values.items():
        if value is None:
            invalid(("query", name), "field required")


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[Month] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[IsoDate] = None,
    date_to: Optional[IsoDate] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[IsoDate], Query(alias="from")] = None,
    to: Optional[IsoDate] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """executionStats explain of the endpoint's main query, built by the same function the endpoint uses."""
    if endpoint == "attendance_list":
        plan = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        _required(emp_code=emp_code, month=month)
        plan = plan_employee_monthly(emp_code, month)
    elif endpoint == "department_summary":
        _required(month=month)
        plan = plan_department_summary(month, department)
    elif endpoint == "late_leaderboard":
        _required(month=month)
        plan = plan_late_leaderboard(month, limit, department)
    else:
        _required(department=department, **{"from": from_, "to": to})
        _trend_range(from_, to)
        plan = plan_department_trend(department, from_, to)
    return {"endpoint": endpoint, "collection": plan.collection, "explain": explain(plan)}
