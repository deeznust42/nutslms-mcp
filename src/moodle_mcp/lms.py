"""High-level operations composed from raw web-service calls.

The MCP server and the CLI are both thin shells over this module.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .client import Moodle, MoodleError
from .deadlines import find_dates, upcoming
from .extract import extract

SCAN_EXT = {".docx", ".pptx", ".pdf", ".doc", ".ppt", ".txt", ".md", ".xlsx"}


def _ts(v: Any) -> str | None:
    if not v:
        return None
    return dt.datetime.fromtimestamp(int(v)).strftime("%Y-%m-%d %H:%M")


def _clean(s: str | None) -> str:
    """Moodle double-escapes names in places; undo it and drop tags."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#039;", "'"), ("&nbsp;", " ")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------

def whoami(m: Moodle) -> dict:
    s = m.siteinfo
    return {
        "fullname": s.get("fullname"),
        "username": s.get("username"),
        "userid": s.get("userid"),
        "site": s.get("sitename"),
        "url": s.get("siteurl"),
        "moodle_release": s.get("release"),
        "functions_available": len(s.get("functions", [])),
    }


def courses(m: Moodle) -> list[dict]:
    out = []
    for c in m.call("core_enrol_get_users_courses", userid=m.userid):
        out.append({
            "id": c["id"],
            "shortname": _clean(c.get("shortname")),
            "fullname": _clean(c.get("fullname")),
            "startdate": _ts(c.get("startdate")),
            "enddate": _ts(c.get("enddate")),
            "progress": c.get("progress"),
        })
    return out


def contents(m: Moodle, courseid: int) -> list[dict]:
    """Course tree: sections -> activities -> attached files."""
    out = []
    for s in m.call("core_course_get_contents", courseid=courseid):
        mods = []
        for mod in s.get("modules", []):
            files = [{
                "filename": f.get("filename"),
                "fileurl": f.get("fileurl"),
                "filesize": f.get("filesize"),
                "modified": _ts(f.get("timemodified")),
            } for f in (mod.get("contents") or []) if f.get("type") == "file"]
            mods.append({
                "cmid": mod.get("id"),
                "type": mod.get("modname"),
                "name": _clean(mod.get("name")),
                "url": mod.get("url"),
                "description": _clean(mod.get("description"))[:400] or None,
                "files": files,
            })
        if mods or _clean(s.get("summary")):
            out.append({
                "section": _clean(s.get("name")),
                "summary": _clean(s.get("summary"))[:400] or None,
                "modules": mods,
            })
    return out


def assignments(m: Moodle, courseids: list[int] | None = None) -> list[dict]:
    ids = courseids or [c["id"] for c in courses(m)]
    data = m.call("mod_assign_get_assignments", courseids=ids)
    out = []
    for c in data.get("courses", []):
        for a in c.get("assignments", []):
            out.append({
                "course": _clean(c.get("shortname")),
                "courseid": c.get("id"),
                "assignid": a.get("id"),
                "cmid": a.get("cmid"),
                "name": _clean(a.get("name")),
                "duedate": _ts(a.get("duedate")),
                "cutoffdate": _ts(a.get("cutoffdate")),
                "allowsubmissionsfrom": _ts(a.get("allowsubmissionsfromdate")),
                "maxattempts": a.get("maxattempts"),
                "intro": _clean(a.get("intro"))[:300] or None,
            })
    return out


def submission_status(m: Moodle, assignid: int) -> dict:
    d = m.call("mod_assign_get_submission_status", assignid=assignid)
    last = d.get("lastattempt") or {}
    sub = last.get("submission") or {}
    files = []
    for p in sub.get("plugins", []):
        for area in p.get("fileareas", []):
            for f in area.get("files", []):
                files.append({"filename": f.get("filename"),
                              "size": f.get("filesize")})
    fb = d.get("feedback") or {}
    grade = (fb.get("grade") or {}).get("grade")
    return {
        "assignid": assignid,
        "status": sub.get("status", "nosubmission"),
        "gradingstatus": last.get("gradingstatus"),
        "submitted_at": _ts(sub.get("timemodified")),
        "files": files,
        "grade": grade,
        "can_edit": last.get("canedit"),
        "can_submit": last.get("cansubmit"),
    }


