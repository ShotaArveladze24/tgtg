"""Telegram handlers."""

from __future__ import annotations

import functools
import html
import logging
import re
from datetime import date, datetime, time
from typing import Optional

import httpx
from telegram import BotCommand, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import keyboards as kb
from . import views
from .config import Config
from .db import Database, TaskFilter
from .models import OPEN_STATUSES, Status
from .parsing import (
    ParseError,
    extract_hashtags,
    format_date,
    is_drive_url,
    parse_kv,
    parse_priority,
    parse_status,
    parse_tag_ops,
    parse_task_id,
    split_title_and_urls,
)
from .services import QUADRANTS, Draft, ServiceError, TaskService

log = logging.getLogger(__name__)

HELP = """<b>TaskBot — commands</b>

<b>Create</b>
Send any text, link or file → the bot proposes a new task (defaults: 9-Undefined, owner PNC, deadline T+7, 0-To_Start, No PROJECT). Hashtags in the text become tags.
/new &lt;text or link&gt; — same, also in groups
/sub &lt;id&gt; &lt;title&gt; — add a subtask

<b>Look up</b>
/task &lt;id&gt; — task card with action buttons
/list [all] [owner=.. project=.. feature=.. folder=.. tag=.. status=.. priority=..] — sorted by deadline
/today · /week · /nextweek · /overdue · /nodate
/high — open tasks with priority 0/1
/matrix [Q1..Q4] — Eisenhower Matrix
/calendar — month view
/search &lt;text&gt; · /stats

<b>Edit</b>
/set &lt;id&gt; &lt;field&gt; &lt;value&gt; — fields: title, description, priority, status, owner, deadline, project, feature, folder, tags, recurrence, parent
/done &lt;id&gt; · /status &lt;id&gt; &lt;s&gt; · /prio &lt;id&gt; &lt;p&gt; · /deadline &lt;id&gt; &lt;date|none&gt; · /assign &lt;id&gt; &lt;owner&gt;
/move &lt;id&gt; project="Name" [feature="Name"]
/tag &lt;id&gt; +tag -tag · /tags
/repeat &lt;id&gt; daily|weekly|monthly|yearly|none
/delete &lt;id&gt;

<b>Reminders, links, files, notes</b>
/remind &lt;id&gt; &lt;spec&gt; — -1d, -2h, -30m (before deadline), in 2h, 25/11/2026 09:00, tomorrow 10:00
/link &lt;id&gt; &lt;Google Drive URL&gt; [title]
/attach &lt;id&gt; — reply to a file; or send a file with caption <code>#&lt;id&gt;</code>
/files &lt;id&gt; · /note &lt;id&gt; &lt;text&gt; · /notes &lt;id&gt; · /history &lt;id&gt;

<b>Setup</b>
/owners · /owner add &lt;name&gt; [telegram_id] · /owner link &lt;name&gt; &lt;telegram_id|me&gt; · /owner rename &lt;old&gt; | &lt;new&gt; · /owner del &lt;name&gt;
/projects · /project &lt;name&gt; · /project add|rename|del ...
/features [project] · /feature add &lt;project&gt; | &lt;name&gt; · /feature del &lt;project&gt; | &lt;name&gt;
/folders · /folder &lt;name&gt; · /folder add|del &lt;name&gt;
/digest on|off — daily summary in this chat
/export — CSV of all tasks
/cancel — abort the current input
/whoami — your Telegram id

Dates: DD/MM/YYYY, today, tomorrow, +3d, +2w, monday, none."""

COMMANDS = [
    ("new", "Create a task"),
    ("task", "Show a task"),
    ("list", "All open tasks by deadline"),
    ("today", "Tasks due today"),
    ("week", "Tasks due this week"),
    ("nextweek", "Tasks due next week"),
    ("overdue", "Overdue tasks"),
    ("high", "Open high-priority tasks"),
    ("matrix", "Eisenhower Matrix"),
    ("calendar", "Calendar"),
    ("search", "Search tasks"),
    ("projects", "Projects"),
    ("owners", "Owners"),
    ("folders", "Folders"),
    ("tags", "Tags"),
    ("stats", "Statistics"),
    ("help", "Help"),
]

FIELD_HINTS = {
    "title": "Send the new title.",
    "description": "Send the description (or '-' to clear).",
    "deadline": "Send the deadline: DD/MM/YYYY, today, tomorrow, +3d, monday, or 'none' for no date.",
    "tags": "Send the tags separated by spaces (replaces existing tags), e.g. <code>work urgent</code>.",
    "recurrence": "Send daily, weekly, monthly, yearly or none.",
    "parent": "Send the parent task id, or 'none' to make it a top-level task.",
}


# ------------------------------------------------------------------ helpers
def svc_of(context: ContextTypes.DEFAULT_TYPE) -> TaskService:
    return context.application.bot_data["svc"]


def cfg_of(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["cfg"]


def actor_of(update: Update) -> str:
    u = update.effective_user
    return (u.username and f"@{u.username}") or u.full_name if u else "system"


def restricted(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        cfg = cfg_of(context)
        if user is None or user.id not in cfg.allowed_users:
            log.warning("Unauthorized access from user %s", user.id if user else None)
            if update.callback_query:
                await update.callback_query.answer("Not authorized", show_alert=True)
            elif update.effective_chat and update.effective_chat.type == ChatType.PRIVATE and update.message:
                await update.message.reply_text(
                    f"⛔ Not authorized. Your Telegram user id is {user.id if user else '?'} — "
                    "ask the admin to add it to TASKBOT_ALLOWED_USERS."
                )
            return
        try:
            return await func(update, context)
        except (ServiceError, ParseError) as exc:
            if update.callback_query:
                await update.callback_query.answer(str(exc)[:190], show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(f"❗ {exc}")

    return wrapper


async def reply(message: Message, chunks: list[str] | str, keyboard=None) -> None:
    if isinstance(chunks, str):
        chunks = [chunks]
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard if last else None,
        )


async def edit_or_reply(update: Update, text: str, keyboard=None) -> None:
    q = update.callback_query
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                  reply_markup=keyboard)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                       reply_markup=keyboard)


async def show_task(message: Message, svc: TaskService, task_id: int) -> None:
    task = svc.require_task(task_id)
    await reply(message, views.task_card(svc, task), kb.task_actions(task))


def args_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args or []).strip()


