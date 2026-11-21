"""End-to-end tests of the Telegram handlers against a fake Bot API."""

import asyncio
import itertools
import json

import pytest
from telegram import Update
from telegram.ext import ExtBot

from taskbot.bot import build_application
from taskbot.models import Priority, Status

USER = {"id": 1, "is_bot": False, "first_name": "Pat", "username": "pat"}
STRANGER = {"id": 999, "is_bot": False, "first_name": "Eve"}
CHAT = {"id": 1, "type": "private", "first_name": "Pat"}
BOT_USER = {"id": 42, "is_bot": True, "first_name": "TaskBot", "username": "task_bot"}


class FakeBot(ExtBot):
    """Records outgoing API calls instead of hitting Telegram."""

    def __init__(self):
        super().__init__(token="42:FAKE")
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "_ids", itertools.count(1000))

    async def _do_post(self, endpoint, data, *args, **kwargs):  # noqa: D401
        params = {}
        for k, v in data.parameters.items() if hasattr(data, "parameters") else data.items():
            params[k] = v
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            return BOT_USER
        if endpoint in ("sendMessage", "sendDocument", "sendPhoto", "editMessageText"):
            return {"message_id": next(self._ids), "date": 0, "chat": CHAT, "text": params.get("text", "")}
        return True


def sent_texts(bot):
    return [p.get("text", "") for e, p in bot.calls if e in ("sendMessage", "editMessageText")]


def last_markup(bot):
    for _e, p in reversed(bot.calls):
        if "reply_markup" in p:
            rm = p["reply_markup"]
            return json.loads(rm) if isinstance(rm, str) else rm
    return None


def find_button(bot, prefix):
    markup = last_markup(bot)
    for row in markup["inline_keyboard"]:
        for b in row:
            if b["callback_data"].startswith(prefix):
                return b["callback_data"]
    raise AssertionError(f"no button {prefix} in {markup}")


@pytest.fixture
def env(svc, cfg):
    bot = FakeBot()
    app = build_application(cfg, svc=svc, bot=bot)
    counter = itertools.count(1)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(app.initialize())

    def send(text=None, user=USER, **extra):
        uid = next(counter)
        msg = {"message_id": uid, "date": 0, "chat": CHAT, "from": user, **extra}
        if text is not None:
            msg["text"] = text
            if text.startswith("/"):
                cmd = text.split()[0]
                msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
        loop.run_until_complete(app.process_update(Update.de_json({"update_id": uid, "message": msg}, bot)))

    def press(data, user=USER):
        uid = next(counter)
        upd = {
            "update_id": uid,
            "callback_query": {
                "id": str(uid), "from": user, "chat_instance": "x", "data": data,
                "message": {"message_id": 500, "date": 0, "chat": CHAT, "from": BOT_USER, "text": "x"},
            },
        }
        loop.run_until_complete(app.process_update(Update.de_json(upd, bot)))

    yield bot, svc, send, press
    loop.run_until_complete(app.shutdown())
    loop.close()


def test_unauthorized(env):
    bot, svc, send, _ = env
    send("hello", user=STRANGER)
    assert "Not authorized" in sent_texts(bot)[-1] and "999" in sent_texts(bot)[-1]
    assert svc.db.find_tasks() == []


def test_link_creates_draft_with_defaults_then_confirm(env, monkeypatch):
    bot, svc, send, press = env
    send("Review contract https://example.com/doc #legal")
    text = sent_texts(bot)[-1]
    assert "Create a new task?" in text and "9-Undefined" in text and "PNC" in text
    assert "27/11/2026" in text and "0-To_Start" in text and "No PROJECT" in text
    press(find_button(bot, "dc:"))
    tasks = svc.db.find_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.title == "Review contract" and t.tags == ["legal"]
    assert svc.db.links(t.id)[0].url == "https://example.com/doc"


def test_edit_draft_fields_before_creating(env):
    bot, svc, send, press = env
    svc.db.add_owner("Anna")
    send("Plan offsite")
    press(find_button(bot, "de:"))
    press(find_button(bot, "df:1:priority"))
    press(find_button(bot, "dv:1:priority:1"))
    press(find_button(bot, "df:1:owner"))
    anna = svc.db.get_owner("Anna")["id"]
    press(f"dv:1:owner:{anna}")
    press("df:1:deadline")
    send("none")
    press(find_button(bot, "dc:"))
    t = svc.db.find_tasks()[0]
    assert t.priority == Priority.HIGH and t.owner == "Anna" and t.deadline is None


def test_invalid_input_keeps_waiting(env):
    bot, svc, send, press = env
    send("x")
    press("df:1:deadline")
    send("not a date")
    assert "Unrecognized date" in sent_texts(bot)[-1]
    send("25/12/2026")
    assert "25/12/2026" in sent_texts(bot)[-1]


def test_commands_on_task(env):
    bot, svc, send, press = env
    send("/project add Website")
    send("/feature add Website | Login")
    send("Fix login")
    press(find_button(bot, "dc:"))
    send("/set 1 priority 0")
    send('/move 1 project="Website" feature="Login"')
    send("/tag 1 +bug +web")
    send("/remind 1 -1d")
    send("/link 1 https://drive.google.com/file/d/xyz Mockups")
    send("/sub 1 Write tests")
    send("/note 1 started")
    send("/status 1 1")
    t = svc.require_task(1)
    assert (t.priority, t.status, t.project, t.feature) == (Priority.CRITICAL, Status.IN_PROGRESS, "Website", "Login")
    assert t.tags == ["bug", "web"]
    assert svc.db.subtasks(1)[0].title == "Write tests"
    send("/high")
    assert "Fix login" in sent_texts(bot)[-1]
    send("/matrix")
    assert "Q1" in sent_texts(bot)[-1]
    send("/project Website")
    assert "Login" in sent_texts(bot)[-1] and "Write tests" in sent_texts(bot)[-1]
    send("/done 1")
    assert "completed" in sent_texts(bot)[-1]
    send("/high")
    assert "No open high-priority" in sent_texts(bot)[-1]


def test_lists(env):
    bot, svc, send, press = env
    for text in ("/list", "/today", "/week", "/nextweek", "/overdue", "/nodate", "/calendar", "/stats",
                 "/owners", "/projects", "/folders", "/tags", "/digest", "/help"):
        send(text)
    press("cal:2026:12")
    press("cald:2026-11-20")
    assert not any("Something went wrong" in t for t in sent_texts(bot))


def test_owner_management(env):
    bot, svc, send, _ = env
    send("/owner add Luca 12345")
    assert svc.db.get_owner("Luca")["telegram_user_id"] == 12345
    send("/owner del PNC")
    assert "cannot be deleted" in sent_texts(bot)[-1]
    send("/owner del Luca")
    assert svc.db.get_owner("Luca") is None


def test_file_upload_creates_task_with_attachment(env):
    bot, svc, send, press = env
    send(document={"file_id": "DOC1", "file_unique_id": "u1", "file_name": "invoice.pdf"})
    press(find_button(bot, "fn:"))
    assert "invoice.pdf" in sent_texts(bot)[-1]
    press(find_button(bot, "dc:"))
    t = svc.db.find_tasks()[0]
    assert t.title == "invoice.pdf"
    assert svc.db.attachments(t.id)[0].file_id == "DOC1"
    # caption '#<id>' attaches directly to an existing task
    send(document={"file_id": "DOC2", "file_unique_id": "u2", "file_name": "b.pdf"}, caption=f"#{t.id}")
    assert len(svc.db.attachments(t.id)) == 2
    press(f"ta:{t.id}:files")
    assert any(e == "sendDocument" for e, _ in bot.calls)
