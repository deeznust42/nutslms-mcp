# Nuts LMS MCP

Give your AI agent your university LMS. Ask Claude, Codex, Cursor or any
other agent what's due this week, have it read the lab manual, check your
grades, pull last year's slides, or submit your assignment, all in plain
English.

Part of [Deez Nust](https://deeznust.com). Built for NUST's LMS
(`lms.nust.edu.pk`), and it works with **any Moodle** site: set `MOODLE_URL`.

```
$ moodle-mcp agenda
as of 2026-09-20  ·  next 30 days  ·  16 documents read

  2026-09-18 18:59  CS-101   Lab 01 Manual + Submission
     submitted/notgraded
  2026-09-22        HU-200   First Graded Activity
     found in First Graded Activity.docx (high confidence)
  2026-10-20        HU-200   Group Assignment 1
     found in Group Assignment 1.docx (high confidence)
```

## What you can ask it

Once it's connected, just talk to your agent:

- "What's due this week? Include anything buried in the course files."
- "Summarise the Lab 4 manual and tell me what I actually have to submit."
- "Did my OOP assignment go through? Has it been graded?"
- "What are my grades so far in every course?"
- "What did instructors upload or announce in the last 3 days?"
- "Find every past paper in Data Structures and download them to ~/exams."
- "Search all my courses for anything about the midterm syllabus."
- "Make a study plan for this week from my deadlines and the lecture slides."
- "Submit ~/labs/lab4.pdf to the Lab 4 assignment." (needs writing turned on, see below)

The agent picks the right tool by itself. For anything the built-in tools
don't cover, it can call any of Moodle's several hundred web-service functions
directly, so in practice it can do whatever the Moodle app can.

## Setup (2 minutes)

You need [uv](https://docs.astral.sh/uv/getting-started/installation/)
(one line to install), then:

**1. Install**

```bash
uv tool install git+https://github.com/deeznust42/nutslms-mcp
```

**2. Log in once**

```bash
moodle-mcp login
```

It asks for your LMS username and password, then caches a token in
`~/.cache/moodle-mcp`. Your password is not stored. Not at NUST? Put your
site's address in front:
`MOODLE_URL=https://lms.your-university.edu moodle-mcp login`

**3. Connect your agent** (pick yours)

Claude Code:

```bash
claude mcp add moodle -- moodle-mcp serve
```

Codex:

```bash
codex mcp add moodle -- moodle-mcp serve
```

Claude Desktop, Cursor, Windsurf, or any other MCP client: add this to its
MCP config file (Claude Desktop: Settings → Developer → Edit Config; Cursor:
`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "moodle": {
      "command": "moodle-mcp",
      "args": ["serve"]
    }
  }
}
```

If your app can't find `moodle-mcp`, use the full path that
`which moodle-mcp` prints. Not at NUST? Add
`"env": { "MOODLE_URL": "https://lms.your-university.edu" }`, or `--env
MOODLE_URL=...` on the `claude`/`codex` command.

Restart the agent and ask it "what's due this week?".

## Let it submit (optional)

By default the agent can read everything but **change nothing**. To let it
submit assignments or do other things that change your LMS, turn writing on:

```bash
claude mcp add moodle --env MOODLE_ALLOW_WRITE=1 -- moodle-mcp serve
codex mcp add moodle --env MOODLE_ALLOW_WRITE=1 -- moodle-mcp serve
```

(or `"MOODLE_ALLOW_WRITE": "1"` under `env` in the JSON config). Even then,
submitting needs the agent to confirm the exact files with you first, because
a submission becomes your graded work of record.

## Use it without an agent

The same thing works as a plain terminal tool:

```bash
moodle-mcp agenda              # everything due, incl. dates found in documents
moodle-mcp courses             # your courses and their ids
moodle-mcp contents 1234       # outline of one course
moodle-mcp assignments         # assignments and due dates
moodle-mcp grades              # grades so far
moodle-mcp news --days 3       # what changed recently
moodle-mcp read "lab 4"        # read a course file as text
moodle-mcp search midterm --deep
```

Add `--json` to any command for raw output.

## Tools the agent gets

| Tool | What it does |
|---|---|
| `whoami` | Confirm the connection and account |
| `list_courses` | Enrolled courses with ids |
| **`agenda`** | **Everything due, including dates found inside documents** |
| `course_contents` | Sections, activities and files for one course |
| `find_files` | Locate files by partial name |
| `read_document` | Any course file as plain text |
| `download_file` | Save a file to disk |
| `search_lms` | Search material; `deep=True` searches inside documents |
| `list_assignments` | Assignments with due dates |
| `assignment_status` | Submitted? graded? which files? |
| `get_grades` | Grades so far |
| `announcements` | Recent forum announcements |
| `whats_new` | Material changed in the last N days |
| `call_api` | Any Moodle web-service function (writes need `MOODLE_ALLOW_WRITE=1`) |
| `submit_assignment` | Upload files and submit for grading (needs `MOODLE_ALLOW_WRITE=1`) |

It reads `.docx`, `.pptx`, `.xlsx`, `.pdf`, old `.doc`/`.ppt` and plain text.

## Why the agenda finds more than the LMS shows

Moodle's calendar only knows about work entered as a Moodle activity. A lot of
instructors just upload a Word file or slide deck that says "submit by Friday",
so that deadline never shows up on your dashboard.

`agenda` merges the assignment list, the calendar, and the **text inside every
course document**. Dates it finds in documents are marked `in document`, with
a confidence level and the sentence they came from, so you can check them.
On the account this was built against, the LMS showed zero upcoming events;
reading the documents found three real graded deliverables.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `MOODLE_URL` | `https://lms.nust.edu.pk` | Your Moodle site root |
| `MOODLE_USER` / `MOODLE_PASS` | none | Log in without the prompt (e.g. on a server) |
| `MOODLE_TOKEN` | none | Use an existing Moodle mobile token directly |
| `MOODLE_ALLOW_WRITE` | off | `1` lets the agent change things and submit |

## Privacy and safety

- Everything runs on your own computer and talks only to your university's
  LMS. There is no server in the middle and nothing is sent anywhere else.
- It signs in through Moodle's official mobile-app web service, the same one
  the Moodle phone app uses. If your university has turned that off, login will
  say so.
- The token lives in `~/.cache/moodle-mcp`. Delete that folder to log out.
  Changing your LMS password also revokes it.
- Read-only unless you turn writing on.

## Caveats

- Dates found in documents are a best-effort read of prose. Open the document
  before you rely on one; `agenda` gives you the evidence to check.
- Dates like `05-09-2026` are read day first (5 September), which is right
  outside the US.
- `agenda` reads every course document, so it is slow. Each file is downloaded
  to a temp folder, read and deleted straight away, so nothing piles up on disk.

## For developers

```bash
git clone https://github.com/deeznust42/nutslms-mcp && cd nutslms-mcp
uv sync && uv run pytest
```

Two fixes for things that break other Moodle clients on real campus servers:

- **Incomplete TLS chains.** Many campus servers omit their intermediate
  certificate. Instead of turning verification off, this fetches the missing
  intermediate from the certificate's AIA URL, caches it, and retries with
  verification still on (and re-fetches if the cached one goes stale).
- **Responses that aren't quite JSON.** Some sites print HTML before the JSON
  body (NUST's does on every call). Parsing starts at the first JSON character.

## Licence

MIT.
