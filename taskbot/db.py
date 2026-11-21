"""SQLite persistence layer."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Iterator, Optional, Sequence

from .models import Attachment, Link, Note, Priority, Reminder, Status, Task

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    telegram_user_id INTEGER
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_default INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS features (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (project_id, name)
);
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    priority INTEGER NOT NULL DEFAULT 9,
    status INTEGER NOT NULL DEFAULT 0,
    owner_id INTEGER REFERENCES owners(id),
    deadline TEXT,
    project_id INTEGER REFERENCES projects(id),
    feature_id INTEGER REFERENCES features(id) ON DELETE SET NULL,
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    recurrence TEXT,
    chat_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE TABLE IF NOT EXISTS task_tags (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (task_id, tag)
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    file_name TEXT,
    mime_type TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at TEXT,
    offset_minutes INTEGER,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    author TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    digest_enabled INTEGER NOT NULL DEFAULT 0,
    last_digest TEXT
);
"""

TASK_SELECT = """
SELECT t.*, o.name AS owner_name, p.name AS project_name, f.name AS feature_name,
       fo.name AS folder_name,
       (SELECT group_concat(tag, ' ') FROM (SELECT tag FROM task_tags WHERE task_id = t.id ORDER BY tag)) AS tag_list
FROM tasks t
LEFT JOIN owners o ON o.id = t.owner_id
LEFT JOIN projects p ON p.id = t.project_id
LEFT JOIN features f ON f.id = t.feature_id
LEFT JOIN folders fo ON fo.id = t.folder_id
"""

