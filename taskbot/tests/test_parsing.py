from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from taskbot.models import Priority, Status
from taskbot.parsing import (
    ParseError,
    add_months,
    extract_hashtags,
    format_offset,
    is_drive_url,
    next_occurrence,
    parse_date,
    parse_duration_minutes,
    parse_kv,
    parse_priority,
    parse_reminder,
    parse_status,
    parse_tag_ops,
    split_title_and_urls,
)

TODAY = date(2026, 11, 20)  # Friday
TZ = ZoneInfo("Europe/Rome")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("25/12/2026", date(2026, 12, 25)),
        ("5/1/27", date(2027, 1, 5)),
        ("05.03.2027", date(2027, 3, 5)),
        ("24/12", date(2026, 12, 24)),
        ("today", TODAY),
        ("tomorrow", date(2026, 11, 21)),
        ("+7", date(2026, 11, 27)),
        ("+2w", date(2026, 12, 4)),
        ("+1m", date(2026, 12, 20)),
        ("monday", date(2026, 11, 23)),
        ("fri", date(2026, 11, 27)),
        ("none", None),
        ("-", None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text, TODAY) == expected


@pytest.mark.parametrize("text", ["31/02/2026", "soon", "2026-13-01"])
def test_parse_date_invalid(text):
    with pytest.raises(ParseError):
        parse_date(text, TODAY)


def test_add_months_clamps():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert next_occurrence(date(2028, 2, 29), "yearly") == date(2029, 2, 28)
    assert next_occurrence(date(2026, 11, 20), "weekly") == date(2026, 11, 27)


def test_durations():
    assert parse_duration_minutes("1d") == 1440
    assert parse_duration_minutes("1d12h") == 2160
    assert parse_duration_minutes("30m") == 30
    assert parse_duration_minutes("1w") == 10080
    assert format_offset(2160) == "1d12h"
    with pytest.raises(ParseError):
        parse_duration_minutes("abc")


def test_parse_reminder():
    now = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)  # 10:00 Rome
    nine = time(9, 0)
    assert parse_reminder("-1d", now, TZ, nine) == ("rel", 1440)
    assert parse_reminder("-2h", now, TZ, nine) == ("rel", 120)
    kind, dt = parse_reminder("in 2h", now, TZ, nine)
    assert kind == "abs" and dt == now + (dt - now) and (dt - now).total_seconds() == 7200
    kind, dt = parse_reminder("25/11/2026 14:30", now, TZ, nine)
    assert dt == datetime(2026, 11, 25, 14, 30, tzinfo=TZ)
    kind, dt = parse_reminder("25/11/2026", now, TZ, nine)
    assert dt == datetime(2026, 11, 25, 9, 0, tzinfo=TZ)
    kind, dt = parse_reminder("tomorrow 10:00", now, TZ, nine)
    assert dt == datetime(2026, 11, 21, 10, 0, tzinfo=TZ)
    kind, dt = parse_reminder("08:00", now, TZ, nine)  # already passed today -> tomorrow
    assert dt == datetime(2026, 11, 21, 8, 0, tzinfo=TZ)
    with pytest.raises(ParseError):
        parse_reminder("whenever", now, TZ, nine)


@pytest.mark.parametrize(
    "text,expected",
    [("0", Priority.CRITICAL), ("1-High", Priority.HIGH), ("medium", Priority.MEDIUM), ("9", Priority.UNDEFINED)],
)
def test_parse_priority(text, expected):
    assert parse_priority(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0", Status.TO_START),
        ("1_In_Progress", Status.IN_PROGRESS),
        ("4-Waiting_for_Other", Status.WAITING_FOR_OTHER),
        ("done", Status.COMPLETED),
        ("9", Status.SUSPENDED),
    ],
)
def test_parse_status(text, expected):
    assert parse_status(text) == expected


def test_parse_enum_invalid():
    with pytest.raises(ParseError):
        parse_priority("4")
    with pytest.raises(ParseError):
        parse_status("6")


def test_links_and_tags():
    title, urls = split_title_and_urls("Check this https://example.com/a?b=1, please")
    assert title == "Check this , please" or title.startswith("Check this")
    assert urls == ["https://example.com/a?b=1"]
    assert is_drive_url("https://drive.google.com/file/d/abc/view")
    assert is_drive_url("https://docs.google.com/document/d/abc")
    assert not is_drive_url("https://example.com/drive.google.com")
    assert extract_hashtags("Fix bug #Work #urgent-1 a#b") == ["work", "urgent-1"]
    assert parse_tag_ops("+a -b c #D") == (["a", "c", "d"], ["b"])


def test_parse_kv():
    kv, rest = parse_kv('all project="My Project" feature=API')
    assert kv == {"project": "My Project", "feature": "API"} and rest == ["all"]
