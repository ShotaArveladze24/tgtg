from datetime import date, datetime, timedelta, timezone

import pytest

from taskbot.models import Priority, Status
from taskbot.services import ServiceError

from .conftest import FIXED_NOW


def make(svc, title="Task", **fields):
    draft = svc.new_draft(title)
    for k, v in fields.items():
        svc.set_draft_field(draft, k, v)
    return svc.create_from_draft(draft, chat_id=100, user_id=1, actor="tester")


def test_defaults(svc):
    t = make(svc, "Read https://example.com")
    assert t.priority == Priority.UNDEFINED
    assert t.owner == "PNC"
    assert t.deadline == date(2026, 11, 27)  # T+7
    assert t.status == Status.TO_START
    assert t.project == "No PROJECT"


def test_draft_with_link_and_attachment(svc):
    draft = svc.new_draft("Spec", urls=["https://example.com", "https://drive.google.com/x"],
                          attachment={"file_id": "F1", "kind": "document", "file_name": "a.pdf"}, tags=["work"])
    t = svc.create_from_draft(draft, 1, 1, "me")
    kinds = [link.kind for link in svc.db.links(t.id)]
    assert kinds == ["source", "drive"]
    assert svc.db.attachments(t.id)[0].file_name == "a.pdf"
    assert t.tags == ["work"]


def test_unknown_owner_and_owner_list(svc):
    with pytest.raises(ServiceError):
        make(svc, owner="Mario")
    svc.db.add_owner("Mario")
    t = make(svc, owner="mario")
    assert t.owner == "Mario"


def test_no_deadline(svc):
    t = make(svc, deadline="none")
    assert t.deadline is None
    assert t.id in [x.id for x in svc.db.find_tasks()]


def test_sorting_by_deadline(svc):
    a = make(svc, "a", deadline="30/11/2026")
    b = make(svc, "b", deadline="none")
    c = make(svc, "c", deadline="21/11/2026")
    assert [t.id for t in svc.db.find_tasks()] == [c.id, a.id, b.id]


def test_periods(svc):
    make(svc, "today", deadline="20/11/2026")
    make(svc, "sunday", deadline="22/11/2026")
    make(svc, "next mon", deadline="23/11/2026")
    make(svc, "past", deadline="10/11/2026")
    done = make(svc, "done today", deadline="20/11/2026")
    svc.set_status(done.id, Status.COMPLETED, "me")
    today = svc.today()
    assert [t.title for t in svc.tasks_between(today, today)] == ["today"]
    start, end = svc.week_range(0)
    assert (start, end) == (date(2026, 11, 16), date(2026, 11, 22))
    assert [t.title for t in svc.tasks_between(start, end)] == ["today", "sunday"]
    start, end = svc.week_range(1)
    assert [t.title for t in svc.tasks_between(start, end)] == ["next mon"]
    assert [t.title for t in svc.overdue()] == ["past"]


def test_high_priority_view(svc):
    ids = {}
    for status in Status:
        ids[status] = make(svc, f"p0 {status.name}", priority="0", status=str(status.value)).id
    make(svc, "p2", priority="2")
    p1 = make(svc, "p1", priority="1")
    got = {t.id for t in svc.high_priority_open()}
    expected = {ids[s] for s in (Status.TO_START, Status.IN_PROGRESS, Status.POSTPONED, Status.WAITING_FOR_OTHER)}
    assert got == expected | {p1.id}


def test_eisenhower(svc):
    q1 = make(svc, "q1", priority="0", deadline="21/11/2026")
    q2 = make(svc, "q2", priority="1", deadline="none")
    q3 = make(svc, "q3", priority="3", deadline="10/11/2026")
    q4 = make(svc, "q4")  # undefined priority, T+7
    m = svc.eisenhower()
    assert [t.id for t in m["Q1"]] == [q1.id]
    assert [t.id for t in m["Q2"]] == [q2.id]
    assert [t.id for t in m["Q3"]] == [q3.id]
    assert [t.id for t in m["Q4"]] == [q4.id]


def test_projects_features_move(svc):
    svc.db.add_project("Website")
    p = svc.db.get_project("Website")
    svc.db.add_feature(p["id"], "Login")
    svc.db.add_project("App")
    t = make(svc, "parent", project="Website", feature="Login")
    assert (t.project, t.feature) == ("Website", "Login")
    sub = svc.add_subtask(t.id, "child", 1, 1, "me")
    assert (sub.project, sub.feature, sub.parent_id) == ("Website", "Login", t.id)
    svc.move(t.id, "App", None, "me")
    assert svc.require_task(sub.id).project == "App"
    assert svc.require_task(sub.id).feature is None
    with pytest.raises(ServiceError):
        svc.move(t.id, "App", "Login", "me")  # Login belongs to Website


def test_set_parent_cycle(svc):
    a = make(svc, "a")
    b = svc.add_subtask(a.id, "b", 1, 1, "me")
    with pytest.raises(ServiceError):
        svc.set_parent(a.id, str(b.id), "me")
    with pytest.raises(ServiceError):
        svc.set_parent(a.id, str(a.id), "me")