def need_id(context: ContextTypes.DEFAULT_TYPE, usage: str) -> tuple[int, str]:
    if not context.args:
        raise ServiceError(f"Usage: {usage}")
    task_id = parse_task_id(context.args[0])
    return task_id, " ".join(context.args[1:]).strip()


def set_awaiting(context: ContextTypes.DEFAULT_TYPE, **state) -> None:
    context.user_data["awaiting"] = state


def pop_awaiting(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.pop("awaiting", None)


def store_draft(context: ContextTypes.DEFAULT_TYPE, draft: Draft) -> str:
    drafts = context.user_data.setdefault("drafts", {})
    counter = context.user_data.get("draft_counter", 0) + 1
    context.user_data["draft_counter"] = counter
    did = str(counter)
    drafts[did] = draft.to_dict()
    # keep only the 20 most recent drafts
    for old in list(drafts)[:-20]:
        drafts.pop(old, None)
    return did


def get_draft(context: ContextTypes.DEFAULT_TYPE, did: str) -> Draft:
    data = context.user_data.get("drafts", {}).get(did)
    if not data:
        raise ServiceError("This draft has expired. Send the input again.")
    return Draft(**data)


def save_draft(context: ContextTypes.DEFAULT_TYPE, did: str, draft: Draft) -> None:
    context.user_data.setdefault("drafts", {})[did] = draft.to_dict()


def attachment_from_message(msg: Message) -> Optional[dict]:
    if msg.document:
        d = msg.document
        return {"file_id": d.file_id, "kind": "document", "file_name": d.file_name, "mime_type": d.mime_type}
    if msg.photo:
        p = msg.photo[-1]
        return {"file_id": p.file_id, "kind": "photo", "file_name": "photo.jpg", "mime_type": "image/jpeg"}
    if msg.video:
        v = msg.video
        return {"file_id": v.file_id, "kind": "video", "file_name": v.file_name or "video", "mime_type": v.mime_type}
    if msg.audio:
        a = msg.audio
        return {"file_id": a.file_id, "kind": "audio", "file_name": a.file_name or a.title or "audio",
                "mime_type": a.mime_type}
    if msg.voice:
        return {"file_id": msg.voice.file_id, "kind": "voice", "file_name": "voice message",
                "mime_type": msg.voice.mime_type}
    return None


async def fetch_title(url: str) -> Optional[str]:
    """Best-effort fetch of a web page <title>."""
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (TaskBot)"}) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", ""):
                    return None
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > 200_000 or b"</title>" in body.lower():
                        break
        m = re.search(rb"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        title = html.unescape(m.group(1).decode("utf-8", "replace")).strip()
        title = re.sub(r"\s+", " ", title)
        return title[:300] or None
    except Exception as exc:  # noqa: BLE001 - best effort only
        log.debug("Could not fetch title for %s: %s", url, exc)
        return None


async def propose_task(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    attachment: Optional[dict] = None,
    parent_id: Optional[int] = None,
) -> None:
    svc, cfg = svc_of(context), cfg_of(context)
    title, urls = split_title_and_urls(text or "")
    tags = extract_hashtags(title)
    title = re.sub(r"(?<!\w)#\w[\w\-]*", "", title).strip() if tags else title
    if not title and urls and cfg.fetch_link_titles and not is_drive_url(urls[0]):
        title = await fetch_title(urls[0]) or ""
    if not title and urls:
        title = urls[0]
    if not title and attachment:
        title = attachment.get("file_name") or attachment["kind"]
    if not title:
        raise ServiceError("Send some text, a link or a file to create a task.")
    draft = svc.new_draft(title, urls=urls, attachment=attachment, tags=tags, parent_id=parent_id)
    did = store_draft(context, draft)
    await reply(message, views.draft_card(draft), kb.draft_actions(did))


# ----------------------------------------------------------------- commands
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_text(f"Your Telegram user id is {u.id}; this chat id is {update.effective_chat.id}.")


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update.message, "👋 Welcome to TaskBot. Send me a link, a text or a file to create a task.\n\n" + HELP)


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update.message, HELP)


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pop_awaiting(context)
    await update.message.reply_text("OK, cancelled.")


@restricted
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = args_text(context)
    reply_to = update.message.reply_to_message
    attachment = attachment_from_message(reply_to) if reply_to else None
    if not text and reply_to:
        text = reply_to.text or reply_to.caption or ""
    if not text and not attachment:
        set_awaiting(context, kind="new")
        await update.message.reply_text("Send the task text or link (or /cancel).")
        return
    await propose_task(update.message, context, text, attachment)


@restricted
async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, _ = need_id(context, "/task <id>")
    await show_task(update.message, svc_of(context), task_id)


def _filter_from_args(svc: TaskService, text: str) -> tuple[TaskFilter, str]:
    kv, rest = parse_kv(text)
    include_all = "all" in [r.lower() for r in rest]
    flt = TaskFilter(statuses=None if include_all else list(OPEN_STATUSES))
    desc = []
    if "owner" in kv:
        flt.owner = kv["owner"]
        desc.append(f"owner={kv['owner']}")
    if "project" in kv:
        p = svc.db.get_project(kv["project"])
        if not p:
            raise ServiceError(f"Unknown project '{kv['project']}'")
        flt.project_id = p["id"]
        desc.append(f"project={p['name']}")
        if "feature" in kv:
            f = svc.db.get_feature(p["id"], kv["feature"])
            if not f:
                raise ServiceError(f"Unknown feature '{kv['feature']}' in {p['name']}")
            flt.feature_id = f["id"]
            desc.append(f"feature={f['name']}")
    elif "feature" in kv:
        raise ServiceError("Use feature= together with project=")
    if "folder" in kv:
        fo = svc.db.get_folder(kv["folder"])
        if not fo:
            raise ServiceError(f"Unknown folder '{kv['folder']}'")
        flt.folder_id = fo["id"]
        desc.append(f"folder={fo['name']}")
    if "tag" in kv:
        flt.tag = kv["tag"].lstrip("#").lower()
        desc.append(f"#{flt.tag}")
    if "status" in kv:
        flt.statuses = [parse_status(s) for s in kv["status"].split(",")]
        desc.append(f"status={kv['status']}")
    if "priority" in kv:
        flt.priorities = [parse_priority(p) for p in kv["priority"].split(",")]
        desc.append(f"priority={kv['priority']}")
    label = "All tasks" if include_all else "Open tasks"
    return flt, label + (f" ({', '.join(desc)})" if desc else "")


