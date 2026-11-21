"""Business logic, independent of Telegram."""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .config import Config
from .db import Database, TaskFilter
from .models import (
    CLOSED_STATUSES,
    HIGH_PRIORITIES,
    OPEN_STATUSES,
    Priority,
    Reminder,
    Status,
    Task,
)
from .parsing import (
    ParseError,
    format_date,
    format_offset,
    is_drive_url,
    next_occurrence,
    normalize_tag,
    parse_date,
    parse_priority,
    parse_recurrence,
    parse_reminder,
    parse_status,
)

log = logging.getLogger(__name__)

EDITABLE_FIELDS = (
    "title",
    "description",
    "priority",
    "status",
    "owner",
    "deadline",
    "project",
    "feature",
    "folder",
    "tags",
    "recurrence",
    "parent",
)

QUADRANTS = {
    "Q1": ("🔥 Do first", "Urgent & Important"),
    "Q2": ("📅 Schedule", "Important, not urgent"),
    "Q3": ("🤝 Delegate", "Urgent, not important"),
    "Q4": ("🗑 Eliminate", "Neither urgent nor important"),
}


class ServiceError(Exception):
    """A user-facing error."""


@dataclass
class Draft:
    """A task that has not been created yet (waiting for confirmation)."""

    title: str
    priority: int
    owner: str
    deadline: Optional[str]  # ISO date
    status: int
    project: str
    feature: Optional[str] = None
    folder: Optional[str] = None
    description: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    attachment: Optional[dict] = None
    parent_id: Optional[int] = None

    @property
    def deadline_date(self) -> Optional[date]:
        return date.fromisoformat(self.deadline) if self.deadline else None

    def to_dict(self) -> dict:
        return asdict(self)