def test_set_field_and_tags(svc):
    t = make(svc)
    svc.set_field(t.id, "priority", "1", "me")
    svc.set_field(t.id, "status", "4", "me")
    svc.set_field(t.id, "tags", "#Alpha beta", "me")
    svc.edit_tags(t.id, ["gamma"], ["beta"], "me")
    t = svc.require_task(t.id)
    assert t.priority == Priority.HIGH and t.status == Status.WAITING_FOR_OTHER
    assert t.tags == ["alpha", "gamma"]
    with pytest.raises(ServiceError):
        svc.set_field(t.id, "colour", "red", "me")
    assert len(svc.db.history(t.id)) >= 4


def test_drive_links(svc):
    t = make(svc)
    svc.add_drive_link(t.id, "https://drive.google.com/file/d/1", "me", "Budget")
    with pytest.raises(ServiceError):
        svc.add_drive_link(t.id, "https://example.com", "me")
    assert svc.db.links(t.id)[0].title == "Budget"


def test_reminders_relative_and_absolute(svc):
    t = make(svc, deadline="22/11/2026")
    rel = svc.add_reminder(t.id, "-1d", "me")
    ab = svc.add_reminder(t.id, "in 2h", "me")
    # relative: 21/11 09:00 Rome = 08:00 UTC
    assert svc.reminder_fire_time(rel, t) == datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)
    assert svc.due_reminders() == []
    svc.current = FIXED_NOW + timedelta(hours=3)
    assert [r.id for r, _ in svc.due_reminders()] == [ab.id]
    svc.db.mark_reminder_sent(ab.id)
    svc.current = datetime(2026, 11, 21, 8, 1, tzinfo=timezone.utc)
    assert [r.id for r, _ in svc.due_reminders()] == [rel.id]
    svc.db.mark_reminder_sent(rel.id)
    # moving the deadline re-arms relative reminders
    svc.set_field(t.id, "deadline", "30/11/2026", "me")
    assert svc.due_reminders() == []
    svc.current = datetime(2026, 11, 29, 8, 0, tzinfo=timezone.utc)
    assert [r.id for r, _ in svc.due_reminders()] == [rel.id]


def test_reminder_validation(svc):
    t = make(svc, deadline="none")
    with pytest.raises(ServiceError):
        svc.add_reminder(t.id, "-1d", "me")
    with pytest.raises(ServiceError):
        svc.add_reminder(t.id, "01/01/2020 10:00", "me")


def test_completed_tasks_do_not_remind(svc):
    t = make(svc)
    svc.add_reminder(t.id, "in 1h", "me")
    svc.set_status(t.id, Status.COMPLETED, "me")
    svc.current = FIXED_NOW + timedelta(hours=2)
    assert svc.due_reminders() == []


def test_reminder_recipients(svc):
    svc.db.add_owner("Anna", 555)
    t = make(svc, owner="Anna")
    assert svc.reminder_recipients(t) == {100, 555}


def test_recurrence(svc):
    t = make(svc, "weekly report", deadline="20/11/2026", tags="report")
    svc.set_field(t.id, "recurrence", "weekly", "me")
    svc.add_reminder(t.id, "-2h", "me")
    _, spawned = svc.set_field(t.id, "status", "5", "me")
    assert len(spawned) == 1
    nxt = spawned[0]
    assert nxt.deadline == date(2026, 11, 27) and nxt.recurrence == "weekly"
    assert nxt.status == Status.TO_START and nxt.tags == ["report"]
    assert svc.db.reminders(nxt.id)[0].offset_minutes == 120
    assert svc.require_task(t.id).recurrence is None
    assert svc.require_task(t.id).completed_at is not None


def test_recurrence_requires_deadline(svc):
    t = make(svc, deadline="none")
    with pytest.raises(ServiceError):
        svc.set_field(t.id, "recurrence", "daily", "me")


def test_delete_project_moves_tasks(svc):
    svc.db.add_project("Tmp")
    t = make(svc, project="Tmp")
    p = svc.db.get_project("Tmp")
    svc.db.delete_project(p["id"], svc.db.default_project()["id"])
    assert svc.require_task(t.id).project == "No PROJECT"


def test_search_and_filters(svc):
    svc.db.add_folder("Home")
    a = make(svc, "Buy milk", folder="Home", tags="shopping")
    svc.db.add_note(a.id, "lactose free", "me")
    make(svc, "Call bank")
    from taskbot.db import TaskFilter

    assert [t.id for t in svc.db.find_tasks(TaskFilter(text="lactose"))] == [a.id]
    assert [t.id for t in svc.db.find_tasks(TaskFilter(tag="shopping"))] == [a.id]
    assert [t.id for t in svc.db.find_tasks(TaskFilter(folder_id=svc.db.get_folder("home")["id"]))] == [a.id]
    assert [t.id for t in svc.db.find_tasks(TaskFilter(owner="pnc"))] != []


def test_export_and_stats(svc):
    make(svc, "x;y")
    data = svc.export_csv().decode("utf-8-sig")
    assert data.splitlines()[0].startswith("id;title")
    assert '"x;y"' in data
    s = svc.stats()
    assert s["total"] == 1 and s["by_status"][Status.TO_START] == 1


def test_month_counts(svc):
    make(svc, deadline="05/11/2026")
    make(svc, deadline="05/11/2026")
    make(svc, deadline="05/12/2026")
    assert svc.month_counts(2026, 11) == {date(2026, 11, 5): 2}
    assert svc.month_counts(2026, 12) == {date(2026, 12, 5): 1}