@restricted
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    flt, label = _filter_from_args(svc, args_text(context))
    tasks = svc.db.find_tasks(flt)
    await reply(update.message, views.task_list(label + " — by deadline", tasks, svc.today()),
                kb.open_tasks_keyboard(tasks))


@restricted
async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.args = ["all"] + list(context.args or [])
    await cmd_list.__wrapped__(update, context)


@restricted
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    today = svc.today()
    overdue = svc.overdue()
    tasks = svc.tasks_between(today, today)
    chunks = views.task_list(f"📌 Today — {today.strftime('%A %d/%m/%Y')}", tasks, today, "Nothing due today 🎉")
    if overdue:
        chunks += views.task_list("⚠️ Overdue", overdue, today)
    await reply(update.message, chunks, kb.open_tasks_keyboard(overdue + tasks))


async def _week(update: Update, context: ContextTypes.DEFAULT_TYPE, offset: int, label: str) -> None:
    svc = svc_of(context)
    start, end = svc.week_range(offset)
    tasks = svc.tasks_between(start, end)
    title = f"{label} ({format_date(start)} – {format_date(end)})"
    await reply(update.message, views.grouped_by_day(title, tasks, svc.today()), kb.open_tasks_keyboard(tasks))


@restricted
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _week(update, context, 0, "🗓 This week")


@restricted
async def cmd_nextweek(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _week(update, context, 1, "🗓 Next week")


@restricted
async def cmd_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    tasks = svc.overdue()
    await reply(update.message, views.task_list("⚠️ Overdue tasks", tasks, svc.today(), "Nothing overdue 🎉"),
                kb.open_tasks_keyboard(tasks))


@restricted
async def cmd_nodate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    tasks = svc.db.find_tasks(TaskFilter(statuses=list(OPEN_STATUSES), has_deadline=False))
    await reply(update.message, views.task_list("Open tasks without deadline", tasks, svc.today()),
                kb.open_tasks_keyboard(tasks))


@restricted
async def cmd_high(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    tasks = svc.high_priority_open()
    await reply(
        update.message,
        views.task_list("🔴 High priority, not completed (P0/P1 · status 0,1,2,4)", tasks, svc.today(),
                        "No open high-priority tasks."),
        kb.open_tasks_keyboard(tasks),
    )


@restricted
async def cmd_matrix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    matrix = svc.eisenhower()
    q = args_text(context).upper()
    if q in QUADRANTS:
        name, desc = QUADRANTS[q]
        tasks = matrix[q]
        await reply(update.message, views.task_list(f"{q} {name} — {desc}", tasks, svc.today()),
                    kb.open_tasks_keyboard(tasks))
        return
    await reply(update.message, views.eisenhower_text(svc, matrix))


@restricted
async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    today = svc.today()
    await update.message.reply_text(
        "📅 <b>Calendar</b> — days with open tasks are marked with •. Tap a day.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.calendar_keyboard(today.year, today.month, svc.month_counts(today.year, today.month), today),
    )


@restricted
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    q = args_text(context)
    if not q:
        raise ServiceError("Usage: /search <text>")
    tasks = svc.db.find_tasks(TaskFilter(text=q, limit=100))
    await reply(update.message, views.task_list(f"🔎 Results for “{q}”", tasks, svc.today()),
                kb.open_tasks_keyboard(tasks))


@restricted
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update.message, views.stats_text(svc_of(context).stats()))


async def _after_edit(message: Message, svc: TaskService, task_id: int, spawned) -> None:
    await show_task(message, svc, task_id)
    for t in spawned:
        await message.reply_text(
            f"🔁 Next occurrence created: #{t.id} due {format_date(t.deadline)}",
        )


@restricted
async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) < 3:
        raise ServiceError("Usage: /set <id> <field> <value>")
    task_id = parse_task_id(context.args[0])
    field, value = context.args[1], " ".join(context.args[2:])
    svc = svc_of(context)
    _, spawned = svc.set_field(task_id, field, value, actor_of(update))
    await _after_edit(update.message, svc, task_id, spawned)


def _shortcut(field: str, usage: str):
    @restricted
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        task_id, value = need_id(context, usage)
        if not value:
            raise ServiceError(f"Usage: {usage}")
        svc = svc_of(context)
        _, spawned = svc.set_field(task_id, field, value, actor_of(update))
        await _after_edit(update.message, svc, task_id, spawned)

    return handler


cmd_status = _shortcut("status", "/status <id> <status>")
cmd_prio = _shortcut("priority", "/prio <id> <priority>")
cmd_deadline = _shortcut("deadline", "/deadline <id> <DD/MM/YYYY|none>")
cmd_assign = _shortcut("owner", "/assign <id> <owner>")
cmd_repeat = _shortcut("recurrence", "/repeat <id> daily|weekly|monthly|yearly|none")


@restricted
async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        raise ServiceError("Usage: /done <id> [id ...]")
    svc = svc_of(context)
    for arg in context.args:
        task_id = parse_task_id(arg)
        svc.require_task(task_id)
        spawned = svc.set_status(task_id, Status.COMPLETED, actor_of(update))
        msg = f"✅ #{task_id} completed."
        for t in spawned:
            msg += f"\n🔁 Next occurrence: #{t.id} due {format_date(t.deadline)}"
        await update.message.reply_text(msg)


@restricted
async def cmd_move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, rest = need_id(context, '/move <id> project="Name" [feature="Name"]')
    kv, _ = parse_kv(rest)
    if "project" not in kv and "feature" not in kv:
        raise ServiceError('Usage: /move <id> project="Name" [feature="Name"]')
    svc = svc_of(context)
    feature = kv.get("feature")
    if feature and feature.lower() in ("none", "-"):
        feature = None
    svc.move(task_id, kv.get("project"), feature, actor_of(update))
    await show_task(update.message, svc, task_id)


@restricted
async def cmd_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, rest = need_id(context, "/tag <id> +tag -tag")
    add, remove = parse_tag_ops(rest)
    if not add and not remove:
        raise ServiceError("Usage: /tag <id> +tag -tag")
    svc = svc_of(context)
    svc.edit_tags(task_id, add, remove, actor_of(update))
    await show_task(update.message, svc, task_id)


@restricted
async def cmd_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    if context.args:
        context.args = [f"tag={context.args[0]}"]
        await cmd_list.__wrapped__(update, context)
        return
    rows = svc.db.all_tags()
    text = "<b>🏷 Tags (open tasks)</b>\n\n" + (
        "\n".join(f"#{html.escape(r['tag'])} ({r['n']})" for r in rows) or "No tags yet."
    ) + "\n\n/tags &lt;tag&gt; lists its tasks."
    await reply(update.message, text)


