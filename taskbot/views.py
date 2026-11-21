"""Text rendering (Telegram HTML parse mode)."""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Iterable, Optional

from .models import Priority, Status, Task
from .parsing import format_date
from .services import QUADRANTS, Draft, TaskService

MAX_MESSAGE = 3900  # Telegram limit is 4096; keep a margin


def deadline_badge(task: Task, today: date) -> str:
    if not task.deadline:
        return "no date"
    text = format_date(task.deadline)
    if not task.is_closed and task.status != Status.SUSPENDED:
        if task.deadline < today:
            return f"⚠️ {text} (overdue)"
        if task.deadline == today:
            return f"📌 {text} (today)"
    return text


def task_line(task: Task, today: date, show_project: bool = True) -> str:
    parts = [f"{task.priority.icon} <b>#{task.id}</b> {escape(task.title)}"]
    meta = [deadline_badge(task, today), task.status.label]
    if task.owner:
        meta.append(f"👤 {escape(task.owner)}")
    if show_project and task.project:
        meta.append(f"📁 {escape(task.project)}" + (f" / {escape(task.feature)}" if task.feature else ""))
    if task.tags:
        meta.append(" ".join(f"#{escape(t)}" for t in task.tags))
    if task.recurrence:
        meta.append(f"🔁 {task.recurrence}")
    parts.append("    " + " · ".join(meta))
    return "\n".join(parts)


def chunk_lines(header: str, lines: Iterable[str]) -> list[str]:
    chunks, current = [], header
    for line in lines:
        if len(current) + len(line) + 2 > MAX_MESSAGE:
            chunks.append(current)
            current = ""
        current += ("\n\n" if current else "") + line
    chunks.append(current)
    return chunks


def task_list(title: str, tasks: list[Task], today: date, empty: str = "No tasks.") -> list[str]:
    if not tasks:
        return [f"<b>{escape(title)}</b>\n\n{empty}"]
    return chunk_lines(f"<b>{escape(title)}</b> ({len(tasks)})", [task_line(t, today) for t in tasks])


def grouped_by_day(title: str, tasks: list[Task], today: date, empty: str = "No tasks.") -> list[str]:
    if not tasks:
        return [f"<b>{escape(title)}</b>\n\n{empty}"]
    lines, current = [], None
    for t in tasks:
        if t.deadline != current:
            current = t.deadline
            label = current.strftime("%A %d/%m/%Y") if current else "No date"
            lines.append(f"<u>{label}</u>")
        lines.append(task_line(t, today))
    return chunk_lines(f"<b>{escape(title)}</b> ({len(tasks)})", lines)


def task_card(svc: TaskService, task: Task) -> str:
    today = svc.today()
    lines = [
        f"{task.priority.icon} <b>#{task.id} {escape(task.title)}</b>",
        "",
        f"<b>Priority:</b> {task.priority.label}",
        f"<b>Status:</b> {task.status.icon} {task.status.label}",
        f"<b>Owner:</b> {escape(task.owner or '-')}",
        f"<b>Deadline:</b> {deadline_badge(task, today)}",
        f"<b>Project:</b> {escape(task.project or '-')}",
        f"<b>Feature:</b> {escape(task.feature or '-')}",
        f"<b>Folder:</b> {escape(task.folder or '-')}",
        f"<b>Tags:</b> {' '.join('#' + escape(t) for t in task.tags) or '-'}",
    ]
    if task.recurrence:
        lines.append(f"<b>Repeats:</b> 🔁 {task.recurrence}")
    if task.parent_id:
        parent = svc.db.get_task(task.parent_id)
        lines.append(f"<b>Parent:</b> #{task.parent_id} {escape(parent.title) if parent else ''}")
    if task.description:
        lines += ["", f"<i>{escape(task.description)}</i>"]

    links = svc.db.links(task.id)
    if links:
        lines += ["", "<b>🔗 Links</b>"]
        icons = {"drive": "🗂", "source": "🌐", "other": "🔗"}
        for link in links:
            label = escape(link.title or link.url)
            lines.append(f"{icons.get(link.kind, '🔗')} [{link.id}] <a href=\"{escape(link.url)}\">{label}</a>")

    atts = svc.db.attachments(task.id)
    if atts:
        lines += ["", f"<b>📎 Attachments ({len(atts)})</b>"]
        for a in atts:
            lines.append(f"[{a.id}] {escape(a.file_name or a.kind)}")

    rems = svc.db.reminders(task.id)
    if rems:
        lines += ["", "<b>⏰ Reminders</b>"]
        for r in rems:
            sent = " ✓ sent" if r.sent_at else ""
            lines.append(f"[{r.id}] {svc.describe_reminder(r, task)}{sent}")

    subs = svc.db.subtasks(task.id)
    if subs:
        done = sum(1 for s in subs if s.status == Status.COMPLETED)
        lines += ["", f"<b>☑️ Subtasks ({done}/{len(subs)})</b>"]
        for s in subs:
            mark = "✅" if s.status == Status.COMPLETED else "▫️"
            lines.append(f"{mark} #{s.id} {escape(s.title)} · {s.status.label}")

    notes = svc.db.notes(task.id)
    if notes:
        lines += ["", f"<b>📝 Notes ({len(notes)})</b>"]
        for n in notes[-3:]:
            when = n.created_at.astimezone(svc.cfg.timezone).strftime("%d/%m %H:%M")
            lines.append(f"{when} {escape(n.author or '')}: {escape(n.text)}")
        if len(notes) > 3:
            lines.append(f"… /notes {task.id} for all")
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 20] + "\n…(truncated)"


