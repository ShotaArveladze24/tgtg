"""Parsing of user input: dates, reminders, enums, links, tags."""

from __future__ import annotations

import re
import shlex
from datetime import date, datetime, time, timedelta
from typing import Optional, Union
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .models import RECURRENCES, Priority, Status

DATE_FMT = "%d/%m/%Y"
DATETIME_FMT = "%d/%m/%Y %H:%M"

NO_DATE_WORDS = {"none", "no", "nodate", "no date", "-", "never", "null", "nessuna"}

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
DURATION_PART_RE = re.compile(r"(\d+)\s*([wdhm])", re.IGNORECASE)
DURATION_RE = re.compile(r"^(\d+\s*[wdhm]\s*)+$", re.IGNORECASE)
_UNIT_MIN = {"w": 7 * 24 * 60, "d": 24 * 60, "h": 60, "m": 1}


class ParseError(ValueError):
    pass


# --------------------------------------------------------------------- dates
def format_date(d: Optional[date]) -> str:
    return d.strftime(DATE_FMT) if d else "No date"


def format_datetime(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime(DATETIME_FMT)


def parse_date(text: str, today: date) -> Optional[date]:
    """Parse a deadline. Returns None for 'no date'. Raises ParseError when invalid.

    Accepted: DD/MM/YYYY, DD/MM/YY, DD/MM, DD-MM-YYYY, DD.MM.YYYY, today, tomorrow,
    +Nd / +Nw / +Nm (days, weeks, months), weekday names (next occurrence), none.
    """
    s = text.strip().lower()
    if s in NO_DATE_WORDS:
        return None
    if s in ("today", "oggi"):
        return today
    if s in ("tomorrow", "domani"):
        return today + timedelta(days=1)
    m = re.fullmatch(r"\+(\d+)\s*([dwm]?)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2) or "d"
        if unit == "d":
            return today + timedelta(days=n)
        if unit == "w":
            return today + timedelta(weeks=n)
        return add_months(today, n)
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, name in enumerate(weekdays):
        if s in (name, name[:3]):
            delta = (i - today.weekday()) % 7 or 7
            return today + timedelta(days=delta)
    m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2}|\d{4}))?", s)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = m.group(3)
        if year is None:
            y = today.year
        elif len(year) == 2:
            y = 2000 + int(year)
        else:
            y = int(year)
        try:
            return date(y, month, day)
        except ValueError as exc:
            raise ParseError(f"Invalid date: {text}") from exc
    raise ParseError(f"Unrecognized date '{text}'. Use DD/MM/YYYY, today, tomorrow, +3d, monday or none.")


def add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year, month = d.year + month_index // 12, month_index % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, min(d.day, day))
        except ValueError:
            continue
    raise ValueError("unreachable")


def next_occurrence(d: date, recurrence: str) -> date:
    if recurrence == "daily":
        return d + timedelta(days=1)
    if recurrence == "weekly":
        return d + timedelta(weeks=1)
    if recurrence == "monthly":
        return add_months(d, 1)
    if recurrence == "yearly":
        return add_months(d, 12)
    raise ParseError(f"Unknown recurrence '{recurrence}'")


def parse_recurrence(text: str) -> Optional[str]:
    s = text.strip().lower()
    if s in NO_DATE_WORDS or s in ("off", "stop"):
        return None
    if s in RECURRENCES:
        return s
    raise ParseError(f"Recurrence must be one of: {', '.join(RECURRENCES)}, none")


# ----------------------------------------------------------------- durations
def parse_duration_minutes(text: str) -> int:
    s = text.strip().lower()
    if not DURATION_RE.fullmatch(s):
        raise ParseError(f"Invalid duration '{text}'. Examples: 1d, 2h, 30m, 1w, 1d12h")
    return sum(int(n) * _UNIT_MIN[u] for n, u in DURATION_PART_RE.findall(s))


def format_offset(minutes: int) -> str:
    parts = []
    for unit, size in (("w", _UNIT_MIN["w"]), ("d", _UNIT_MIN["d"]), ("h", 60), ("m", 1)):
        if minutes >= size:
            parts.append(f"{minutes // size}{unit}")
            minutes %= size
    return "".join(parts) or "0m"


# ----------------------------------------------------------------- reminders
ReminderSpec = Union[tuple[str, datetime], tuple[str, int]]


def parse_reminder(text: str, now: datetime, tz: ZoneInfo, default_time: time) -> ReminderSpec:
    """Parse a reminder specification.

    Returns ("rel", minutes_before_deadline) or ("abs", aware_datetime).

    Relative to the deadline:  -1d, -2h, -30m, -1w, -1d12h
    Relative to now:           in 2h, +3d, in 1d
    Absolute:                  DD/MM/YYYY HH:MM, DD/MM/YYYY (at default time), HH:MM (next occurrence),
                               tomorrow 10:00, today 18:00
    """
    s = text.strip().lower()
    local_now = now.astimezone(tz)
    if s.startswith("-"):
        return ("rel", parse_duration_minutes(s[1:]))
    m = re.fullmatch(r"(?:in\s+|\+)(.+)", s)
    if m and DURATION_RE.fullmatch(m.group(1).strip()):
        return ("abs", local_now + timedelta(minutes=parse_duration_minutes(m.group(1))))
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        t = time(int(m.group(1)), int(m.group(2)))
        dt = datetime.combine(local_now.date(), t, tzinfo=tz)
        if dt <= local_now:
            dt += timedelta(days=1)
        return ("abs", dt)
    m = re.fullmatch(r"(.+?)\s+(\d{1,2}):(\d{2})", s)
    if m:
        d = parse_date(m.group(1), local_now.date())
        if d is None:
            raise ParseError("A reminder needs a date")
        return ("abs", datetime.combine(d, time(int(m.group(2)), int(m.group(3))), tzinfo=tz))
    d = parse_date(s, local_now.date())
    if d is None:
        raise ParseError("A reminder needs a date")
    return ("abs", datetime.combine(d, default_time, tzinfo=tz))