@restricted
async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, spec = need_id(context, "/remind <id> <-1d | in 2h | DD/MM/YYYY HH:MM>")
    svc = svc_of(context)
    if not spec:
        task = svc.require_task(task_id)
        rems = svc.db.reminders(task_id)
        text = f"⏰ Reminders for #{task_id}:\n" + (
            "\n".join(f"[{r.id}] {svc.describe_reminder(r, task)}{' ✓' if r.sent_at else ''}" for r in rems)
            or "none"
        ) + "\nAdd: /remind <id> <spec> · Remove: /unremind <id> <reminder_id>"
        await update.message.reply_text(text)
        return
    rem = svc.add_reminder(task_id, spec, actor_of(update))
    await update.message.reply_text(f"⏰ Reminder set for #{task_id}: {svc.describe_reminder(rem, svc.require_task(task_id))}")


@restricted
async def cmd_unremind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, rest = need_id(context, "/unremind <id> <reminder_id>")
    if not rest.isdigit() or not svc_of(context).db.delete_reminder(task_id, int(rest)):
        raise ServiceError("Reminder not found. See /remind <id> for ids.")
    await update.message.reply_text("Reminder removed.")


@restricted
async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, rest = need_id(context, "/link <id> <Google Drive URL> [title]")
    if not rest:
        raise ServiceError("Usage: /link <id> <Google Drive URL> [title]")
    url, *title = rest.split(maxsplit=1)
    svc = svc_of(context)
    svc.add_drive_link(task_id, url, actor_of(update), title[0] if title else None)
    await show_task(update.message, svc, task_id)


@restricted
async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, rest = need_id(context, "/unlink <id> <link_id>")
    if not rest.isdigit() or not svc_of(context).db.delete_link(task_id, int(rest)):
        raise ServiceError("Link not found. Link ids are shown in [brackets] on the task card.")
    await update.message.reply_text("Link removed.")


@restricted
async def cmd_attach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, _ = need_id(context, "/attach <id> (as a reply to a file)")
    svc = svc_of(context)
    svc.require_task(task_id)
    reply_to = update.message.reply_to_message
    att = attachment_from_message(reply_to) if reply_to else None
    if att:
        svc.add_attachment(task_id, att, actor_of(update))
        await update.message.reply_text(f"📎 Attached to #{task_id}.")
        return
    set_awaiting(context, kind="attach_file", task_id=task_id)
    await update.message.reply_text(f"Send the file to attach to #{task_id} (or /cancel).")


@restricted
async def cmd_detach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, rest = need_id(context, "/detach <id> <attachment_id>")
    if not rest.isdigit() or not svc_of(context).db.delete_attachment(task_id, int(rest)):
        raise ServiceError("Attachment not found. Ids are shown in [brackets] on the task card.")
    await update.message.reply_text("Attachment removed.")


async def send_files(message: Message, svc: TaskService, task_id: int) -> None:
    svc.require_task(task_id)
    atts = svc.db.attachments(task_id)
    if not atts:
        await message.reply_text(f"No attachments on #{task_id}. Send a file with caption #{task_id} to add one.")
        return
    for a in atts:
        caption = f"#{task_id} · [{a.id}] {a.file_name or a.kind}"
        sender = {
            "photo": message.reply_photo,
            "video": message.reply_video,
            "audio": message.reply_audio,
            "voice": message.reply_voice,
        }.get(a.kind, message.reply_document)
        await sender(a.file_id, caption=caption)


@restricted
async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, _ = need_id(context, "/files <id>")
    await send_files(update.message, svc_of(context), task_id)


@restricted
async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parent_id, title = need_id(context, "/sub <parent_id> <title>")
    svc = svc_of(context)
    if not title:
        set_awaiting(context, kind="subtask", task_id=parent_id)
        await update.message.reply_text(f"Send the subtask title for #{parent_id} (or /cancel).")
        return
    t = svc.add_subtask(parent_id, title, update.effective_chat.id, update.effective_user.id, actor_of(update))
    await show_task(update.message, svc, t.id)


@restricted
async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, text = need_id(context, "/note <id> <text>")
    svc = svc_of(context)
    svc.require_task(task_id)
    if not text:
        set_awaiting(context, kind="note", task_id=task_id)
        await update.message.reply_text(f"Send the note for #{task_id} (or /cancel).")
        return
    svc.db.add_note(task_id, text, actor_of(update))
    await update.message.reply_text(f"📝 Note added to #{task_id}.")


@restricted
async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, _ = need_id(context, "/notes <id>")
    svc = svc_of(context)
    svc.require_task(task_id)
    notes = svc.db.notes(task_id)
    lines = [
        f"{n.created_at.astimezone(svc.cfg.timezone).strftime('%d/%m/%Y %H:%M')} "
        f"<b>{html.escape(n.author or '')}</b>: {html.escape(n.text)}"
        for n in notes
    ]
    await reply(update.message, views.chunk_lines(f"<b>📝 Notes of #{task_id}</b>", lines or ["No notes."]))


def history_text(svc: TaskService, task_id: int) -> list[str]:
    rows = svc.db.history(task_id)
    lines = [
        f"{datetime.fromisoformat(r['at']).astimezone(svc.cfg.timezone).strftime('%d/%m/%Y %H:%M')} "
        f"{html.escape(r['actor'] or '')}: {html.escape(r['action'])}"
        for r in rows
    ]
    return views.chunk_lines(f"<b>🕓 History of #{task_id}</b>", lines or ["No history."])


@restricted
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, _ = need_id(context, "/history <id>")
    svc = svc_of(context)
    svc.require_task(task_id)
    await reply(update.message, history_text(svc, task_id))


@restricted
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id, _ = need_id(context, "/delete <id>")
    task = svc_of(context).require_task(task_id)
    await update.message.reply_text(
        f"Delete #{task.id} “{task.title}” and all its subtasks, links, files and notes?",
        reply_markup=kb.confirm_delete(task_id),
    )


# --------------------------------------------------------- setup commands
def _split_pipe(text: str, n: int, usage: str) -> list[str]:
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != n or not all(parts):
        raise ServiceError(f"Usage: {usage}")
    return parts