def draft_card(draft: Draft) -> str:
    lines = [
        "🆕 <b>Create a new task?</b>",
        "",
        f"<b>Title:</b> {escape(draft.title)}",
        f"<b>Priority:</b> {Priority(draft.priority).label}",
        f"<b>Owner:</b> {escape(draft.owner)}",
        f"<b>Deadline:</b> {format_date(draft.deadline_date)}",
        f"<b>Status:</b> {Status(draft.status).label}",
        f"<b>Project:</b> {escape(draft.project)}",
    ]
    if draft.feature:
        lines.append(f"<b>Feature:</b> {escape(draft.feature)}")
    if draft.folder:
        lines.append(f"<b>Folder:</b> {escape(draft.folder)}")
    if draft.tags:
        lines.append(f"<b>Tags:</b> {' '.join('#' + escape(t) for t in draft.tags)}")
    if draft.description:
        lines.append(f"<b>Description:</b> {escape(draft.description)}")
    if draft.parent_id:
        lines.append(f"<b>Subtask of:</b> #{draft.parent_id}")
    for url in draft.urls:
        lines.append(f"🔗 {escape(url)}")
    if draft.attachment:
        lines.append(f"📎 {escape(draft.attachment.get('file_name') or draft.attachment['kind'])}")
    return "\n".join(lines)


def eisenhower_text(svc: TaskService, matrix: dict[str, list[Task]]) -> list[str]:
    today = svc.today()
    lines = [
        f"<i>Important = priority 0-Critical/1-High · Urgent = deadline within {svc.cfg.urgent_days} days "
        f"or overdue</i>"
    ]
    for q, (name, desc) in QUADRANTS.items():
        tasks = matrix[q]
        block = [f"<b>{q} {name}</b> — {desc} ({len(tasks)})"]
        if not tasks:
            block.append("    —")
        for t in tasks[:15]:
            block.append(f"    {t.priority.icon} #{t.id} {escape(t.title)} · {deadline_badge(t, today)}")
        if len(tasks) > 15:
            block.append(f"    … and {len(tasks) - 15} more (/matrix {q})")
        lines.append("\n".join(block))
    return chunk_lines("<b>Eisenhower Matrix</b>", lines)


def stats_text(stats: dict) -> str:
    lines = ["<b>📊 Statistics</b>", "", f"Total tasks: {stats['total']}", f"Overdue: {stats['overdue']}",
             f"Completed this week: {stats['completed_this_week']}", ""]
    for status, n in stats["by_status"].items():
        lines.append(f"{status.icon} {status.label}: {n}")
    return "\n".join(lines)


def digest_text(svc: TaskService) -> Optional[str]:
    today = svc.today()
    due = svc.tasks_between(today, today)
    overdue = svc.overdue()
    shown = {t.id for t in due + overdue}
    high = [t for t in svc.high_priority_open() if t.id not in shown]
    if not (due or overdue or high):
        return None
    parts = [f"☀️ <b>Daily digest — {today.strftime('%A %d/%m/%Y')}</b>"]
    if overdue:
        parts.append("<b>⚠️ Overdue</b>\n" + "\n".join(task_line(t, today) for t in overdue[:20]))
    if due:
        parts.append("<b>📌 Due today</b>\n" + "\n".join(task_line(t, today) for t in due[:20]))
    if high:
        parts.append("<b>🔴 Other high-priority open tasks</b>\n" + "\n".join(task_line(t, today) for t in high[:10]))
    return "\n\n".join(parts)[:MAX_MESSAGE]