ORDER_BY_DEADLINE = " ORDER BY t.deadline IS NULL, t.deadline, t.priority, t.id"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _d(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


@dataclass
class TaskFilter:
    statuses: Optional[Sequence[Status]] = None
    priorities: Optional[Sequence[Priority]] = None
    deadline_from: Optional[date] = None
    deadline_to: Optional[date] = None
    has_deadline: Optional[bool] = None
    owner: Optional[str] = None
    project_id: Optional[int] = None
    feature_id: Optional[int] = None
    folder_id: Optional[int] = None
    tag: Optional[str] = None
    parent_id: Optional[int] = None
    top_level_only: bool = False
    text: Optional[str] = None
    limit: Optional[int] = None


class Database:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False, detect_types=0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ setup
    def seed(self, default_owner: str, default_project: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO owners(name) VALUES (?)", (default_owner,))
            c.execute("INSERT OR IGNORE INTO projects(name, is_default) VALUES (?, 1)", (default_project,))
            c.execute("UPDATE projects SET is_default = 1 WHERE name = ?", (default_project,))

    # ----------------------------------------------------------------- owners
    def list_owners(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM owners ORDER BY name").fetchall()

    def get_owner(self, name: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM owners WHERE name = ?", (name,)).fetchone()

    def add_owner(self, name: str, telegram_user_id: Optional[int] = None) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO owners(name, telegram_user_id) VALUES (?, ?)", (name, telegram_user_id))
            return cur.lastrowid

    def set_owner_user(self, name: str, telegram_user_id: Optional[int]) -> bool:
        with self.tx() as c:
            return c.execute(
                "UPDATE owners SET telegram_user_id = ? WHERE name = ?", (telegram_user_id, name)
            ).rowcount > 0

    def rename_owner(self, old: str, new: str) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE owners SET name = ? WHERE name = ?", (new, old)).rowcount > 0

    def owner_task_count(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM tasks t JOIN owners o ON o.id = t.owner_id WHERE o.name = ?", (name,)
        ).fetchone()
        return row[0]

    def delete_owner(self, name: str) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM owners WHERE name = ?", (name,)).rowcount > 0

    # --------------------------------------------------------------- projects
    def list_projects(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*, (SELECT count(*) FROM tasks t WHERE t.project_id = p.id
                              AND t.status NOT IN (3, 5)) AS open_tasks
               FROM projects p ORDER BY p.is_default DESC, p.name"""
        ).fetchall()

    def get_project(self, name: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()

    def get_project_by_id(self, project_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()

    def default_project(self) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM projects WHERE is_default = 1 ORDER BY id LIMIT 1").fetchone()

    def add_project(self, name: str) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO projects(name) VALUES (?)", (name,)).lastrowid

    def rename_project(self, old: str, new: str) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE projects SET name = ? WHERE name = ?", (new, old)).rowcount > 0

    def delete_project(self, project_id: int, fallback_project_id: int) -> None:
        """Delete a project, moving its tasks to the fallback project."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET project_id = ?, feature_id = NULL WHERE project_id = ?",
                (fallback_project_id, project_id),
            )
            c.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # --------------------------------------------------------------- features
    def list_features(self, project_id: Optional[int] = None) -> list[sqlite3.Row]:
        sql = """SELECT f.*, p.name AS project_name,
                   (SELECT count(*) FROM tasks t WHERE t.feature_id = f.id AND t.status NOT IN (3, 5)) AS open_tasks
                 FROM features f JOIN projects p ON p.id = f.project_id"""
        if project_id is not None:
            return self.conn.execute(sql + " WHERE f.project_id = ? ORDER BY f.name", (project_id,)).fetchall()
        return self.conn.execute(sql + " ORDER BY p.name, f.name").fetchall()

    def get_feature(self, project_id: int, name: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM features WHERE project_id = ? AND name = ?", (project_id, name)
        ).fetchone()

    def get_feature_by_id(self, feature_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM features WHERE id = ?", (feature_id,)).fetchone()

    def add_feature(self, project_id: int, name: str) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO features(project_id, name) VALUES (?, ?)", (project_id, name)).lastrowid

    def delete_feature(self, feature_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM features WHERE id = ?", (feature_id,))

    # ---------------------------------------------------------------- folders
    def list_folders(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT fo.*, (SELECT count(*) FROM tasks t WHERE t.folder_id = fo.id
                               AND t.status NOT IN (3, 5)) AS open_tasks
               FROM folders fo ORDER BY fo.name"""
        ).fetchall()

    def get_folder(self, name: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM folders WHERE name = ?", (name,)).fetchone()

    def add_folder(self, name: str) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO folders(name) VALUES (?)", (name,)).lastrowid

    def delete_folder(self, folder_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

    # ------------------------------------------------------------------ tasks
    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            priority=Priority(row["priority"]),
            status=Status(row["status"]),
            owner=row["owner_name"],
            deadline=_d(row["deadline"]),
            project_id=row["project_id"],
            project=row["project_name"],
            feature_id=row["feature_id"],
            feature=row["feature_name"],
            folder_id=row["folder_id"],
            folder=row["folder_name"],
            parent_id=row["parent_id"],
            recurrence=row["recurrence"],
            chat_id=row["chat_id"],
            created_by=row["created_by"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            completed_at=_dt(row["completed_at"]),
            tags=row["tag_list"].split() if row["tag_list"] else [],
        )

    def create_task(
        self,
        title: str,
        *,
        priority: Priority = Priority.UNDEFINED,
        status: Status = Status.TO_START,
        owner_id: Optional[int] = None,
        deadline: Optional[date] = None,
        project_id: Optional[int] = None,
        feature_id: Optional[int] = None,
        folder_id: Optional[int] = None,
        parent_id: Optional[int] = None,
        description: Optional[str] = None,
        recurrence: Optional[str] = None,
        chat_id: Optional[int] = None,
        created_by: Optional[int] = None,
    ) -> int:
        now = utcnow().isoformat()
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO tasks (title, description, priority, status, owner_id, deadline, project_id,
                                      feature_id, folder_id, parent_id, recurrence, chat_id, created_by,
                                      created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    title,
                    description,
                    int(priority),
                    int(status),
                    owner_id,
                    deadline.isoformat() if deadline else None,
                    project_id,
                    feature_id,
                    folder_id,
                    parent_id,
                    recurrence,
                    chat_id,
                    created_by,
                    now,
                    now,
                ),
            )
            return cur.lastrowid

    def get_task(self, task_id: int) -> Optional[Task]:
        row = self.conn.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    _UPDATABLE = {
        "title",
        "description",
        "priority",
        "status",
        "owner_id",
        "deadline",
        "project_id",
        "feature_id",
        "folder_id",
        "parent_id",
        "recurrence",
        "completed_at",
    }

    def update_task(self, task_id: int, **fields) -> None:
        bad = set(fields) - self._UPDATABLE
        if bad:
            raise ValueError(f"Cannot update fields: {bad}")
        values = []
        for value in fields.values():
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, date):
                value = value.isoformat()
            elif isinstance(value, (Priority, Status)):
                value = int(value)
            values.append(value)
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(
                f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?",
                (*values, utcnow().isoformat(), task_id),
            )

    def delete_task(self, task_id: int) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount > 0

    def find_tasks(self, flt: Optional[TaskFilter] = None) -> list[Task]:
        flt = flt or TaskFilter()
        where, params = [], []
        if flt.statuses is not None:
            where.append(f"t.status IN ({','.join('?' * len(flt.statuses))})")
            params += [int(s) for s in flt.statuses]
        if flt.priorities is not None:
            where.append(f"t.priority IN ({','.join('?' * len(flt.priorities))})")
            params += [int(p) for p in flt.priorities]
        if flt.deadline_from is not None:
            where.append("t.deadline >= ?")
            params.append(flt.deadline_from.isoformat())
        if flt.deadline_to is not None:
            where.append("t.deadline <= ?")
            params.append(flt.deadline_to.isoformat())
        if flt.has_deadline is True:
            where.append("t.deadline IS NOT NULL")
        elif flt.has_deadline is False:
            where.append("t.deadline IS NULL")
        if flt.owner is not None:
            where.append("o.name = ? COLLATE NOCASE")
            params.append(flt.owner)
        if flt.project_id is not None:
            where.append("t.project_id = ?")
            params.append(flt.project_id)
        if flt.feature_id is not None:
            where.append("t.feature_id = ?")
            params.append(flt.feature_id)
        if flt.folder_id is not None:
            where.append("t.folder_id = ?")
            params.append(flt.folder_id)
        if flt.tag is not None:
            where.append("EXISTS (SELECT 1 FROM task_tags tt WHERE tt.task_id = t.id AND tt.tag = ?)")
            params.append(flt.tag)
        if flt.parent_id is not None:
            where.append("t.parent_id = ?")
            params.append(flt.parent_id)
        elif flt.top_level_only:
            where.append("t.parent_id IS NULL")
        if flt.text:
            where.append(
                "(t.title LIKE ? OR t.description LIKE ? OR EXISTS "
                "(SELECT 1 FROM notes n WHERE n.task_id = t.id AND n.text LIKE ?))"
            )
            like = f"%{flt.text}%"
            params += [like, like, like]
        sql = TASK_SELECT
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += ORDER_BY_DEADLINE
        if flt.limit:
            sql += f" LIMIT {int(flt.limit)}"
        return [self._row_to_task(r) for r in self.conn.execute(sql, params).fetchall()]

    def subtasks(self, task_id: int) -> list[Task]:
        return self.find_tasks(TaskFilter(parent_id=task_id))

    def deadline_counts(self, start: date, end: date, statuses: Sequence[Status]) -> dict[date, int]:
        rows = self.conn.execute(
            f"""SELECT deadline, count(*) FROM tasks WHERE deadline BETWEEN ? AND ?
                AND status IN ({','.join('?' * len(statuses))}) GROUP BY deadline""",
            (start.isoformat(), end.isoformat(), *[int(s) for s in statuses]),
        ).fetchall()
        return {date.fromisoformat(r[0]): r[1] for r in rows}

    # ------------------------------------------------------------------- tags
    def set_tags(self, task_id: int, tags: Iterable[str]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
            c.executemany(
                "INSERT OR IGNORE INTO task_tags(task_id, tag) VALUES (?, ?)", [(task_id, t) for t in tags]
            )

    def add_tags(self, task_id: int, tags: Iterable[str]) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT OR IGNORE INTO task_tags(task_id, tag) VALUES (?, ?)", [(task_id, t) for t in tags]
            )

    def remove_tags(self, task_id: int, tags: Iterable[str]) -> None:
        with self.tx() as c:
            c.executemany("DELETE FROM task_tags WHERE task_id = ? AND tag = ?", [(task_id, t) for t in tags])

    def all_tags(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT tt.tag, count(*) AS n FROM task_tags tt JOIN tasks t ON t.id = tt.task_id
               WHERE t.status NOT IN (3, 5) GROUP BY tt.tag ORDER BY tt.tag"""
        ).fetchall()

    # ------------------------------------------------------------------ links
    def add_link(self, task_id: int, url: str, kind: str, title: Optional[str] = None) -> int:
        with self.tx() as c:
            return c.execute(
                "INSERT INTO links(task_id, url, kind, title) VALUES (?, ?, ?, ?)", (task_id, url, kind, title)
            ).lastrowid

    def links(self, task_id: int) -> list[Link]:
        rows = self.conn.execute("SELECT * FROM links WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [Link(id=r["id"], task_id=r["task_id"], url=r["url"], kind=r["kind"], title=r["title"]) for r in rows]

    def delete_link(self, task_id: int, link_id: int) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM links WHERE id = ? AND task_id = ?", (link_id, task_id)).rowcount > 0

    # ------------------------------------------------------------ attachments
    def add_attachment(
        self, task_id: int, file_id: str, kind: str, file_name: Optional[str] = None, mime_type: Optional[str] = None
    ) -> int:
        with self.tx() as c:
            return c.execute(
                """INSERT INTO attachments(task_id, file_id, kind, file_name, mime_type, added_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (task_id, file_id, kind, file_name, mime_type, utcnow().isoformat()),
            ).lastrowid

    def attachments(self, task_id: int) -> list[Attachment]:
        rows = self.conn.execute("SELECT * FROM attachments WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [
            Attachment(
                id=r["id"],
                task_id=r["task_id"],
                file_id=r["file_id"],
                kind=r["kind"],
                file_name=r["file_name"],
                mime_type=r["mime_type"],
                added_at=_dt(r["added_at"]),
            )
            for r in rows
        ]

    def delete_attachment(self, task_id: int, attachment_id: int) -> bool:
        with self.tx() as c:
            return (
                c.execute("DELETE FROM attachments WHERE id = ? AND task_id = ?", (attachment_id, task_id)).rowcount
                > 0
            )

    # -------------------------------------------------------------- reminders
    def add_reminder(self, task_id: int, at: Optional[datetime] = None, offset_minutes: Optional[int] = None) -> int:
        if (at is None) == (offset_minutes is None):
            raise ValueError("Exactly one of 'at' or 'offset_minutes' must be given")
        with self.tx() as c:
            return c.execute(
                "INSERT INTO reminders(task_id, at, offset_minutes) VALUES (?, ?, ?)",
                (task_id, at.astimezone(timezone.utc).isoformat() if at else None, offset_minutes),
            ).lastrowid

    def _row_to_reminder(self, r: sqlite3.Row) -> Reminder:
        return Reminder(
            id=r["id"],
            task_id=r["task_id"],
            at=_dt(r["at"]),
            offset_minutes=r["offset_minutes"],
            sent_at=_dt(r["sent_at"]),
        )

    def reminders(self, task_id: int) -> list[Reminder]:
        rows = self.conn.execute("SELECT * FROM reminders WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [self._row_to_reminder(r) for r in rows]

    def pending_reminders(self) -> list[Reminder]:
        rows = self.conn.execute(
            """SELECT r.* FROM reminders r JOIN tasks t ON t.id = r.task_id
               WHERE r.sent_at IS NULL AND t.status NOT IN (3, 5)"""
        ).fetchall()
        return [self._row_to_reminder(r) for r in rows]

    def mark_reminder_sent(self, reminder_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE reminders SET sent_at = ? WHERE id = ?", (utcnow().isoformat(), reminder_id))

    def reset_relative_reminders(self, task_id: int) -> None:
        """Re-arm relative reminders (e.g. after the deadline changed)."""
        with self.tx() as c:
            c.execute(
                "UPDATE reminders SET sent_at = NULL WHERE task_id = ? AND offset_minutes IS NOT NULL", (task_id,)
            )

    def delete_reminder(self, task_id: int, reminder_id: int) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM reminders WHERE id = ? AND task_id = ?", (reminder_id, task_id)).rowcount > 0

    # ------------------------------------------------------------------ notes
    def add_note(self, task_id: int, text: str, author: Optional[str]) -> int:
        with self.tx() as c:
            return c.execute(
                "INSERT INTO notes(task_id, text, author, created_at) VALUES (?, ?, ?, ?)",
                (task_id, text, author, utcnow().isoformat()),
            ).lastrowid

    def notes(self, task_id: int) -> list[Note]:
        rows = self.conn.execute("SELECT * FROM notes WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [
            Note(id=r["id"], task_id=r["task_id"], text=r["text"], author=r["author"], created_at=_dt(r["created_at"]))
            for r in rows
        ]

    # ---------------------------------------------------------------- history
    def log(self, task_id: int, actor: Optional[str], action: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO history(task_id, at, actor, action) VALUES (?, ?, ?, ?)",
                (task_id, utcnow().isoformat(), actor, action),
            )

    def history(self, task_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM history WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()

    # ------------------------------------------------------------------ chats
    def set_digest(self, chat_id: int, enabled: bool) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO chats(chat_id, digest_enabled) VALUES (?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET digest_enabled = excluded.digest_enabled""",
                (chat_id, int(enabled)),
            )

    def digest_chats(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM chats WHERE digest_enabled = 1").fetchall()

    def mark_digest_sent(self, chat_id: int, day: date) -> None:
        with self.tx() as c:
            c.execute("UPDATE chats SET last_digest = ? WHERE chat_id = ?", (day.isoformat(), chat_id))
