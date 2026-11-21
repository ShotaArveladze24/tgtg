"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo


def _parse_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(";", ",").split(",") if x.strip()}


def _parse_time(raw: str) -> time:
    h, m = raw.strip().split(":")
    return time(int(h), int(m))


@dataclass
class Config:
    token: str = ""
    allowed_users: set[int] = field(default_factory=set)
    db_path: str = "taskbot.db"
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Rome"))
    default_owner: str = "PNC"
    default_project: str = "No PROJECT"
    default_deadline_days: int = 7
    urgent_days: int = 3
    deadline_time: time = time(9, 0)
    digest_time: time = time(8, 0)
    reminder_interval: int = 60
    fetch_link_titles: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        return cls(
            token=env.get("TASKBOT_TOKEN", ""),
            allowed_users=_parse_ids(env.get("TASKBOT_ALLOWED_USERS", "")),
            db_path=env.get("TASKBOT_DB", "taskbot.db"),
            timezone=ZoneInfo(env.get("TASKBOT_TIMEZONE", "Europe/Rome")),
            default_owner=env.get("TASKBOT_DEFAULT_OWNER", "PNC"),
            default_project=env.get("TASKBOT_DEFAULT_PROJECT", "No PROJECT"),
            default_deadline_days=int(env.get("TASKBOT_DEFAULT_DEADLINE_DAYS", "7")),
            urgent_days=int(env.get("TASKBOT_URGENT_DAYS", "3")),
            deadline_time=_parse_time(env.get("TASKBOT_DEADLINE_TIME", "09:00")),
            digest_time=_parse_time(env.get("TASKBOT_DIGEST_TIME", "08:00")),
            reminder_interval=int(env.get("TASKBOT_REMINDER_INTERVAL", "60")),
            fetch_link_titles=env.get("TASKBOT_FETCH_LINK_TITLES", "1").lower() in ("1", "true", "yes"),
        )
