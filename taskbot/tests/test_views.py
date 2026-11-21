from datetime import date

from taskbot import keyboards as kb
from taskbot import views
from taskbot.models import Status


def test_task_card_and_list(svc):
    draft = svc.new_draft("Write <spec>", urls=["https://drive.google.com/a"], tags=["work"])
    t = svc.create_from_draft(draft, 1, 1, "me")
    svc.add_subtask(t.id, "child", 1, 1, "me")
    svc.add_reminder(t.id, "-1d", "me")
    svc.db.add_note(t.id, "hello", "me")
    card = views.task_card(svc, svc.require_task(t.id))
    assert "Write &lt;spec&gt;" in card
    assert "9-Undefined" in card and "0-To_Start" in card and "27/11/2026" in card
    assert "Subtasks (0/1)" in card and "Reminders" in card and "#work" in card
    chunks = views.task_list("Open", svc.db.find_tasks(), svc.today())
    assert len(chunks) == 1 and "#1" in chunks[0]


def test_overdue_badge(svc):
    t = svc.create_from_draft(svc.new_draft("x"), 1, 1, "me")
    svc.set_field(t.id, "deadline", "01/11/2026", "me")
    assert "overdue" in views.deadline_badge(svc.require_task(t.id), svc.today())
    svc.set_status(t.id, Status.COMPLETED, "me")
    assert "overdue" not in views.deadline_badge(svc.require_task(t.id), svc.today())


def test_chunking():
    chunks = views.chunk_lines("H", ["x" * 1000] * 10)
    assert len(chunks) > 1 and all(len(c) <= views.MAX_MESSAGE for c in chunks)


def test_draft_card(svc):
    text = views.draft_card(svc.new_draft("Hello"))
    assert "Priority:</b> 9-Undefined" in text and "Owner:</b> PNC" in text
    assert "Deadline:</b> 27/11/2026" in text and "Project:</b> No PROJECT" in text


def test_eisenhower_and_digest(svc):
    d = svc.new_draft("urgent")
    svc.set_draft_field(d, "priority", "0")
    svc.set_draft_field(d, "deadline", "today")
    svc.create_from_draft(d, 1, 1, "me")
    assert "Q1" in views.eisenhower_text(svc, svc.eisenhower())[0]
    assert "Due today" in views.digest_text(svc)


def test_calendar_keyboard_callback_sizes(svc):
    markup = kb.calendar_keyboard(2026, 11, {date(2026, 11, 20): 2}, date(2026, 11, 20))
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "[20•2]" in labels
    for row in markup.inline_keyboard:
        for b in row:
            assert len(b.callback_data.encode()) <= 64


def test_choice_keyboards(svc):
    for field in kb.CHOICE_FIELDS:
        opts = kb.choices(svc.db, field, svc.db.default_project()["id"])
        markup = kb.choice_keyboard("tv", "123456", field, opts, "tm:123456")
        for row in markup.inline_keyboard:
            for b in row:
                assert len(b.callback_data.encode()) <= 64
