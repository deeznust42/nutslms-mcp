"""MCP server exposing a Moodle LMS to any agent that speaks MCP."""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from typing import Any

try:  # MCP SDK v1
    from mcp.server.fastmcp import FastMCP as _Server
except ModuleNotFoundError:  # v2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server

from .client import Moodle, MoodleError
from . import lms

mcp = _Server("moodle")
_m: Moodle | None = None


def M() -> Moodle:
    global _m
    if _m is None:
        _m = Moodle()
        _m.ensure_auth()
    return _m


def ok(data: Any) -> str:
    return json.dumps(data, indent=2, default=str, ensure_ascii=False)


def guard(fn):
    """Surface Moodle errors as readable text instead of tracebacks.

    functools.wraps matters here beyond cosmetics: the MCP SDK derives each
    tool's input schema by inspecting the signature, and wraps keeps that
    introspection pointing at the real function.
    """
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except MoodleError as e:
            return f"Moodle error: {e}"
        except Exception as e:  # noqa: BLE001
            return f"Error: {type(e).__name__}: {e}"
    return wrapper


WRITES_ALLOWED = os.environ.get("MOODLE_ALLOW_WRITE", "").lower() in ("1", "true", "yes")


# --------------------------------------------------------------------------
# Orientation
# --------------------------------------------------------------------------

@mcp.tool()
@guard
def whoami() -> str:
    """Who is logged in, which Moodle site, and how many API functions it exposes.

    Call this first to confirm the connection works."""
    return ok(lms.whoami(M()))


@mcp.tool()
@guard
def list_courses() -> str:
    """List the courses the user is enrolled in, with their numeric course ids.

    Most other tools take a courseid from here."""
    return ok(lms.courses(M()))


# --------------------------------------------------------------------------
# The headline tool
# --------------------------------------------------------------------------

@mcp.tool()
@guard
def agenda(days: int = 30, deep: bool = True) -> str:
    """Everything the user owes, from every surface that carries a date.

    This is the tool to reach for when asked "what's due", "what's coming up",
    "am I behind", or "what did I miss".

    It combines three sources:
      - the assignment API (real Moodle assignments, with submission status),
      - the calendar,
      - and, when deep=True, the text inside course documents.

    That last source matters: instructors very often upload a Word or
    PowerPoint handout instead of creating a Moodle activity, which means the
    deadline exists only as prose inside a file and is invisible to the
    calendar. Those items come back with kind="in document", a confidence
    rating, and the surrounding sentence as evidence so the claim can be
    checked. Treat them as leads to confirm, not as certainties.

    deep=True downloads and parses course files, so the first call is slow;
    files are cached afterwards. Set deep=False for a fast, shallow check."""
    return ok(lms.agenda(M(), days=days, deep=deep))


# --------------------------------------------------------------------------
# Course material
# --------------------------------------------------------------------------

@mcp.tool()
@guard
def course_contents(courseid: int) -> str:
    """Full outline of one course: sections, activities, and attached files.

    Use this to see what material exists before reading any of it. File entries
    include a fileurl that read_document and download_file accept."""
    return ok(lms.contents(M(), courseid))


@mcp.tool()
@guard
def find_files(name: str = "", courseid: int = 0, cmid: int = 0) -> str:
    """Locate course files by partial filename or activity name.

    Matches case-insensitively against both the file name and the name of the
    activity holding it. Returns fileurls for read_document / download_file."""
    return ok(lms.find_file(M(), cmid=cmid or None, name=name or None,
                            courseid=courseid or None))


@mcp.tool()
@guard
def read_document(fileurl: str, filename: str = "", max_chars: int = 20000) -> str:
    """Read a course file as plain text.

    Handles .docx, .pptx, .pdf, .xlsx, legacy .doc/.ppt, and plain text. Use it
    to read lab manuals, handouts and lecture slides directly. Get fileurl from
    course_contents or find_files."""
    return ok(lms.read_file(M(), fileurl, filename or None, max_chars=max_chars))


@mcp.tool()
@guard
def download_file(fileurl: str, dest: str, filename: str = "") -> str:
    """Download a course file to a path on disk, authenticating automatically."""
    out = lms.save_file(M(), fileurl, Path(dest), filename or None)
    return ok({"saved_to": str(out), "bytes": out.stat().st_size})


@mcp.tool()
@guard
def search_lms(query: str, deep: bool = False) -> str:
    """Search across course material for a phrase.

    deep=False searches activity names and descriptions only (fast).
    deep=True also reads inside course documents and returns matching snippets."""
    return ok(lms.search(M(), query, deep=deep))


