"""SQLite storage: traces, model calls, feedback, issues, proposals, memory, settings."""
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_local = threading.local()

SCHEMA = """
create table if not exists traces(id text primary key, ts real, app text, version text, ok int,
  error text, fingerprint text, ms int, input text, output text);
create table if not exists calls(id integer primary key, ts real, trace_id text, task text, model text,
  ms int, tin int, tout int, usd real, ok int, error text);
create table if not exists feedback(id integer primary key, ts real, trace_id text, rating int, text text,
  seen int default 0);
create table if not exists issues(id integer primary key, fingerprint text unique, kind text, app text,
  title text, detail text, count int default 0, seen_count int default 0, first_ts real, last_ts real,
  status text default 'open');
create table if not exists proposals(id integer primary key, ts real, issue_id int, app text, channel text,
  branch text, status text, attempts int default 0, summary text, diff text, gate text, reason text);
create table if not exists scorecards(id integer primary key, ts real, app text, version text, card text);
create table if not exists cache(k text primary key, v text, ts real);
create table if not exists kv(k text primary key, v text);
create virtual table if not exists memory using fts5(ts unindexed, kind, key, text);
"""


def db():
    path = os.environ.get("FORGE_DB") or str(ROOT / ".forge" / "forge.db")
    if getattr(_local, "path", None) != path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("pragma journal_mode=wal")
        c.executescript(SCHEMA)
        _local.c, _local.path = c, path
    return _local.c


def q(sql, *args):
    return db().execute(sql, args).fetchall()


def x(sql, *args):
    return db().execute(sql, args).lastrowid


def kv_get(key, default=None):
    rows = q("select v from kv where k=?", key)
    value = json.loads(rows[0]["v"]) if rows else None
    return default if value is None else value


def kv_set(key, value):
    x("insert into kv(k,v) values(?,?) on conflict(k) do update set v=excluded.v", key, json.dumps(value))


def today():
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def remember(kind, key, text):
    x("insert into memory(ts,kind,key,text) values(?,?,?,?)", time.time(), kind, key, text)


def recall(query, n=3):
    # shortcut: keyword search (FTS5), not embeddings; upgrade if recall misses reworded problems
    words = list(dict.fromkeys(re.findall(r"[A-Za-z_]{4,}", query)))[:12]
    if not words:
        return []
    match = " OR ".join(f'"{w}"' for w in words)
    return q("select kind, key, text from memory where memory match ? order by rank limit ?", match, n)
