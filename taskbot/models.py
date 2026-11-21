"""Domain enums and dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum
from typing import Optional


class Priority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    MEDIUM = 2
    LOW = 3
    UNDEFINED = 9

    @property
    def label(self) -> str:
        return f"{self.value}-{self.name.capitalize()}"

    @property
    def icon(self) -> str:
        return {0: "🔴", 1: "🟠", 2: "🟡", 3: "🟢", 9: "⚪"}[self.value]


class Status(IntEnum):
    TO_START = 0
    IN_PROGRESS = 1
    POSTPONED = 2
    CANCELED = 3
    WAITING_FOR_OTHER = 4
    COMPLETED = 5
    SUSPENDED = 9

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self.value]

    @property
    def icon(self) -> str:
        return {0: "⏳", 1: "▶️", 2: "⏸", 3: "✖️", 4: "👥", 5: "✅", 9: "💤"}[self.value]


_STATUS_LABELS = {
    0: "0-To_Start",
    1: "1-In_Progress",
    2: "2-Postponed",
    3: "3-Canceled",
    4: "4-Waiting_for_Other",
    5: "5-Completed",
    9: "9-Suspended",
}

# Statuses considered "open" (not completed / canceled / suspended).
OPEN_STATUSES = (Status.TO_START, Status.IN_PROGRESS, Status.POSTPONED, Status.WAITING_FOR_OTHER)
# Statuses that stop reminders and keep tasks out of active views.
CLOSED_STATUSES = (Status.COMPLETED, Status.CANCELED)
HIGH_PRIORITIES = (Priority.CRITICAL, Priority.HIGH)

RECURRENCES = ("daily", "weekly", "monthly", "yearly")


@dataclass
class Task:
    id: int
    title: str
    priority: Priority = Priority.UNDEFINED
    status: Status = Status.TO_START
    owner: Optional[str] = None
    deadline: Optional[date] = None
    project_id: Optional[int] = None
    project: Optional[str] = None
    feature_id: Optional[int] = None
    feature: Optional[str] = None
    folder_id: Optional[int] = None
    folder: Optional[str] = None
    parent_id: Optional[int] = None
    description: Optional[str] = None
    recurrence: Optional[str] = None
    chat_id: Optional[int] = None
    created_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    tags: list[str] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES


@dataclass
class Link:
    id: int
    task_id: int
    url: str
    kind: str  # source | drive | other
    title: Optional[str] = None


@dataclass
class Attachment:
    id: int
    task_id: int
    file_id: str
    kind: str  # document | photo | video | audio | voice
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    added_at: Optional[datetime] = None


@dataclass
class Reminder:
    id: int
    task_id: int
    at: Optional[datetime] = None  # absolute fire time
    offset_minutes: Optional[int] = None  # minutes BEFORE the deadline (relative)
    sent_at: Optional[datetime] = None

    @property
    def is_relative(self) -> bool:
        return self.offset_minutes is not None


@dataclass
class Note:
    id: int
    task_id: int
    text: str
    author: Optional[str]
    created_at: datetime
