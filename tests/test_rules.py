"""Unit tests for the attendance rules (R1-R5, R7). They need no database."""
from datetime import date, datetime, timezone

import pytest

from app import main

IST = main.IST


def ist(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(timezone.utc)


MONDAY = date(2026, 7, 6)


@pytest.mark.parametrize(
    "punch, expected",
    [((9, 40, 0), 0), ((9, 40, 1), 10), ((10, 15, 59), 45), ((9, 0, 0), 0)],
)
def test_late_minutes(punch, expected):
    assert main.late_minutes_for(ist(2026, 7, 6, *punch), MONDAY, "09:30") == expected


@pytest.mark.parametrize(
    "punch, expected", [((18, 59, 59), 0), ((19, 0, 0), 30), ((19, 10, 0), 40), ((17, 0, 0), 0)]
)
def test_overtime_minutes(punch, expected):
    end = main.shift_end_instant(MONDAY, "09:30", "18:30")
    assert main.overtime_minutes_for(ist(2026, 7, 6, *punch), end) == expected


def test_work_hours_round_half_up():
    # 16182 seconds is exactly 4.495 hours and must round up to 4.50
    assert str(main.work_hours_for(ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 13, 29, 42))) == "4.50"


def test_half_day_uses_rounded_hours():
    derived = main.derive_fields(
        {"shift_start": "09:30", "shift_end": "18:30"},
        MONDAY,
        ist(2026, 7, 6, 9, 0),
        ist(2026, 7, 6, 13, 29, 42),
    )
    assert derived["work_hours"] == 4.5
    assert derived["half_day"] is False


def test_overnight_attendance_date_and_shift_end():
    assert main.attendance_date(ist(2026, 7, 7, 3, 0), "22:00", "06:00") == date(2026, 7, 6)
    assert main.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == date(2026, 7, 6)
    assert main.attendance_date(ist(2026, 7, 7, 6, 0), "22:00", "06:00") == date(2026, 7, 7)
    assert main.shift_end_instant(date(2026, 7, 6), "22:00", "06:00") == ist(2026, 7, 7, 6, 0)


def test_millisecond_conversion_truncates_to_seconds():
    assert main.dt_to_ms(main.ms_to_dt(1_783_312_500_999)) == 1_783_312_500_000


def test_weekdays_between():
    assert main.weekdays_between(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert main.weekdays_between(date(2026, 7, 15), date(2026, 7, 31)) == 13
