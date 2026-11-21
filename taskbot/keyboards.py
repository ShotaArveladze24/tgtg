"""Inline keyboards and callback-data conventions.

Callback data (max 64 bytes):
  t:<id>                      show task card
  tm:<id>                     task edit menu
  tf:<id>:<field>             edit a task field (choice list or text prompt)
  tv:<id>:<field>:<value>     set a task field to a chosen value
  ta:<id>:<action>            task action (files, notes, history, delete, delok, sub, remind, link)
  dc:<did> / dx:<did>         confirm / cancel a draft
  de:<did>                    draft edit menu
  df:<did>:<field>            edit a draft field
  dv:<did>:<field>:<value>    set a draft field to a chosen value
  fa:<key>                    attach a pending upload to an existing task (asks for id)
  cal:<yyyy>:<mm>             calendar month
  cald:<yyyy-mm-dd>           tasks of a day
  noop                        does nothing
"""

from __future__ import annotations

import calendar
from datetime import date

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup

from .db import Database
from .models import Priority, Status, Task

CHOICE_FIELDS = ("priority", "status", "owner", "project", "feature", "folder")
TEXT_FIELDS = ("title", "deadline", "tags", "description", "recurrence", "parent")


def _rows(buttons: list[B], per_row: int) -> list[list[B]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def task_actions(task: Task) -> InlineKeyboardMarkup:
    tid = task.id
    status_btn = (
        B("↩️ Reopen", callback_data=f"tv:{tid}:status:0")
        if task.status == Status.COMPLETED
        else B("✅ Done", callback_data=f"tv:{tid}:status:5")
    )
    return InlineKeyboardMarkup(
        [
            [status_btn, B("▶️ Start", callback_data=f"tv:{tid}:status:1"), B("✏️ Edit", callback_data=f"tm:{tid}")],
            [
                B("⏰ Reminder", callback_data=f"ta:{tid}:remind"),
                B("🗂 Drive link", callback_data=f"ta:{tid}:link"),
                B("☑️ Subtask", callback_data=f"ta:{tid}:sub"),
            ],
            [
                B("📎 Files", callback_data=f"ta:{tid}:files"),
                B("📝 Note", callback_data=f"ta:{tid}:note"),
                B("🕓 History", callback_data=f"ta:{tid}:history"),
            ],
            [B("🗑 Delete", callback_data=f"ta:{tid}:delete"), B("🔄 Refresh", callback_data=f"t:{tid}")],
        ]
    )


def open_tasks_keyboard(tasks: list[Task], limit: int = 24) -> InlineKeyboardMarkup | None:
    if not tasks:
        return None
    buttons = [B(f"#{t.id}", callback_data=f"t:{t.id}") for t in tasks[:limit]]
    return InlineKeyboardMarkup(_rows(buttons, 6))


def task_edit_menu(task_id: int) -> InlineKeyboardMarkup:
    fields = ["title", "priority", "status", "owner", "deadline", "project", "feature", "folder", "tags",
              "description", "recurrence", "parent"]
    buttons = [B(f.capitalize() if f != "recurrence" else "Repeat", callback_data=f"tf:{task_id}:{f}") for f in fields]
    rows = _rows(buttons, 3)
    rows.append([B("⬅️ Back", callback_data=f"t:{task_id}")])
    return InlineKeyboardMarkup(rows)


def draft_actions(did: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [B("✅ Create", callback_data=f"dc:{did}"), B("✏️ Edit fields", callback_data=f"de:{did}")],
            [B("❌ Cancel", callback_data=f"dx:{did}")],
        ]
    )


def draft_edit_menu(did: str) -> InlineKeyboardMarkup:
    fields = ["title", "priority", "owner", "deadline", "status", "project", "feature", "folder", "tags",
              "description"]
    rows = _rows([B(f.capitalize(), callback_data=f"df:{did}:{f}") for f in fields], 3)
    rows.append([B("✅ Create", callback_data=f"dc:{did}"), B("❌ Cancel", callback_data=f"dx:{did}")])
    return InlineKeyboardMarkup(rows)


def choices(db: Database, field: str, project_id: int | None = None) -> list[tuple[str, str]]:
    """(label, value) pairs for a choice field."""
    if field == "priority":
        return [(f"{p.icon} {p.label}", str(p.value)) for p in Priority]
    if field == "status":
        return [(f"{s.icon} {s.label}", str(s.value)) for s in Status]
    if field == "owner":
        return [(o["name"], str(o["id"])) for o in db.list_owners()]
    if field == "project":
        return [(p["name"], str(p["id"])) for p in db.list_projects()]
    if field == "feature":
        feats = db.list_features(project_id) if project_id is not None else []
        return [("— none —", "0")] + [(f["name"], str(f["id"])) for f in feats]
    if field == "folder":
        return [("— none —", "0")] + [(f["name"], str(f["id"])) for f in db.list_folders()]
    raise ValueError(field)


def choice_keyboard(prefix: str, key: str, field: str, options: list[tuple[str, str]], back: str):
    buttons = [B(label[:40], callback_data=f"{prefix}:{key}:{field}:{value}") for label, value in options]
    rows = _rows(buttons, 2)
    rows.append([B("⬅️ Back", callback_data=back)])
    return InlineKeyboardMarkup(rows)


def calendar_keyboard(year: int, month: int, counts: dict[date, int], today: date) -> InlineKeyboardMarkup:
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    rows = [
        [
            B("◀️", callback_data=f"cal:{prev_y}:{prev_m:02d}"),
            B(f"{calendar.month_name[month]} {year}", callback_data="noop"),
            B("▶️", callback_data=f"cal:{next_y}:{next_m:02d}"),
        ],
        [B(d, callback_data="noop") for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")],
    ]
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for d in week:
            if d.month != month:
                row.append(B(" ", callback_data="noop"))
                continue
            label = str(d.day)
            n = counts.get(d, 0)
            if n:
                label += f"•{n}" if n > 1 else "•"
            if d == today:
                label = f"[{label}]"
            row.append(B(label, callback_data=f"cald:{d.isoformat()}"))
        rows.append(row)
    rows.append([B("📅 Today", callback_data=f"cal:{today.year}:{today.month:02d}")])
    return InlineKeyboardMarkup(rows)


def upload_choice(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [B("🆕 New task with this file", callback_data=f"fn:{key}")],
            [B("📎 Attach to existing task", callback_data=f"fa:{key}")],
            [B("❌ Ignore", callback_data=f"fx:{key}")],
        ]
    )


def confirm_delete(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[B("⚠️ Yes, delete", callback_data=f"ta:{task_id}:delok"), B("Cancel", callback_data=f"t:{task_id}")]]
    )