def grades(m: Moodle, courseid: int | None = None) -> list[dict]:
    out = []
    ids = [courseid] if courseid else [c["id"] for c in courses(m)]
    for cid in ids:
        try:
            d = m.call("gradereport_user_get_grade_items",
                       courseid=cid, userid=m.userid)
        except MoodleError as e:
            out.append({"courseid": cid, "error": str(e)})
            continue
        for rep in d.get("usergrades", []):
            for it in rep.get("gradeitems", []):
                if it.get("graderaw") is None and it.get("gradeformatted") in ("-", "", None):
                    continue
                out.append({
                    "courseid": cid,
                    "course": _clean(rep.get("courseshortname") or ""),
                    "item": _clean(it.get("itemname") or it.get("itemtype")),
                    "grade": it.get("gradeformatted"),
                    "range": f"{it.get('grademin')}–{it.get('grademax')}",
                    "percentage": it.get("percentageformatted"),
                    "feedback": _clean(it.get("feedback"))[:300] or None,
                })
    return out


def announcements(m: Moodle, courseids: list[int] | None = None,
                  limit: int = 10) -> list[dict]:
    ids = courseids or [c["id"] for c in courses(m)]
    forums = m.call("mod_forum_get_forums_by_courses", courseids=ids)
    out = []
    for f in forums:
        try:
            d = m.call("mod_forum_get_forum_discussions",
                       forumid=f["id"], perpage=limit)
        except MoodleError:
            continue
        for disc in d.get("discussions", []):
            out.append({
                "forum": _clean(f.get("name")),
                "courseid": f.get("course"),
                "subject": _clean(disc.get("subject")),
                "author": disc.get("userfullname"),
                "posted": _ts(disc.get("created")),
                "message": _clean(disc.get("message"))[:800],
            })
    return sorted(out, key=lambda x: x["posted"] or "", reverse=True)[:limit]


def whats_new(m: Moodle, days: int = 7) -> list[dict]:
    since = int(time.time()) - days * 86400
    out = []
    for c in courses(m):
        try:
            d = m.call("core_course_get_updates_since",
                       courseid=c["id"], since=since)
        except MoodleError:
            continue
        changed = {i["id"] for i in d.get("instances", [])}
        if not changed:
            continue
        for sec in contents(m, c["id"]):
            for mod in sec["modules"]:
                if mod["cmid"] in changed:
                    out.append({
                        "course": c["shortname"],
                        "section": sec["section"],
                        "type": mod["type"],
                        "name": mod["name"],
                        "cmid": mod["cmid"],
                        "files": [f["filename"] for f in mod["files"]],
                    })
    return out


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def _filename(fileurl: str, filename: str | None) -> str:
    import urllib.parse
    name = filename or Path(fileurl.split("?")[0]).name or "download"
    return urllib.parse.unquote(name)


def fetch_text(m: Moodle, fileurl: str, filename: str | None = None) -> tuple[str, str]:
    """Download a file to a temp folder, extract its text, then delete it.

    Nothing is kept on disk, so reading course files never builds up a cache.
    """
    name = _filename(fileurl, filename)
    with tempfile.TemporaryDirectory(prefix="moodle-mcp-") as d:
        return name, extract(m.download(fileurl, Path(d) / name))


def save_file(m: Moodle, fileurl: str, dest: Path, filename: str | None = None) -> Path:
    """Download a file straight to dest (a file path or an existing folder)."""
    dest = Path(dest).expanduser()
    if dest.is_dir():
        dest = dest / _filename(fileurl, filename)
    return m.download(fileurl, dest)


def find_file(m: Moodle, cmid: int | None = None, name: str | None = None,
              courseid: int | None = None) -> list[dict]:
    """Locate files by activity id or fuzzy name across courses."""
    hits = []
    cids = [courseid] if courseid else [c["id"] for c in courses(m)]
    for cid in cids:
        for sec in contents(m, cid):
            for mod in sec["modules"]:
                if cmid and mod["cmid"] != cmid:
                    continue
                for f in mod["files"]:
                    if name and name.lower() not in (f["filename"] or "").lower() \
                       and name.lower() not in mod["name"].lower():
                        continue
                    hits.append({"courseid": cid, "section": sec["section"],
                                 "activity": mod["name"], "cmid": mod["cmid"],
                                 **f})
    return hits


def read_file(m: Moodle, fileurl: str, filename: str | None = None,
              max_chars: int = 20000) -> dict:
    name, text = fetch_text(m, fileurl, filename)
    return {
        "filename": name,
        "chars": len(text),
        "truncated": len(text) > max_chars,
        "text": text[:max_chars],
    }