@restricted
async def cmd_owners(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    lines = []
    for o in svc.db.list_owners():
        linked = f" (Telegram {o['telegram_user_id']})" if o["telegram_user_id"] else ""
        default = " — default" if o["name"].lower() == svc.cfg.default_owner.lower() else ""
        lines.append(f"👤 {html.escape(o['name'])}{linked}{default}")
    await reply(update.message, "<b>Owners</b>\n\n" + "\n".join(lines)
                + "\n\n/owner add &lt;name&gt; [telegram_id] · /owner link &lt;name&gt; &lt;id|me&gt; · "
                  "/owner rename &lt;old&gt; | &lt;new&gt; · /owner del &lt;name&gt;")


@restricted
async def cmd_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    if not context.args:
        await cmd_owners.__wrapped__(update, context)
        return
    sub, rest = context.args[0].lower(), " ".join(context.args[1:]).strip()
    if sub == "add":
        if not rest:
            raise ServiceError("Usage: /owner add <name> [telegram_id]")
        m = re.fullmatch(r"(.+?)(?:\s+(\d{3,}))?", rest)
        name, tg = m.group(1).strip(), m.group(2)
        if svc.db.get_owner(name):
            raise ServiceError(f"Owner '{name}' already exists")
        svc.db.add_owner(name, int(tg) if tg else None)
        await update.message.reply_text(f"👤 Owner '{name}' added.")
    elif sub == "link":
        m = re.fullmatch(r"(.+?)\s+(\d+|me|none)", rest)
        if not m:
            raise ServiceError("Usage: /owner link <name> <telegram_id|me|none>")
        name, who = m.group(1).strip(), m.group(2)
        tg = update.effective_user.id if who == "me" else None if who == "none" else int(who)
        if not svc.db.set_owner_user(name, tg):
            raise ServiceError(f"Unknown owner '{name}'")
        await update.message.reply_text(
            f"👤 '{name}' linked to Telegram id {tg}. Reminders for their tasks will also be sent to them privately "
            "(they must have started the bot)." if tg else f"👤 '{name}' unlinked."
        )
    elif sub == "rename":
        old, new = _split_pipe(rest, 2, "/owner rename <old> | <new>")
        if old.lower() == svc.cfg.default_owner.lower():
            raise ServiceError("The default owner is set in TASKBOT_DEFAULT_OWNER and cannot be renamed here")
        if not svc.db.rename_owner(old, new):
            raise ServiceError(f"Unknown owner '{old}'")
        await update.message.reply_text(f"👤 '{old}' renamed to '{new}'.")
    elif sub in ("del", "delete", "remove"):
        if rest.lower() == svc.cfg.default_owner.lower():
            raise ServiceError("The default owner cannot be deleted")
        if not svc.db.get_owner(rest):
            raise ServiceError(f"Unknown owner '{rest}'")
        n = svc.db.owner_task_count(rest)
        if n:
            raise ServiceError(f"'{rest}' owns {n} task(s). Reassign them first (/list owner=\"{rest}\" all).")
        svc.db.delete_owner(rest)
        await update.message.reply_text(f"👤 Owner '{rest}' deleted.")
    else:
        raise ServiceError("Usage: /owner add|link|rename|del ...")


@restricted
async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    lines = []
    for p in svc.db.list_projects():
        lines.append(f"📁 <b>{html.escape(p['name'])}</b> — {p['open_tasks']} open")
        for f in svc.db.list_features(p["id"]):
            lines.append(f"    └ 🧩 {html.escape(f['name'])} — {f['open_tasks']} open")
    await reply(update.message, "<b>Projects</b>\n\n" + "\n".join(lines)
                + "\n\n/project &lt;name&gt; shows its tasks · /project add &lt;name&gt; · "
                  "/feature add &lt;project&gt; | &lt;feature&gt;")


@restricted
async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    text = args_text(context)
    if not text:
        await cmd_projects.__wrapped__(update, context)
        return
    sub, _, rest = text.partition(" ")
    sub, rest = sub.lower(), rest.strip()
    if sub == "add":
        if not rest:
            raise ServiceError("Usage: /project add <name>")
        if svc.db.get_project(rest):
            raise ServiceError(f"Project '{rest}' already exists")
        svc.db.add_project(rest)
        await update.message.reply_text(f"📁 Project '{rest}' created.")
    elif sub == "rename":
        old, new = _split_pipe(rest, 2, "/project rename <old> | <new>")
        p = svc.db.get_project(old)
        if not p:
            raise ServiceError(f"Unknown project '{old}'")
        if p["is_default"]:
            raise ServiceError("The default project cannot be renamed here (TASKBOT_DEFAULT_PROJECT)")
        svc.db.rename_project(old, new)
        await update.message.reply_text(f"📁 '{old}' renamed to '{new}'.")
    elif sub in ("del", "delete", "remove"):
        p = svc.db.get_project(rest)
        if not p:
            raise ServiceError(f"Unknown project '{rest}'")
        if p["is_default"]:
            raise ServiceError("The default project cannot be deleted")
        default = svc.db.default_project()
        svc.db.delete_project(p["id"], default["id"])
        await update.message.reply_text(f"📁 Project '{rest}' deleted; its tasks moved to '{default['name']}'.")
    else:
        p = svc.db.get_project(text)
        if not p:
            raise ServiceError(f"Unknown project '{text}'. Usage: /project <name> | add | rename | del")
        await reply(update.message, project_tree(svc, p))


def project_tree(svc: TaskService, project) -> list[str]:
    today = svc.today()
    tasks = svc.db.find_tasks(TaskFilter(project_id=project["id"], statuses=list(OPEN_STATUSES)))
    by_id = {t.id: t for t in tasks}
    children: dict[Optional[int], list] = {}
    for t in tasks:
        parent = t.parent_id if t.parent_id in by_id else None
        children.setdefault(parent, []).append(t)

    def render(task, depth):
        out = [("    " * depth) + ("└ " if depth else "") + views.task_line(task, today, show_project=False)
               .replace("\n", "\n" + "    " * depth)]
        for c in children.get(task.id, []):
            out += render(c, depth + 1)
        return out

    lines = []
    groups: dict[Optional[str], list] = {}
    for t in children.get(None, []):
        groups.setdefault(t.feature, []).append(t)
    for feat in sorted(groups, key=lambda f: (f is None, (f or "").lower())):
        lines.append(f"🧩 <b>{html.escape(feat) if feat else '(no feature)'}</b>")
        for t in groups[feat]:
            lines += render(t, 1)
    return views.chunk_lines(f"📁 <b>{html.escape(project['name'])}</b> — {len(tasks)} open tasks",
                             lines or ["No open tasks."])


@restricted
async def cmd_features(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    name = args_text(context)
    pid = None
    if name:
        p = svc.db.get_project(name)
        if not p:
            raise ServiceError(f"Unknown project '{name}'")
        pid = p["id"]
    rows = svc.db.list_features(pid)
    lines = [f"🧩 {html.escape(r['project_name'])} / <b>{html.escape(r['name'])}</b> — {r['open_tasks']} open"
             for r in rows]
    await reply(update.message, "<b>Features</b>\n\n" + ("\n".join(lines) or "No features yet."))


@restricted
async def cmd_feature(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    text = args_text(context)
    sub, _, rest = text.partition(" ")
    sub = sub.lower()
    if sub == "add":
        proj_name, name = _split_pipe(rest, 2, "/feature add <project> | <feature>")
        p = svc.db.get_project(proj_name)
        if not p:
            raise ServiceError(f"Unknown project '{proj_name}'")
        if svc.db.get_feature(p["id"], name):
            raise ServiceError("Feature already exists")
        svc.db.add_feature(p["id"], name)
        await update.message.reply_text(f"🧩 Feature '{name}' added to '{p['name']}'.")
    elif sub in ("del", "delete", "remove"):
        proj_name, name = _split_pipe(rest, 2, "/feature del <project> | <feature>")
        p = svc.db.get_project(proj_name)
        f = svc.db.get_feature(p["id"], name) if p else None
        if not f:
            raise ServiceError("Feature not found")
        svc.db.delete_feature(f["id"])
        await update.message.reply_text(f"🧩 Feature '{name}' deleted (its tasks stay in '{p['name']}').")
    else:
        await cmd_features.__wrapped__(update, context)


@restricted
async def cmd_folders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    lines = [f"🗄 <b>{html.escape(f['name'])}</b> — {f['open_tasks']} open" for f in svc.db.list_folders()]
    await reply(update.message, "<b>Folders</b>\n\n" + ("\n".join(lines) or "No folders yet.")
                + "\n\n/folder add &lt;name&gt; · /folder del &lt;name&gt; · /folder &lt;name&gt;")


@restricted
async def cmd_folder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    text = args_text(context)
    if not text:
        await cmd_folders.__wrapped__(update, context)
        return
    sub, _, rest = text.partition(" ")
    sub, rest = sub.lower(), rest.strip()
    if sub == "add":
        if not rest:
            raise ServiceError("Usage: /folder add <name>")
        if svc.db.get_folder(rest):
            raise ServiceError("Folder already exists")
        svc.db.add_folder(rest)
        await update.message.reply_text(f"🗄 Folder '{rest}' created.")
    elif sub in ("del", "delete", "remove"):
        f = svc.db.get_folder(rest)
        if not f:
            raise ServiceError(f"Unknown folder '{rest}'")
        svc.db.delete_folder(f["id"])
        await update.message.reply_text(f"🗄 Folder '{rest}' deleted (tasks kept, folder cleared).")
    else:
        f = svc.db.get_folder(text)
        if not f:
            raise ServiceError(f"Unknown folder '{text}'")
        tasks = svc.db.find_tasks(TaskFilter(folder_id=f["id"], statuses=list(OPEN_STATUSES)))
        await reply(update.message, views.task_list(f"🗄 {f['name']}", tasks, svc.today()),
                    kb.open_tasks_keyboard(tasks))


@restricted
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = args_text(context).lower()
    svc = svc_of(context)
    if arg not in ("on", "off"):
        text = views.digest_text(svc) or "Nothing due, overdue or high-priority. 🎉"
        await reply(update.message, text + "\n\nUse /digest on|off to receive it every morning.")
        return
    svc.db.set_digest(update.effective_chat.id, arg == "on")
    t = svc.cfg.digest_time.strftime("%H:%M")
    await update.message.reply_text(f"Daily digest {'enabled at ' + t if arg == 'on' else 'disabled'} for this chat.")


@restricted
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    data = svc.export_csv()
    await update.message.reply_document(data, filename=f"tasks_{svc.today().isoformat()}.csv",
                                        caption="All tasks (CSV, ';' separated)")


# ------------------------------------------------------- free text & files
@restricted
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    svc = svc_of(context)
    state = pop_awaiting(context)
    text = msg.text or ""
    if state:
        kind = state["kind"]
        try:
            if kind == "draft_field":
                did = state["draft"]
                draft = get_draft(context, did)
                svc.set_draft_field(draft, state["field"], text)
                save_draft(context, did, draft)
                await reply(msg, views.draft_card(draft), kb.draft_edit_menu(did))
            elif kind == "task_field":
                _, spawned = svc.set_field(state["task_id"], state["field"], text, actor_of(update))
                await _after_edit(msg, svc, state["task_id"], spawned)
            elif kind == "remind":
                rem = svc.add_reminder(state["task_id"], text, actor_of(update))
                task = svc.require_task(state["task_id"])
                await msg.reply_text(f"⏰ Reminder set for #{task.id}: {svc.describe_reminder(rem, task)}")
            elif kind == "link":
                url, *title = text.split(maxsplit=1)
                svc.add_drive_link(state["task_id"], url, actor_of(update), title[0] if title else None)
                await show_task(msg, svc, state["task_id"])
            elif kind == "subtask":
                t = svc.add_subtask(state["task_id"], text, msg.chat_id, update.effective_user.id, actor_of(update))
                await show_task(msg, svc, t.id)
            elif kind == "note":
                svc.db.add_note(state["task_id"], text, actor_of(update))
                await msg.reply_text(f"📝 Note added to #{state['task_id']}.")
            elif kind == "attach_upload":
                task_id = parse_task_id(text)
                att = context.user_data.get("uploads", {}).pop(state["key"], None)
                if not att:
                    raise ServiceError("The uploaded file has expired; send it again.")
                svc.add_attachment(task_id, att, actor_of(update))
                await msg.reply_text(f"📎 Attached to #{task_id}.")
            elif kind == "new":
                await propose_task(msg, context, text)
            elif kind == "attach_file":
                raise ServiceError("Please send a file (or /cancel).")
        except (ServiceError, ParseError):
            set_awaiting(context, **state)  # let the user retry
            raise
        return
    if msg.chat.type != ChatType.PRIVATE:
        return  # in groups, only explicit commands create tasks
    await propose_task(msg, context, text)


@restricted
async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    svc = svc_of(context)
    att = attachment_from_message(msg)
    if not att:
        return
    caption = (msg.caption or "").strip()
    state = context.user_data.get("awaiting")
    if state and state["kind"] == "attach_file":
        pop_awaiting(context)
        svc.add_attachment(state["task_id"], att, actor_of(update))
        await msg.reply_text(f"📎 Attached to #{state['task_id']}.")
        return
    m = re.fullmatch(r"(?:/attach\s+)?#(\d+)", caption)
    if m:
        task_id = int(m.group(1))
        svc.add_attachment(task_id, att, actor_of(update))
        await msg.reply_text(f"📎 Attached to #{task_id}.")
        return
    if msg.chat.type != ChatType.PRIVATE:
        return
    uploads = context.user_data.setdefault("uploads", {})
    key = str(msg.message_id)
    uploads[key] = {**att, "caption": caption}
    for old in list(uploads)[:-20]:
        uploads.pop(old, None)
    await msg.reply_text(
        f"📎 Received <b>{html.escape(att.get('file_name') or att['kind'])}</b>. What should I do with it?",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.upload_choice(key),
    )


# ----------------------------------------------------------------- buttons
def _choice_value_to_name(svc: TaskService, field: str, value: str) -> str:
    """Convert an id from a choice button into the text the services expect."""
    if field in ("priority", "status"):
        return value
    if value == "0":
        return "none"
    vid = int(value)
    if field == "owner":
        row = svc.db.conn.execute("SELECT name FROM owners WHERE id = ?", (vid,)).fetchone()
    elif field == "project":
        row = svc.db.conn.execute("SELECT name FROM projects WHERE id = ?", (vid,)).fetchone()
    elif field == "feature":
        row = svc.db.conn.execute("SELECT name FROM features WHERE id = ?", (vid,)).fetchone()
    elif field == "folder":
        row = svc.db.conn.execute("SELECT name FROM folders WHERE id = ?", (vid,)).fetchone()
    else:
        raise ServiceError("Invalid choice")
    if not row:
        raise ServiceError("That option no longer exists")
    return row[0]


@restricted
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    svc = svc_of(context)
    actor = actor_of(update)
    parts = data.split(":")
    head = parts[0]

    if head == "noop":
        await q.answer()
        return

    # ---------------------------------------------------------- tasks
    if head == "t":
        task = svc.require_task(int(parts[1]))
        await q.answer()
        await edit_or_reply(update, views.task_card(svc, task), kb.task_actions(task))
    elif head == "tm":
        task = svc.require_task(int(parts[1]))
        await q.answer()
        await edit_or_reply(update, views.task_card(svc, task) + "\n\n✏️ <b>Choose a field to edit</b>",
                            kb.task_edit_menu(task.id))
    elif head == "tf":
        task_id, field = int(parts[1]), parts[2]
        task = svc.require_task(task_id)
        await q.answer()
        if field in kb.CHOICE_FIELDS:
            options = kb.choices(svc.db, field, task.project_id)
            if field == "feature" and len(options) == 1:
                raise ServiceError(f"Project '{task.project}' has no features. Add one with /feature add")
            await edit_or_reply(update, f"#{task_id} {html.escape(task.title)}\n\nChoose <b>{field}</b>:",
                                kb.choice_keyboard("tv", str(task_id), field, options, f"tm:{task_id}"))
        else:
            set_awaiting(context, kind="task_field", task_id=task_id, field=field)
            await q.message.reply_text(f"#{task_id} — {FIELD_HINTS.get(field, 'Send the new value.')} (or /cancel)",
                                       parse_mode=ParseMode.HTML)
    elif head == "tv":
        task_id, field, value = int(parts[1]), parts[2], parts[3]
        _, spawned = svc.set_field(task_id, field, _choice_value_to_name(svc, field, value), actor)
        task = svc.require_task(task_id)
        await q.answer("Saved")
        await edit_or_reply(update, views.task_card(svc, task), kb.task_actions(task))
        for t in spawned:
            await q.message.reply_text(f"🔁 Next occurrence created: #{t.id} due {format_date(t.deadline)}")
    elif head == "ta":
        task_id, action = int(parts[1]), parts[2]
        task = svc.require_task(task_id)
        if action == "remind":
            set_awaiting(context, kind="remind", task_id=task_id)
            await q.answer()
            await q.message.reply_text(
                f"⏰ Reminder for #{task_id}: send e.g. <code>-1d</code>, <code>-2h</code> (before deadline), "
                "<code>in 3h</code>, <code>25/11/2026 09:00</code> or <code>tomorrow 10:00</code> (or /cancel)",
                parse_mode=ParseMode.HTML,
            )
        elif action == "link":
            set_awaiting(context, kind="link", task_id=task_id)
            await q.answer()
            await q.message.reply_text(f"🗂 Send the Google Drive link for #{task_id}, optionally followed by a title "
                                       "(or /cancel).")
        elif action == "sub":
            set_awaiting(context, kind="subtask", task_id=task_id)
            await q.answer()
            await q.message.reply_text(f"☑️ Send the subtask title for #{task_id} (or /cancel).")
        elif action == "note":
            set_awaiting(context, kind="note", task_id=task_id)
            await q.answer()
            await q.message.reply_text(f"📝 Send the note for #{task_id} (or /cancel).")
        elif action == "files":
            await q.answer()
            await send_files(q.message, svc, task_id)
        elif action == "history":
            await q.answer()
            await reply(q.message, history_text(svc, task_id))
        elif action == "delete":
            await q.answer()
            await edit_or_reply(update, f"Delete #{task.id} “{html.escape(task.title)}” and all its subtasks, "
                                        "links, files and notes?", kb.confirm_delete(task_id))
        elif action == "delok":
            svc.delete_task(task_id)
            await q.answer("Deleted")
            await edit_or_reply(update, f"🗑 Task #{task_id} deleted.")

    # ---------------------------------------------------------- drafts
    elif head == "dc":
        did = parts[1]
        draft = get_draft(context, did)
        task = svc.create_from_draft(draft, q.message.chat_id, update.effective_user.id, actor)
        context.user_data.get("drafts", {}).pop(did, None)
        await q.answer("Task created")
        await edit_or_reply(update, "✅ Task created\n\n" + views.task_card(svc, task), kb.task_actions(task))
    elif head == "dx":
        context.user_data.get("drafts", {}).pop(parts[1], None)
        await q.answer("Cancelled")
        await edit_or_reply(update, "❌ Task creation cancelled.")
    elif head == "de":
        draft = get_draft(context, parts[1])
        await q.answer()
        await edit_or_reply(update, views.draft_card(draft) + "\n\n✏️ <b>Choose a field to edit</b>",
                            kb.draft_edit_menu(parts[1]))
    elif head == "df":
        did, field = parts[1], parts[2]
        draft = get_draft(context, did)
        await q.answer()
        if field in kb.CHOICE_FIELDS:
            project_id = None
            if field == "feature":
                p = svc.db.get_project(draft.project)
                project_id = p["id"] if p else None
            options = kb.choices(svc.db, field, project_id)
            if field == "feature" and len(options) == 1:
                raise ServiceError(f"Project '{draft.project}' has no features. Add one with /feature add")
            await edit_or_reply(update, views.draft_card(draft) + f"\n\nChoose <b>{field}</b>:",
                                kb.choice_keyboard("dv", did, field, options, f"de:{did}"))
        else:
            set_awaiting(context, kind="draft_field", draft=did, field=field)
            await q.message.reply_text(FIELD_HINTS.get(field, "Send the new value.") + " (or /cancel)",
                                       parse_mode=ParseMode.HTML)
    elif head == "dv":
        did, field, value = parts[1], parts[2], parts[3]
        draft = get_draft(context, did)
        svc.set_draft_field(draft, field, _choice_value_to_name(svc, field, value))
        save_draft(context, did, draft)
        await q.answer("Saved")
        await edit_or_reply(update, views.draft_card(draft) + "\n\n✏️ <b>Choose a field to edit</b>",
                            kb.draft_edit_menu(did))

    # ---------------------------------------------------------- uploads
    elif head in ("fn", "fa", "fx"):
        key = parts[1]
        uploads = context.user_data.get("uploads", {})
        att = uploads.get(key)
        if not att:
            raise ServiceError("This upload has expired; send the file again.")
        if head == "fx":
            uploads.pop(key, None)
            await q.answer()
            await edit_or_reply(update, "OK, file ignored.")
        elif head == "fn":
            uploads.pop(key, None)
            await q.answer()
            caption = att.pop("caption", "")
            await propose_task(q.message, context, caption, attachment=att)
        else:
            set_awaiting(context, kind="attach_upload", key=key)
            await q.answer()
            await q.message.reply_text("Send the id of the task to attach this file to (or /cancel).")

    # ---------------------------------------------------------- calendar
    elif head == "cal":
        year, month = int(parts[1]), int(parts[2])
        today = svc.today()
        await q.answer()
        try:
            await q.edit_message_reply_markup(
                kb.calendar_keyboard(year, month, svc.month_counts(year, month), today))
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
    elif head == "cald":
        d = date.fromisoformat(parts[1])
        tasks = svc.tasks_between(d, d)
        await q.answer()
        await reply(q.message, views.task_list(f"📅 {d.strftime('%A %d/%m/%Y')}", tasks, svc.today(),
                                               "No open tasks this day."), kb.open_tasks_keyboard(tasks))
    else:
        await q.answer("Unknown action")


# ------------------------------------------------------------------- jobs
async def job_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    for rem, task in svc.due_reminders():
        text = "⏰ <b>Reminder</b>\n\n" + views.task_card(svc, task)
        delivered = False
        for chat_id in svc.reminder_recipients(task):
            try:
                await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                               disable_web_page_preview=True, reply_markup=kb.task_actions(task))
                delivered = True
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not deliver reminder %s to %s: %s", rem.id, chat_id, exc)
        if delivered or not svc.reminder_recipients(task):
            svc.db.mark_reminder_sent(rem.id)


async def job_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = svc_of(context)
    today = svc.today()
    text = views.digest_text(svc)
    for chat in svc.db.digest_chats():
        if chat["last_digest"] == today.isoformat():
            continue
        if text:
            try:
                await context.bot.send_message(chat["chat_id"], text, parse_mode=ParseMode.HTML,
                                               disable_web_page_preview=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not send digest to %s: %s", chat["chat_id"], exc)
                continue
        svc.db.mark_digest_sent(chat["chat_id"], today)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. The error has been logged.")
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------- app
async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])


def build_application(cfg: Config, svc: Optional[TaskService] = None, bot=None) -> Application:
    """Build the bot. `svc` and `bot` can be injected for tests."""
    if not cfg.token and bot is None:
        raise SystemExit("TASKBOT_TOKEN is not set")
    if not cfg.allowed_users:
        log.warning("TASKBOT_ALLOWED_USERS is empty: nobody can use the bot. Use /whoami to get your id.")
    svc = svc or TaskService(Database(cfg.db_path), cfg)
    builder = Application.builder().post_init(_post_init)
    builder = builder.bot(bot) if bot is not None else builder.token(cfg.token)
    app = builder.build()
    app.bot_data["svc"] = svc
    app.bot_data["cfg"] = cfg

    commands = {
        "start": cmd_start, "help": cmd_help, "cancel": cmd_cancel, "new": cmd_new, "task": cmd_task,
        "list": cmd_list, "all": cmd_all, "today": cmd_today, "week": cmd_week, "nextweek": cmd_nextweek,
        "overdue": cmd_overdue, "nodate": cmd_nodate, "high": cmd_high, "matrix": cmd_matrix,
        "calendar": cmd_calendar, "search": cmd_search, "stats": cmd_stats, "set": cmd_set,
        "status": cmd_status, "prio": cmd_prio, "deadline": cmd_deadline, "assign": cmd_assign,
        "repeat": cmd_repeat, "done": cmd_done, "move": cmd_move, "tag": cmd_tag, "tags": cmd_tags,
        "remind": cmd_remind, "unremind": cmd_unremind, "link": cmd_link, "unlink": cmd_unlink,
        "attach": cmd_attach, "detach": cmd_detach, "files": cmd_files, "sub": cmd_sub, "note": cmd_note,
        "notes": cmd_notes, "history": cmd_history, "delete": cmd_delete, "owners": cmd_owners,
        "owner": cmd_owner, "projects": cmd_projects, "project": cmd_project, "features": cmd_features,
        "feature": cmd_feature, "folders": cmd_folders, "folder": cmd_folder, "digest": cmd_digest,
        "export": cmd_export, "whoami": cmd_whoami,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(job_reminders, interval=cfg.reminder_interval, first=10)
    app.job_queue.run_daily(job_digest, time=time(cfg.digest_time.hour, cfg.digest_time.minute,
                                                   tzinfo=cfg.timezone))
    return app
