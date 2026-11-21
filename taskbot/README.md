# TaskBot — Telegram task manager

A standalone Telegram bot to manage tasks, independent of the TGTG scanner in this repository.
Python 3.10+, `python-telegram-bot` and SQLite (one file, no external services).

## Quick start

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Install and configure:

   ```bash
   pip install -r taskbot/requirements.txt
   cp taskbot/.env.sample taskbot/.env   # edit token and allowed users
   set -a; . taskbot/.env; set +a
   python -m taskbot
   ```

   Send `/whoami` to the bot to get your Telegram id for `TASKBOT_ALLOWED_USERS`.

   With Docker: `cd taskbot && docker compose up -d --build`.

3. Tests: `pip install pytest && python -m pytest taskbot/tests`

## Data model

```
Project ─┬─ Feature ─┬─ Task ─┬─ Subtask ─ …
         │           │        └─ links (source / Google Drive / other), attachments, reminders, notes, tags, history
         └─ (tasks without feature)
Folder   — independent classification (e.g. Work, Home), orthogonal to Project/Feature
Owner    — managed list (/owner); default "PNC"; optionally linked to a Telegram user to receive reminders
```

| Field    | Values                                                                                   | Default       |
|----------|------------------------------------------------------------------------------------------|---------------|
| Priority | 0-Critical, 1-High, 2-Medium, 3-Low, 9-Undefined                                         | 9-Undefined   |
| Status   | 0-To_Start, 1-In_Progress, 2-Postponed, 3-Canceled, 4-Waiting_for_Other, 5-Completed, 9-Suspended | 0-To_Start |
| Owner    | from `/owners`                                                                           | PNC           |
| Deadline | DD/MM/YYYY or none                                                                       | today + 7     |
| Project  | from `/projects`                                                                         | No PROJECT    |

## How to use

* **Create** — in a private chat, send any text, link or file. The bot shows a draft with the defaults and buttons
  *Create* / *Edit fields* / *Cancel*. Hashtags in the text become tags. For a bare link the page title is used
  as task title. In groups use `/new …`.
* **Files** — send a file: choose *New task with this file* or *Attach to existing task*. A file with caption `#12`
  is attached to task 12 directly. `/files 12` sends them back. Files stay on Telegram's servers (only the file id
  is stored).
* **Google Drive** — `/link 12 https://drive.google.com/… [title]` (only drive.google.com / docs.google.com).
* **Look up** — `/task 12` opens a card with buttons (Done, Start, Edit, Reminder, Drive link, Subtask, Files, Note,
  History, Delete).
* **Lists** — `/list` (open tasks sorted by deadline; tasks without date last), `/list all`, filters
  `owner= project= feature= folder= tag= status= priority=`; `/today` (plus overdue), `/week`, `/nextweek`
  (Monday–Sunday), `/overdue`, `/nodate`, `/search`.
* **High priority view** — `/high`: priority 0/1 with status 0, 1, 2 or 4.
* **Eisenhower Matrix** — `/matrix`: Important = priority 0/1; Urgent = deadline within `TASKBOT_URGENT_DAYS`
  (default 3) or overdue. `/matrix Q1` lists one quadrant.
* **Calendar** — `/calendar`: month grid, days with open tasks marked `•n`; tap a day to list its tasks.
* **Edit / move** — buttons on the card, or `/set <id> <field> <value>`, `/move <id> project="X" feature="Y"`
  (subtasks move too), `/tag <id> +a -b`, shortcuts `/done`, `/status`, `/prio`, `/deadline`, `/assign`.
* **Reminders** — `/remind <id> -1d` (before the deadline, counted from `TASKBOT_DEADLINE_TIME`, default 09:00;
  they follow the deadline if it changes), `in 2h`, `25/11/2026 14:30`, `tomorrow 10:00`; several per task.
  Sent to the chat where the task was created and to the owner's linked Telegram account.
* **Setup** — `/owner add|link|rename|del`, `/project add|rename|del`, `/feature add|del <project> | <name>`,
  `/folder add|del`.

### Extras

* Subtasks with progress (`/sub`), recurring tasks (`/repeat <id> weekly`: completing it creates the next occurrence
  with tags, links and relative reminders), notes (`/note`, `/notes`), change history (`/history`), daily digest
  (`/digest on`), statistics (`/stats`), CSV export (`/export`), multi-user access via `TASKBOT_ALLOWED_USERS`.

## Configuration

See [`.env.sample`](.env.sample). Drafts and pending inputs live in memory, so a draft that was not confirmed is
lost if the bot restarts. Tasks themselves are always in the database.