def search(m: Moodle, query: str, deep: bool = False) -> list[dict]:
    """Search activity names, and with deep=True the file contents too."""
    q = query.lower()
    out = []
    for c in courses(m):
        for sec in contents(m, c["id"]):
            for mod in sec["modules"]:
                blob = f"{mod['name']} {mod.get('description') or ''}"
                if q in blob.lower():
                    out.append({"course": c["shortname"], "section": sec["section"],
                                "activity": mod["name"], "cmid": mod["cmid"],
                                "where": "activity"})
                    continue
                if not deep:
                    continue
                for f in mod["files"]:
                    if Path(f["filename"] or "").suffix.lower() not in SCAN_EXT:
                        continue
                    try:
                        text = fetch_text(m, f["fileurl"], f["filename"])[1]
                    except Exception:
                        continue
                    i = text.lower().find(q)
                    if i != -1:
                        out.append({
                            "course": c["shortname"], "section": sec["section"],
                            "activity": mod["name"], "cmid": mod["cmid"],
                            "file": f["filename"], "where": "file contents",
                            "snippet": re.sub(r"\s+", " ",
                                              text[max(0, i - 120):i + 200]),
                        })
    return out


# --------------------------------------------------------------------------
# The headline feature
# --------------------------------------------------------------------------

def agenda(m: Moodle, days: int = 30, deep: bool = True,
           ref: dt.date | None = None) -> dict:
    """Everything with a date attached, from every surface that carries one.

    Moodle's own calendar and assignment APIs only see work that was entered as
    an activity. Where instructors instead upload a handout, the date exists
    only as prose inside the file, so with deep=True we also read the course
    documents and mine them for dates.
    """
    ref = ref or dt.date.today()
    horizon = ref + dt.timedelta(days=days)
    items: list[dict] = []

    # 1. Real assignments.
    for a in assignments(m):
        if not a["duedate"]:
            continue
        d = dt.date.fromisoformat(a["duedate"][:10])
        if ref - dt.timedelta(days=7) <= d <= horizon:
            st = {}
            try:
                st = submission_status(m, a["assignid"])
            except MoodleError:
                pass
            items.append({
                "date": a["duedate"], "course": a["course"], "title": a["name"],
                "kind": "assignment", "source": "assignment API",
                "confidence": "certain",
                "status": st.get("status"), "grading": st.get("gradingstatus"),
                "overdue": d < ref and st.get("status") != "submitted",
            })

    # 2. Calendar events.
    try:
        ev = m.call("core_calendar_get_action_events_by_timesort",
                    timesortfrom=int(time.mktime(ref.timetuple())),
                    timesortto=int(time.mktime(horizon.timetuple())),
                    limitnum=50)
        for e in ev.get("events", []):
            items.append({
                "date": _ts(e.get("timesort")),
                "course": _clean((e.get("course") or {}).get("shortname")),
                "title": _clean(e.get("name")), "kind": e.get("modulename") or "event",
                "source": "calendar", "confidence": "certain",
            })
    except MoodleError:
        pass

    # 3. Dates hiding inside course documents.
    scanned = 0
    if deep:
        for c in courses(m):
            for sec in contents(m, c["id"]):
                for mod in sec["modules"]:
                    for f in mod["files"]:
                        ext = Path(f["filename"] or "").suffix.lower()
                        if ext not in SCAN_EXT:
                            continue
                        try:
                            text = fetch_text(m, f["fileurl"], f["filename"])[1]
                        except Exception:
                            continue
                        scanned += 1
                        for h in upcoming(find_dates(text, ref=ref), ref=ref,
                                          horizon_days=days):
                            if h.confidence == "low":
                                continue
                            items.append({
                                "date": h.date, "course": c["shortname"],
                                "title": mod["name"], "kind": "in document",
                                "source": f["filename"],
                                "confidence": h.confidence,
                                "evidence": h.context[:260],
                                "cmid": mod["cmid"],
                            })

    items.sort(key=lambda x: (x["date"] or "9999"))
    return {
        "as_of": ref.isoformat(),
        "window_days": days,
        "documents_scanned": scanned,
        "count": len(items),
        "items": items,
        "note": ("Items marked 'in document' were mined from prose inside "
                 "course files and are not in Moodle's calendar. Verify before "
                 "relying on them."),
    }