# --------------------------------------------------------------------- enums
_PRIORITY_ALIASES = {
    "critical": Priority.CRITICAL,
    "crit": Priority.CRITICAL,
    "high": Priority.HIGH,
    "medium": Priority.MEDIUM,
    "med": Priority.MEDIUM,
    "low": Priority.LOW,
    "undefined": Priority.UNDEFINED,
    "none": Priority.UNDEFINED,
}

_STATUS_ALIASES = {
    "to_start": Status.TO_START,
    "tostart": Status.TO_START,
    "todo": Status.TO_START,
    "new": Status.TO_START,
    "in_progress": Status.IN_PROGRESS,
    "inprogress": Status.IN_PROGRESS,
    "progress": Status.IN_PROGRESS,
    "doing": Status.IN_PROGRESS,
    "postponed": Status.POSTPONED,
    "canceled": Status.CANCELED,
    "cancelled": Status.CANCELED,
    "cancel": Status.CANCELED,
    "waiting_for_other": Status.WAITING_FOR_OTHER,
    "waiting": Status.WAITING_FOR_OTHER,
    "wait": Status.WAITING_FOR_OTHER,
    "completed": Status.COMPLETED,
    "complete": Status.COMPLETED,
    "done": Status.COMPLETED,
    "suspended": Status.SUSPENDED,
    "suspend": Status.SUSPENDED,
}


def _norm_enum_text(text: str) -> str:
    s = text.strip().lower().replace(" ", "_").replace("-", "_")
    m = re.fullmatch(r"(\d)(?:_.*)?", s)
    return m.group(1) if m else s


def parse_priority(text: str) -> Priority:
    s = _norm_enum_text(text)
    if s.isdigit():
        try:
            return Priority(int(s))
        except ValueError:
            pass
    elif s in _PRIORITY_ALIASES:
        return _PRIORITY_ALIASES[s]
    raise ParseError("Priority must be 0-Critical, 1-High, 2-Medium, 3-Low or 9-Undefined")


def parse_status(text: str) -> Status:
    s = _norm_enum_text(text)
    if s.isdigit():
        try:
            return Status(int(s))
        except ValueError:
            pass
    elif s in _STATUS_ALIASES:
        return _STATUS_ALIASES[s]
    raise ParseError(
        "Status must be 0-To_Start, 1-In_Progress, 2-Postponed, 3-Canceled, "
        "4-Waiting_for_Other, 5-Completed or 9-Suspended"
    )


# --------------------------------------------------------------------- links
def extract_urls(text: str) -> list[str]:
    return [u.rstrip(".,;:!?)]") for u in URL_RE.findall(text or "")]


def is_drive_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("drive.google.com", "docs.google.com")


def split_title_and_urls(text: str) -> tuple[str, list[str]]:
    urls = extract_urls(text)
    title = URL_RE.sub("", text or "")
    title = re.sub(r"\s+", " ", title).strip(" -–:\n\t")
    return title, urls


# ---------------------------------------------------------------------- tags
def normalize_tag(tag: str) -> str:
    t = tag.strip().lstrip("#").lower()
    t = re.sub(r"[^\w\-]", "", t)
    return t


def parse_tag_ops(text: str) -> tuple[list[str], list[str]]:
    """'+a -b c #d' -> (add=[a, c, d], remove=[b])."""
    add, remove = [], []
    for tok in text.replace(",", " ").split():
        if tok.startswith("-"):
            tag = normalize_tag(tok[1:])
            if tag:
                remove.append(tag)
        else:
            tag = normalize_tag(tok.lstrip("+"))
            if tag:
                add.append(tag)
    return add, remove


def extract_hashtags(text: str) -> list[str]:
    return [normalize_tag(t) for t in re.findall(r"(?<!\w)#(\w[\w\-]*)", text or "")]


# ----------------------------------------------------------- key=value pairs
def parse_kv(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse 'project="My proj" feature=API rest words' into ({...}, [rest])."""
    try:
        tokens = shlex.split(text or "")
    except ValueError as exc:
        raise ParseError(f"Could not parse arguments: {exc}") from exc
    kv, rest = {}, []
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            kv[k.strip().lower()] = v.strip()
        else:
            rest.append(tok)
    return kv, rest


def parse_task_id(text: str) -> int:
    s = text.strip().lstrip("#")
    if not s.isdigit():
        raise ParseError(f"'{text}' is not a valid task id")
    return int(s)