# --------------------------------------------------------------------------
# Assignments, grades, news
# --------------------------------------------------------------------------

@mcp.tool()
@guard
def list_assignments(courseid: int = 0) -> str:
    """List real Moodle assignments with due dates and cutoffs.

    Note this only sees work entered as a Moodle assignment activity. For the
    complete picture including deadlines buried in documents, use agenda."""
    return ok(lms.assignments(M(), [courseid] if courseid else None))


@mcp.tool()
@guard
def assignment_status(assignid: int) -> str:
    """Submission and grading status for one assignment.

    Shows whether it was submitted, when, which files, and any grade.
    Get assignid from list_assignments."""
    return ok(lms.submission_status(M(), assignid))


@mcp.tool()
@guard
def get_grades(courseid: int = 0) -> str:
    """Grades received so far. Omit courseid for every enrolled course."""
    return ok(lms.grades(M(), courseid or None))


@mcp.tool()
@guard
def announcements(limit: int = 10) -> str:
    """Recent announcement-forum posts across all courses, newest first."""
    return ok(lms.announcements(M(), limit=limit))


@mcp.tool()
@guard
def whats_new(days: int = 7) -> str:
    """Course material added or changed in the last N days.

    This is the polling backbone for a watcher: run it on a schedule, then feed
    anything new to read_document to see whether it contains a deadline."""
    return ok(lms.whats_new(M(), days=days))


# --------------------------------------------------------------------------
# Escape hatch + writes
# --------------------------------------------------------------------------

READ_HINTS = ("get_", "is_", "view_", "search_", "check_", "_get_", "list_")


@mcp.tool()
@guard
def call_api(wsfunction: str, params_json: str = "{}") -> str:
    """Call any Moodle web-service function directly.

    An escape hatch for anything the other tools do not cover; a typical Moodle
    exposes several hundred functions. params_json is a JSON object, and lists
    are converted to Moodle's bracket syntax automatically, so
    {"courseids": [12, 15]} becomes courseids[0]=12&courseids[1]=15.

    Functions that appear to modify data are refused unless the server is
    started with MOODLE_ALLOW_WRITE=1."""
    params = json.loads(params_json) if params_json.strip() else {}
    looks_read = any(h in wsfunction for h in READ_HINTS)
    if not looks_read and not WRITES_ALLOWED:
        return (f"Refused: '{wsfunction}' may modify data and "
                "MOODLE_ALLOW_WRITE is not enabled on this server.")
    return ok(M().call(wsfunction, **params))


@mcp.tool()
@guard
def submit_assignment(assignid: int, filepaths: str, confirm: bool = False) -> str:
    """Upload files to an assignment and submit them for grading.

    filepaths is a comma-separated list of local paths. This is irreversible in
    the sense that it becomes the graded submission of record, so it requires
    confirm=True and the server must run with MOODLE_ALLOW_WRITE=1.

    Always show the user exactly which files will go to which assignment and
    get their explicit agreement before calling this."""
    if not WRITES_ALLOWED:
        return ("Refused: submitting is disabled. Start the server with "
                "MOODLE_ALLOW_WRITE=1 to enable it.")
    if not confirm:
        return ("Refused: set confirm=True only after the user has explicitly "
                "approved submitting these exact files.")
    m = M()
    paths = [Path(p.strip()).expanduser() for p in filepaths.split(",") if p.strip()]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        return f"These files do not exist: {missing}"

    itemid = 0
    for p in paths:
        res = _upload(m, p, itemid)
        itemid = res.get("itemid", itemid)
    m.call("mod_assign_save_submission", assignmentid=assignid,
           plugindata={"files_filemanager": itemid})
    m.call("mod_assign_submit_for_grading", assignmentid=assignid,
           acceptsubmissionstatement=1)
    return ok({"submitted": True, "assignid": assignid,
               "files": [p.name for p in paths],
               "status": lms.submission_status(m, assignid)})


def _upload(m: Moodle, path: Path, itemid: int = 0) -> dict:
    """Push one file to the user's draft area via webservice/upload.php."""
    import urllib.request
    boundary = "----moodlemcp" + os.urandom(8).hex()
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
            f"\r\n\r\n{value}\r\n".encode())

    field("token", m.token)
    field("itemid", str(itemid))
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file_1\"; "
        f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream"
        f"\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{m.url}/webservice/upload.php", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    out = m._parse(m._open(req))
    if isinstance(out, dict) and out.get("error"):
        raise MoodleError(out["error"])
    return out[0] if isinstance(out, list) and out else {}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