class TaskService:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.cfg = config
        db.seed(config.default_owner, config.default_project)

    # ---------------------------------------------------------------- time
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return self.now().astimezone(self.cfg.timezone).date()

    # --------------------------------------------------------------- drafts
    def new_draft(
        self,
        title: str,
        urls: Optional[list[str]] = None,
        attachment: Optional[dict] = None,
        tags: Optional[list[str]] = None,
        parent_id: Optional[int] = None,
    ) -> Draft:
        project = self.cfg.default_project
        feature = None
        if parent_id is not None:
            parent = self.require_task(parent_id)
            project = parent.project or project
            feature = parent.feature
        return Draft(
            title=(title or "Untitled task").strip()[:500],
            priority=int(Priority.UNDEFINED),
            owner=self.cfg.default_owner,
            deadline=(self.today() + timedelta(days=self.cfg.default_deadline_days)).isoformat(),
            status=int(Status.TO_START),
            project=project,
            feature=feature,
            tags=list(dict.fromkeys(tags or [])),
            urls=list(dict.fromkeys(urls or [])),
            attachment=attachment,
            parent_id=parent_id,
        )

    def set_draft_field(self, draft: Draft, name: str, value: str) -> None:
        """Validate and set a draft field from user text."""
        if name == "title":
            if not value.strip():
                raise ServiceError("Title cannot be empty")
            draft.title = value.strip()[:500]
        elif name == "description":
            draft.description = value.strip() or None
        elif name == "priority":
            draft.priority = int(self._parse(parse_priority, value))
        elif name == "status":
            draft.status = int(self._parse(parse_status, value))
        elif name == "owner":
            draft.owner = self._require_owner(value)["name"]
        elif name == "deadline":
            d = self._parse(parse_date, value, self.today())
            draft.deadline = d.isoformat() if d else None
        elif name == "project":
            proj = self._require_project(value)
            if proj["name"] != draft.project:
                draft.feature = None
            draft.project = proj["name"]
        elif name == "feature":
            if value.strip().lower() in ("", "none", "-"):
                draft.feature = None
            else:
                proj = self._require_project(draft.project)
                draft.feature = self._require_feature(proj, value)["name"]
        elif name == "folder":
            draft.folder = None if value.strip().lower() in ("", "none", "-") else self._require_folder(value)["name"]
        elif name == "tags":
            draft.tags = [t for t in (normalize_tag(x) for x in value.replace(",", " ").split()) if t]
        else:
            raise ServiceError(f"Field '{name}' cannot be set on a new task")

    def create_from_draft(self, draft: Draft, chat_id: Optional[int], user_id: Optional[int], actor: str) -> Task:
        owner = self._require_owner(draft.owner)
        project = self._require_project(draft.project)
        feature = self._require_feature(project, draft.feature) if draft.feature else None
        folder = self._require_folder(draft.folder) if draft.folder else None
        task_id = self.db.create_task(
            draft.title,
            priority=Priority(draft.priority),
            status=Status(draft.status),
            owner_id=owner["id"],
            deadline=draft.deadline_date,
            project_id=project["id"],
            feature_id=feature["id"] if feature else None,
            folder_id=folder["id"] if folder else None,
            parent_id=draft.parent_id,
            description=draft.description,
            chat_id=chat_id,
            created_by=user_id,
        )
        if draft.tags:
            self.db.set_tags(task_id, draft.tags)
        for i, url in enumerate(draft.urls):
            kind = "drive" if is_drive_url(url) else ("source" if i == 0 else "other")
            self.db.add_link(task_id, url, kind)
        if draft.attachment:
            a = draft.attachment
            self.db.add_attachment(task_id, a["file_id"], a["kind"], a.get("file_name"), a.get("mime_type"))
        self.db.log(task_id, actor, "created")
        return self.require_task(task_id)

    # --------------------------------------------------------------- lookups
    def require_task(self, task_id: int) -> Task:
        task = self.db.get_task(task_id)
        if not task:
            raise ServiceError(f"Task #{task_id} not found")
        return task

    def _require_owner(self, name: str) -> sqlite3.Row:
        row = self.db.get_owner(name.strip())
        if not row:
            names = ", ".join(o["name"] for o in self.db.list_owners())
            raise ServiceError(f"Unknown owner '{name}'. Known owners: {names}. Add one with /owner add <name>")
        return row

    def _require_project(self, name: str) -> sqlite3.Row:
        row = self.db.get_project(name.strip())
        if not row:
            raise ServiceError(f"Unknown project '{name}'. Create it with /project add <name>")
        return row

    def _require_feature(self, project: sqlite3.Row, name: str) -> sqlite3.Row:
        row = self.db.get_feature(project["id"], name.strip())
        if not row:
            raise ServiceError(
                f"Feature '{name}' not found in project '{project['name']}'. "
                f"Create it with /feature add {project['name']} | {name}"
            )
        return row

    def _require_folder(self, name: str) -> sqlite3.Row:
        row = self.db.get_folder(name.strip())
        if not row:
            raise ServiceError(f"Unknown folder '{name}'. Create it with /folder add <name>")
        return row

    @staticmethod
    def _parse(fn, *args):
        try:
            return fn(*args)
        except ParseError as exc:
            raise ServiceError(str(exc)) from exc

    # --------------------------------------------------------------- editing
    def set_field(self, task_id: int, name: str, value: str, actor: str) -> tuple[Task, list[Task]]:
        """Set a field from user text. Returns (task, newly spawned recurring tasks)."""
        task = self.require_task(task_id)
        spawned: list[Task] = []
        name = name.lower()
        if name == "title":
            if not value.strip():
                raise ServiceError("Title cannot be empty")
            self.db.update_task(task_id, title=value.strip()[:500])
        elif name in ("description", "desc"):
            name = "description"
            self.db.update_task(task_id, description=value.strip() or None)
        elif name == "priority":
            self.db.update_task(task_id, priority=self._parse(parse_priority, value))
        elif name == "status":
            spawned = self.set_status(task_id, self._parse(parse_status, value), actor, log=False)
        elif name == "owner":
            self.db.update_task(task_id, owner_id=self._require_owner(value)["id"])
        elif name == "deadline":
            d = self._parse(parse_date, value, self.today())
            self.db.update_task(task_id, deadline=d)
            self.db.reset_relative_reminders(task_id)
        elif name == "project":
            self.move(task_id, value, None, actor, log=False)
        elif name == "feature":
            project = task.project or self.cfg.default_project
            feature = None if value.strip().lower() in ("", "none", "-") else value
            self.move(task_id, project, feature, actor, log=False)
        elif name == "folder":
            folder = None if value.strip().lower() in ("", "none", "-") else self._require_folder(value)["id"]
            self.db.update_task(task_id, folder_id=folder)
        elif name == "tags":
            self.db.set_tags(task_id, [t for t in (normalize_tag(x) for x in value.replace(",", " ").split()) if t])
        elif name in ("recurrence", "repeat"):
            name = "recurrence"
            rec = self._parse(parse_recurrence, value)
            if rec and not task.deadline:
                raise ServiceError("A recurring task needs a deadline")
            self.db.update_task(task_id, recurrence=rec)
        elif name == "parent":
            self.set_parent(task_id, value, actor, log=False)
        else:
            raise ServiceError(f"Unknown field '{name}'. Editable: {', '.join(EDITABLE_FIELDS)}")
        self.db.log(task_id, actor, f"{name} → {value.strip() or '(empty)'}")
        return self.require_task(task_id), spawned

    def set_status(self, task_id: int, status: Status, actor: str, log: bool = True) -> list[Task]:
        task = self.require_task(task_id)
        completed_at = self.now() if status == Status.COMPLETED else None
        self.db.update_task(task_id, status=status, completed_at=completed_at)
        if log:
            self.db.log(task_id, actor, f"status → {status.label}")
        spawned = []
        if status == Status.COMPLETED and task.status != Status.COMPLETED and task.recurrence and task.deadline:
            spawned.append(self._spawn_next_occurrence(task, actor))
        return spawned

    def _spawn_next_occurrence(self, task: Task, actor: str) -> Task:
        new_deadline = next_occurrence(task.deadline, task.recurrence)
        today = self.today()
        while new_deadline < today:
            new_deadline = next_occurrence(new_deadline, task.recurrence)
        owner = self.db.get_owner(task.owner) if task.owner else None
        new_id = self.db.create_task(
            task.title,
            priority=task.priority,
            status=Status.TO_START,
            owner_id=owner["id"] if owner else None,
            deadline=new_deadline,
            project_id=task.project_id,
            feature_id=task.feature_id,
            folder_id=task.folder_id,
            parent_id=task.parent_id,
            description=task.description,
            recurrence=task.recurrence,
            chat_id=task.chat_id,
            created_by=task.created_by,
        )
        self.db.set_tags(new_id, task.tags)
        for link in self.db.links(task.id):
            self.db.add_link(new_id, link.url, link.kind, link.title)
        for rem in self.db.reminders(task.id):
            if rem.is_relative:
                self.db.add_reminder(new_id, offset_minutes=rem.offset_minutes)
        # the recurrence moves to the new occurrence
        self.db.update_task(task.id, recurrence=None)
        self.db.log(new_id, actor, f"created as next occurrence of #{task.id}")
        return self.require_task(new_id)

    def move(
        self, task_id: int, project_name: Optional[str], feature_name: Optional[str], actor: str, log: bool = True
    ) -> Task:
        """Move a task (and its subtasks) under another project and/or feature."""
        task = self.require_task(task_id)
        project = self._require_project(project_name) if project_name else self._require_project(
            task.project or self.cfg.default_project
        )
        feature = self._require_feature(project, feature_name) if feature_name else None
        ids = [task_id] + [t.id for t in self._descendants(task_id)]
        for tid in ids:
            self.db.update_task(tid, project_id=project["id"], feature_id=feature["id"] if feature else None)
        if log:
            dest = project["name"] + (f" / {feature['name']}" if feature else "")
            self.db.log(task_id, actor, f"moved to {dest}")
        return self.require_task(task_id)

    def set_parent(self, task_id: int, value: str, actor: str, log: bool = True) -> Task:
        task = self.require_task(task_id)
        v = value.strip().lstrip("#").lower()
        if v in ("", "none", "-", "0"):
            self.db.update_task(task_id, parent_id=None)
        else:
            if not v.isdigit():
                raise ServiceError("Parent must be a task id or 'none'")
            parent_id = int(v)
            if parent_id == task_id or parent_id in {t.id for t in self._descendants(task_id)}:
                raise ServiceError("A task cannot be a subtask of itself or of its own subtasks")
            parent = self.require_task(parent_id)
            self.db.update_task(task_id, parent_id=parent_id)
            if parent.project_id != task.project_id or parent.feature_id != task.feature_id:
                self.move(task_id, parent.project, parent.feature, actor, log=False)
        if log:
            self.db.log(task_id, actor, f"parent → {value}")
        return self.require_task(task_id)

    def _descendants(self, task_id: int) -> list[Task]:
        out, stack = [], [task_id]
        while stack:
            for child in self.db.subtasks(stack.pop()):
                out.append(child)
                stack.append(child.id)
        return out

    def edit_tags(self, task_id: int, add: list[str], remove: list[str], actor: str) -> Task:
        self.require_task(task_id)
        if add:
            self.db.add_tags(task_id, add)
        if remove:
            self.db.remove_tags(task_id, remove)
        parts = [f"+{t}" for t in add] + [f"-{t}" for t in remove]
        self.db.log(task_id, actor, "tags " + " ".join(parts))
        return self.require_task(task_id)

    def add_drive_link(self, task_id: int, url: str, actor: str, title: Optional[str] = None) -> int:
        self.require_task(task_id)
        if not is_drive_url(url):
            raise ServiceError("This is not a Google Drive/Docs link (drive.google.com or docs.google.com)")
        link_id = self.db.add_link(task_id, url, "drive", title)
        self.db.log(task_id, actor, "Drive link added")
        return link_id

    def add_link(self, task_id: int, url: str, actor: str) -> int:
        self.require_task(task_id)
        link_id = self.db.add_link(task_id, url, "drive" if is_drive_url(url) else "other")
        self.db.log(task_id, actor, "link added")
        return link_id

    def add_attachment(self, task_id: int, attachment: dict, actor: str) -> int:
        self.require_task(task_id)
        att_id = self.db.add_attachment(
            task_id, attachment["file_id"], attachment["kind"], attachment.get("file_name"), attachment.get("mime_type")
        )
        self.db.log(task_id, actor, f"attachment added: {attachment.get('file_name') or attachment['kind']}")
        return att_id

    def add_subtask(self, parent_id: int, title: str, chat_id: Optional[int], user_id: Optional[int], actor: str):
        draft = self.new_draft(title, parent_id=parent_id)
        parent = self.require_task(parent_id)
        draft.owner = parent.owner or draft.owner
        draft.deadline = parent.deadline.isoformat() if parent.deadline else draft.deadline
        return self.create_from_draft(draft, chat_id, user_id, actor)

    def delete_task(self, task_id: int) -> None:
        self.require_task(task_id)
        self.db.delete_task(task_id)

    # ------------------------------------------------------------- reminders
    def add_reminder(self, task_id: int, spec: str, actor: str) -> Reminder:
        task = self.require_task(task_id)
        kind, value = self._parse(parse_reminder, spec, self.now(), self.cfg.timezone, self.cfg.deadline_time)
        if kind == "rel":
            if not task.deadline:
                raise ServiceError("Relative reminders (e.g. -1d) need a deadline. Set one or use an absolute date.")
            rid = self.db.add_reminder(task_id, offset_minutes=value)
        else:
            if value <= self.now():
                raise ServiceError("That reminder time is in the past")
            rid = self.db.add_reminder(task_id, at=value)
        rem = next(r for r in self.db.reminders(task_id) if r.id == rid)
        self.db.log(task_id, actor, f"reminder added: {self.describe_reminder(rem, task)}")
        return rem

    def reminder_fire_time(self, rem: Reminder, task: Task) -> Optional[datetime]:
        if rem.at:
            return rem.at
        if task.deadline is None:
            return None
        due = datetime.combine(task.deadline, self.cfg.deadline_time, tzinfo=self.cfg.timezone)
        return due - timedelta(minutes=rem.offset_minutes)

    def describe_reminder(self, rem: Reminder, task: Task) -> str:
        fire = self.reminder_fire_time(rem, task)
        when = fire.astimezone(self.cfg.timezone).strftime("%d/%m/%Y %H:%M") if fire else "no deadline"
        if rem.is_relative:
            return f"{format_offset(rem.offset_minutes)} before deadline ({when})"
        return when

    def due_reminders(self) -> list[tuple[Reminder, Task]]:
        now = self.now()
        due = []
        for rem in self.db.pending_reminders():
            task = self.db.get_task(rem.task_id)
            if not task:
                continue
            fire = self.reminder_fire_time(rem, task)
            if fire is not None and fire <= now:
                due.append((rem, task))
        return due

    def reminder_recipients(self, task: Task) -> set[int]:
        recipients = set()
        if task.chat_id:
            recipients.add(task.chat_id)
        if task.owner:
            owner = self.db.get_owner(task.owner)
            if owner and owner["telegram_user_id"]:
                recipients.add(owner["telegram_user_id"])
        return recipients

    # ----------------------------------------------------------------- views
    def week_range(self, offset_weeks: int = 0) -> tuple[date, date]:
        today = self.today()
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset_weeks)
        return start, start + timedelta(days=6)

    def tasks_between(self, start: date, end: date, include_closed: bool = False) -> list[Task]:
        statuses = None if include_closed else list(OPEN_STATUSES)
        return self.db.find_tasks(TaskFilter(statuses=statuses, deadline_from=start, deadline_to=end))

    def overdue(self) -> list[Task]:
        return self.db.find_tasks(
            TaskFilter(statuses=list(OPEN_STATUSES), deadline_to=self.today() - timedelta(days=1))
        )

    def high_priority_open(self) -> list[Task]:
        """Point 9: priority 0/1 with status in 0,1,2,4."""
        return self.db.find_tasks(TaskFilter(priorities=list(HIGH_PRIORITIES), statuses=list(OPEN_STATUSES)))

    def is_urgent(self, task: Task) -> bool:
        return task.deadline is not None and task.deadline <= self.today() + timedelta(days=self.cfg.urgent_days)

    @staticmethod
    def is_important(task: Task) -> bool:
        return task.priority in HIGH_PRIORITIES

    def eisenhower(self) -> dict[str, list[Task]]:
        matrix: dict[str, list[Task]] = {q: [] for q in QUADRANTS}
        for task in self.db.find_tasks(TaskFilter(statuses=list(OPEN_STATUSES))):
            urgent, important = self.is_urgent(task), self.is_important(task)
            q = "Q1" if urgent and important else "Q2" if important else "Q3" if urgent else "Q4"
            matrix[q].append(task)
        return matrix

    def month_counts(self, year: int, month: int) -> dict[date, int]:
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
        return self.db.deadline_counts(start, end, list(OPEN_STATUSES))

    def stats(self) -> dict:
        all_tasks = self.db.find_tasks()
        today = self.today()
        by_status = {s: 0 for s in Status}
        for t in all_tasks:
            by_status[t.status] += 1
        week_start, week_end = self.week_range()
        return {
            "total": len(all_tasks),
            "by_status": by_status,
            "overdue": sum(1 for t in all_tasks if t.status in OPEN_STATUSES and t.deadline and t.deadline < today),
            "completed_this_week": sum(
                1
                for t in all_tasks
                if t.status == Status.COMPLETED
                and t.completed_at
                and week_start <= t.completed_at.astimezone(self.cfg.timezone).date() <= week_end
            ),
        }

    # ---------------------------------------------------------------- export
    def export_csv(self) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        writer.writerow(
            ["id", "title", "priority", "status", "owner", "deadline", "project", "feature", "folder", "parent",
             "tags", "recurrence", "links", "created_at", "completed_at"]
        )
        for t in self.db.find_tasks():
            writer.writerow(
                [
                    t.id,
                    t.title,
                    t.priority.label,
                    t.status.label,
                    t.owner or "",
                    format_date(t.deadline) if t.deadline else "",
                    t.project or "",
                    t.feature or "",
                    t.folder or "",
                    t.parent_id or "",
                    " ".join(t.tags),
                    t.recurrence or "",
                    " ".join(link.url for link in self.db.links(t.id)),
                    t.created_at.astimezone(self.cfg.timezone).strftime("%d/%m/%Y %H:%M") if t.created_at else "",
                    t.completed_at.astimezone(self.cfg.timezone).strftime("%d/%m/%Y %H:%M") if t.completed_at else "",
                ]
            )
        return buf.getvalue().encode("utf-8-sig")


__all__ = ["TaskService", "Draft", "ServiceError", "QUADRANTS", "EDITABLE_FIELDS", "CLOSED_STATUSES"]
