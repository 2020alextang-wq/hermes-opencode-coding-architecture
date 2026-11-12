"""Local, metadata-only Hermes request ledger. Never records conversation content."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time

_WRITE_LOCK = threading.Lock()
_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
_LABEL = re.compile(r"[\w./:-]{1,160}\Z")


def _path():
    return Path(os.environ.get("CODING_HERMES_USAGE_DB") or
                Path(__file__).resolve().parent.parent / "state/hermes-usage.sqlite")


def _valid(value):
    return isinstance(value, str) and bool(_LABEL.fullmatch(value))


def _error(exc):
    # Never print hook kwargs or provider responses, even on storage failure.
    code = type(exc).__name__
    print(f"hermes-coding-usage recording_error: {code}", file=sys.stderr)
    return {"status": "recording_error", "error": code}


def _read():
    path = _path()
    if not path.exists() and not path.parent.is_file():
        return None
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)


def register(ctx):
    ctx.register_hook("post_api_request", on_post_api_request)


def on_post_api_request(**kwargs):
    task, session = kwargs.get("task_id"), kwargs.get("session_id")
    if not _valid(task) or not _valid(session):
        return {"status": "incomplete/not_attributable"}
    response = kwargs.get("response")
    try:
        response_id = response.get("id") if isinstance(response, dict) else getattr(response, "id", None)
    except Exception as exc:
        return _error(exc)
    response_id = response_id if _valid(response_id) else ""
    ordinal = kwargs.get("api_call_count")
    if type(ordinal) is not int or ordinal < 1:
        return {"status": "incomplete/not_attributable"}
    provider, model = kwargs.get("provider"), kwargs.get("model")
    if not _valid(provider) or not _valid(model):
        return {"status": "incomplete/not_attributable"}
    usage = kwargs.get("usage")
    complete = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in _FIELDS)
    values = [usage[k] for k in _FIELDS] if complete else [None] * len(_FIELDS)
    path = _path()
    try:
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.chmod(0o700)
            with closing(sqlite3.connect(path, timeout=1)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""CREATE TABLE IF NOT EXISTS api_requests (
                    session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    api_call_count INTEGER NOT NULL, response_id TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER, reasoning_tokens INTEGER,
                    complete INTEGER NOT NULL, recorded_at REAL NOT NULL,
                    UNIQUE(session_id, task_id, api_call_count, response_id))""")
                conn.execute("BEGIN")
                cursor = conn.execute("""INSERT OR IGNORE INTO api_requests VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session, task, ordinal, response_id, provider, model, *values, int(complete), time.time()))
                conn.commit()
                path.chmod(0o600)
                return {"status": ("recorded" if complete else "incomplete") if cursor.rowcount else "duplicate"}
    except (OSError, sqlite3.Error, ValueError) as exc:
        return _error(exc)


def latest(session_id=None):
    try:
        conn = _read()
        if conn is None:
            return {"status": "not_found"}
        with closing(conn):
            clause = "WHERE session_id=?" if session_id else ""
            row = conn.execute("SELECT task_id, session_id FROM api_requests " + clause +
                               " ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
                               (session_id,) if session_id else ()).fetchone()
        return {"status": "ok", "task_id": row[0], "session_id": row[1]} if row else {"status": "not_found"}
    except (OSError, sqlite3.Error, ValueError) as exc:
        return _error(exc)


def summarize(task_id, session_id=None):
    if not _valid(task_id) or session_id is not None and not _valid(session_id):
        return {"status": "incomplete/not_attributable", "usage": None}
    try:
        conn = _read()
        if conn is None:
            return {"status": "not_found", "usage": None}
        with closing(conn):
            rows = conn.execute("""SELECT session_id, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, reasoning_tokens, complete
                FROM api_requests WHERE task_id=?""" + (" AND session_id=?" if session_id else ""),
                (task_id, session_id) if session_id else (task_id,)).fetchall()
        if not rows:
            return {"status": "not_found", "usage": None}
        sessions = {row[0] for row in rows}
        incomplete = sum(not bool(row[6]) for row in rows)
        result = {"task_id": task_id, "session_id": session_id or next(iter(sessions)),
                  "incomplete_count": incomplete}
        if incomplete or len(sessions) != 1:
            return {**result, "status": "incomplete/not_attributable", "usage": None}
        totals = [sum(row[i] for row in rows) for i in range(1, 6)]
        counts = dict(zip(("input", "output", "cache_read", "cache_write", "reasoning"), totals))
        counts["total"] = sum(totals[:4])  # reasoning already belongs to output
        counts["request_count"] = len(rows)
        return {**result, "status": "complete", "usage": counts}
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {**_error(exc), "usage": None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true")
    group.add_argument("--task")
    parser.add_argument("--session")
    args = parser.parse_args()
    print(json.dumps(latest() if args.latest else summarize(args.task, args.session)))
