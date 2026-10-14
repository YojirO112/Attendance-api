"""Unit tests for the business rules R1-R5 and the request validation. No database needed:

    pip install -r requirements-dev.txt
    pytest
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app import main as m

IST = m.IST


def ist(y, mo, d, hh, mm, ss=0, micro=0):
    """An IST wall-clock time as an aware UTC datetime."""
    return datetime(y, mo, d, hh, mm, ss, micro, tzinfo=IST).astimezone(timezone.utc)


# ---------------------------------------------------------------- R1: attendance date
def test_date_is_the_ist_date_not_the_utc_date():
    # 00:30 IST on the 7th is still the 6th in UTC
    assert m.attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == "2026-07-07"


def test_overnight_early_morning_punch_in_belongs_to_previous_day():
    assert m.attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"


def test_day_shift_is_never_shifted_back():
    assert m.attendance_date(ist(2026, 7, 7, 5, 0), "09:30", "18:30") == "2026-07-07"


def test_instants_are_truncated_to_whole_seconds():
    ms = int(ist(2026, 7, 6, 9, 40, 0, 900_000).timestamp() * 1000)
    assert m.ms_to_dt(ms) == ist(2026, 7, 6, 9, 40, 0)


def test_epoch_conversion_round_trip_and_naive_is_utc():
    moment = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
    assert m.ms_to_dt(m.dt_to_ms(moment)) == moment
    assert m.dt_to_ms(datetime(2026, 7, 6, 4, 0)) == m.dt_to_ms(moment)
    assert m.dt_to_ms(None) is None


# ---------------------------------------------------------------- R2: late
@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 40, 0, 0),    # exactly 10:00 is still inside the grace period
    (9, 40, 1, 10),   # one second later: late, counted from shift start
    (10, 15, 59, 45),
    (9, 20, 0, 0),    # early
    (9, 30, 0, 0),
])
def test_late_minutes(h, mi, s, expected):
    assert m.compute_late_minutes(ist(2026, 7, 6, h, mi, s), "2026-07-06", "09:30") == expected


def test_late_for_overnight_shift_uses_the_shift_start_of_the_attendance_date():
    punch = ist(2026, 7, 6, 22, 20)
    assert m.compute_late_minutes(punch, "2026-07-06", "22:00") == 20


# ---------------------------------------------------------------- R3: overtime
@pytest.mark.parametrize("hh,mm,expected", [(18, 59, 0), (19, 0, 30), (19, 10, 40), (18, 0, 0)])
def test_overtime_needs_at_least_30_minutes(hh, mm, expected):
    assert m.compute_overtime(ist(2026, 7, 6, hh, mm), "2026-07-06", "09:30", "18:30") == expected


def test_overtime_for_overnight_shift_ends_on_the_next_day():
    assert m.compute_overtime(ist(2026, 7, 7, 6, 40), "2026-07-06", "22:00", "06:00") == 40
    assert m.compute_overtime(ist(2026, 7, 7, 5, 0), "2026-07-06", "22:00", "06:00") == 0


# ---------------------------------------------------------------- R4 / R5: work hours, half day
def test_work_hours_round_half_up():
    start = ist(2026, 7, 6, 9, 0)
    assert m.compute_work_hours(start, start + timedelta(seconds=18)) == 0.01  # 0.005 -> 0.01 (not banker's)
    assert m.compute_work_hours(start, start + timedelta(hours=9, minutes=7, seconds=12)) == 9.12
    assert m.compute_work_hours(start, start + timedelta(hours=3, minutes=30)) == 3.5


def test_half_day_uses_the_rounded_value():
    assert m.compute_half_day(4.49) is True
    assert m.compute_half_day(4.5) is False
    start = ist(2026, 7, 6, 9, 0)
    just_under = start + timedelta(hours=4, minutes=29, seconds=59)  # 4.4997h rounds to 4.50
    assert m.compute_half_day(m.compute_work_hours(start, just_under)) is False


def test_derive_fields_for_each_situation():
    pin, pout = ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10)
    full = m.derive_fields("PRESENT", pin, pout, "2026-07-06", "09:30", "18:30")
    assert full == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}
    open_ = m.derive_fields("WFH", pin, None, "2026-07-06", "09:30", "18:30")
    assert open_ == {"work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}
    gone = m.derive_fields("LEAVE", None, None, "2026-07-06", "09:30", "18:30")
    assert gone == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


# ---------------------------------------------------------------- calendar helpers (R7)
def test_month_bounds_and_weekdays():
    assert m.month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert m.month_bounds("2028-02")[1] == date(2028, 2, 29)
    assert m.month_bounds("2026-12")[1] == date(2026, 12, 31)
    assert m.weekdays_between(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert m.weekdays_between(date(2026, 7, 13), date(2026, 7, 31)) == 15  # mid-month joiner
    assert m.weekdays_between(date(2026, 8, 1), date(2026, 7, 31)) == 0
    assert m.weekend_dates(date(2026, 7, 1), date(2026, 7, 31))[:2] == ["2026-07-04", "2026-07-05"]


def test_half_up_helper():
    assert str(m.half_up(2.675, 2)) == "2.68"  # float rounding would give 2.67
    assert str(m.half_up(0.00625, 4)) == "0.0063"


# ---------------------------------------------------------------- validation
def employee(**over):
    base = dict(emp_code="EMP0001", name="Asha Rao", email="asha@example.com", department="Engineering", joined_on="2026-01-05")
    return {**base, **over}


def test_employee_defaults_and_valid_body():
    body = m.EmployeeIn(**employee())
    assert (body.shift_start, body.shift_end) == ("09:30", "18:30")


@pytest.mark.parametrize("over", [
    {"emp_code": "E1"}, {"emp_code": "emp0001"}, {"emp_code": "EMP1234567"},
    {"name": ""}, {"name": "   "}, {"email": "no-at-sign"}, {"department": ""},
    {"shift_start": "9:30"}, {"shift_start": "24:00"}, {"shift_end": "09:30"},
    {"joined_on": "2026-02-30"}, {"joined_on": "05-01-2026"}, {"joined_on": "2026-1-5"},
])
def test_employee_rejects_bad_input(over):
    with pytest.raises(ValidationError):
        m.EmployeeIn(**employee(**over))


@pytest.mark.parametrize("value", [1783312500, 1783312500000.0, "1783312500000", True, 4102444800001, 0, -5])
def test_instants_must_be_strict_epoch_milliseconds(value):
    with pytest.raises(ValidationError):
        m.PunchInIn(emp_code="EMP0001", punched_at=value)


def test_punch_in_accepts_ms_and_only_presence_statuses():
    assert m.PunchInIn(emp_code="EMP0001", punched_at=1783312500000).status == "PRESENT"
    with pytest.raises(ValidationError):
        m.PunchInIn(emp_code="EMP0001", status="ABSENT")


def test_regularize_body_rules():
    ok = m.RegularizeIn(punch_out=1783312500000, reason="forgot to punch", regularized_by="hr")
    assert ok.status is None and ok.punch_in is None
    for bad in ({"reason": "abc"}, {"regularized_by": ""}, {"punch_in": None}, {"status": "HOLIDAY"}):
        with pytest.raises(ValidationError):
            m.RegularizeIn(**{"reason": "long enough", "regularized_by": "hr", **bad})


def test_derived_fields_sent_by_a_client_are_ignored():
    body = m.RegularizeIn(punch_out=1783312500000, reason="forgot to punch", regularized_by="hr",
                          work_hours=99, late_minutes=99)
    assert not hasattr(body, "work_hours")


def test_month_validation():
    from pydantic import TypeAdapter
    adapter = TypeAdapter(m.Month)
    assert adapter.validate_python("2026-07") == "2026-07"
    for bad in ("2026-13", "2026-00", "2026-7", "0000-01", "July"):
        with pytest.raises(ValidationError):
            adapter.validate_python(bad)


# ---------------------------------------------------------------- serialisation
def test_record_out_tolerates_legacy_documents_and_never_leaks_id():
    legacy = {"_id": "x", "emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
              "punch_in": datetime(2026, 7, 14, 4, 2), "punch_out": datetime(2026, 7, 14, 13, 1),
              "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0}
    out = m.record_out(legacy)
    assert "_id" not in out and "id" not in out
    assert out["half_day"] is False and out["history"] == []
    assert out["punch_in"] == m.dt_to_ms(datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc))


def test_history_out_converts_nested_instants():
    pin_from, pin_to = datetime(2026, 7, 7, 4, 35), datetime(2026, 7, 7, 3, 58)
    entry = {"at": datetime(2026, 7, 8, 5, 0), "by": "hr.admin", "reason": "biometric glitch",
             "changes": {"punch_in": {"from": pin_from, "to": pin_to}, "late_minutes": {"from": 35, "to": 0}}}
    out = m.history_out(entry)
    assert out["changes"]["punch_in"] == {"from": m.dt_to_ms(pin_from), "to": m.dt_to_ms(pin_to)}
    assert out["changes"]["late_minutes"] == {"from": 35, "to": 0}


def test_work_hours_half_up_where_float_round_goes_wrong():
    start = ist(2026, 7, 6, 9, 0)
    assert m.compute_work_hours(start, start + timedelta(hours=8, minutes=2, seconds=42)) == 8.05  # round() gives 8.04
    assert m.compute_work_hours(start, start + timedelta(hours=8, minutes=7, seconds=30)) == 8.13  # round() gives 8.12
