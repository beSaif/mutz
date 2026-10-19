"""App state in data/app.sqlite (owned by agent F): leads, needs, decisions, pitch drafts and versions,
the lead timeline, outreach drafts, Mutz threads and the anonymous LeadSignal log."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("MUTZ_APP_DB", "data/app.sqlite"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS lead(
  id TEXT PRIMARY KEY, title TEXT, company TEXT, summary TEXT, sector TEXT, origin TEXT,
  stage TEXT DEFAULT 'new',            -- new|triaged|pitched|visit|won|lost
  step TEXT DEFAULT 'brief',           -- brief|matches|map|pitch|sent
  input_kind TEXT DEFAULT 'text', input_text TEXT, website TEXT, pdf_name TEXT, keep_file INTEGER DEFAULT 0,
  lang TEXT DEFAULT 'en', profile TEXT, passages TEXT, struck TEXT DEFAULT '[]',
  search_state TEXT DEFAULT 'idle',    -- idle|running|done|error
  search_error TEXT, lost_reason TEXT, demo INTEGER DEFAULT 0, opened INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS need(
  id TEXT PRIMARY KEY, lead_id TEXT, pos INTEGER, text TEXT, label TEXT, type TEXT, priority INTEGER,
  from_sentence TEXT, source TEXT, terms TEXT, shareable INTEGER DEFAULT 1, status TEXT DEFAULT 'active',
  route TEXT, result TEXT, coverage TEXT, searched_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS decision(
  lead_id TEXT, need_id TEXT, entity_id TEXT, decision TEXT, added_by TEXT, match TEXT, at TEXT,
  PRIMARY KEY(lead_id, need_id, entity_id));
CREATE TABLE IF NOT EXISTS pitch(
  lead_id TEXT PRIMARY KEY, draft TEXT, saved_at TEXT);
CREATE TABLE IF NOT EXISTS pitch_version(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, n INTEGER, pitch_json TEXT, built_from TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS entry(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, kind TEXT, text TEXT, confidential INTEGER DEFAULT 1,
  shareable INTEGER DEFAULT 0, added_by TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS outreach(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, entity_id TEXT, name TEXT, lang TEXT, draft TEXT,
  status TEXT DEFAULT 'drafted', at TEXT);
CREATE TABLE IF NOT EXISTS mutz_thread(key TEXT PRIMARY KEY, messages TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS lead_signal(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id_hash TEXT, year INTEGER, sector TEXT, need_concept_id TEXT,
  need_type TEXT, priority INTEGER, coverage TEXT, archives_searched TEXT, stage TEXT, lost_reason TEXT,
  demo INTEGER DEFAULT 0, at TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS missed_expert(id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT, name TEXT, at TEXT);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# columns added after the first schema; older app.sqlite files get them on start
MIGRATIONS = [("lead", "owner", "TEXT"), ("decision", "who", "TEXT"), ("entry", "who", "TEXT")]


def init():
    with _lock, conn() as c:
        c.executescript(SCHEMA)
        for t, col, typ in MIGRATIONS:
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({t})")}:
                c.execute(f"ALTER TABLE {t} ADD COLUMN {col} {typ}")


def q(sql, args=()):
    with _lock, conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def x(sql, args=()):
    with _lock, conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def new_id():
    return uuid.uuid4().hex[:10]


J = json.dumps


def L(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- leads

def create_lead(title, input_text="", origin="S-GE", website="", sector="", summary="", demo=0, stage="new", input_kind="text",
                owner=None):
    lid = new_id()
    x("INSERT INTO lead(id,title,company,summary,sector,origin,stage,input_kind,input_text,website,demo,owner,created_at,updated_at)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (lid, title, "", summary, sector, origin, stage, input_kind, input_text, website, demo, owner, now(), now()))
    add_entry(lid, "one_pager" if input_text else "note", input_text or "Lead created", confidential=1, who=owner)
    return lid


def lead(lid):
    r = one("SELECT * FROM lead WHERE id=?", (lid,))
    if r:
        r["profile"] = L(r["profile"], {}) or {}
        r["passages"] = L(r["passages"], []) or []
        r["struck"] = L(r["struck"], []) or []
    return r


def update_lead(lid, **kw):
    if not kw:
        return
    kw["updated_at"] = now()
    cols = ",".join(f"{k}=?" for k in kw)
    x(f"UPDATE lead SET {cols} WHERE id=?", (*[J(v) if isinstance(v, (dict, list)) else v for v in kw.values()], lid))


def leads():
    return q("SELECT * FROM lead ORDER BY updated_at DESC")


def delete_lead(lid):
    for t in ("need", "decision", "pitch", "pitch_version", "entry", "outreach"):
        x(f"DELETE FROM {t} WHERE lead_id=?", (lid,))
    x("DELETE FROM mutz_thread WHERE key=?", (lid,))
    x("DELETE FROM lead WHERE id=?", (lid,))


def reset():
    """Every lead and what hangs off it, for a clean demo. Officers, caches and the seeded gap report rows stay."""
    for t in ("need", "decision", "pitch", "pitch_version", "entry", "outreach", "mutz_thread", "lead"):
        x(f"DELETE FROM {t}")
    x("DELETE FROM lead_signal WHERE demo != 2")


# ---------------------------------------------------------------- officers (no passwords: a cookie says who is working)

OFFICERS = [{"id": "saif", "name": "Saif", "focus": "Life sciences and medtech"},
            {"id": "jumainah", "name": "Jumainah", "focus": "Cleantech and industry"}]


def officers():
    return kv_get("officers") or OFFICERS


def officer(oid):
    return next((o for o in officers() if o["id"] == oid), None)


def add_officer(name, focus=""):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "officer"
    have = {o["id"] for o in officers()}
    oid, i = base, 2
    while oid in have:
        oid, i = f"{base}-{i}", i + 1
    kv_set("officers", officers() + [{"id": oid, "name": name, "focus": focus}])
    return oid


def owner_name(l):
    """The lead owner's name for pitches and emails; the placeholder if the lead has none."""
    o = officer((l or {}).get("owner"))
    return o["name"] if o else "[Officer name]"


# ---------------------------------------------------------------- needs

def needs(lid, active_only=True):
    rows = q("SELECT * FROM need WHERE lead_id=? " + ("AND status='active' " if active_only else "") + "ORDER BY pos", (lid,))
    for r in rows:
        r["terms"] = L(r["terms"], []) or []
        r["result"] = L(r["result"], None)
        r["route"] = L(r["route"], None)
    return rows


def need(nid):
    r = one("SELECT * FROM need WHERE id=?", (nid,))
    if r:
        r["terms"] = L(r["terms"], []) or []
        r["result"] = L(r["result"], None)
        r["route"] = L(r["route"], None)
    return r


def replace_needs(lid, items):
    x("DELETE FROM need WHERE lead_id=?", (lid,))
    for i, n in enumerate(items):
        add_need(lid, n, pos=i)


def add_need(lid, n, pos=None):
    if pos is None:
        pos = (one("SELECT COALESCE(MAX(pos),-1)+1 AS p FROM need WHERE lead_id=?", (lid,)) or {"p": 0})["p"]
    nid = new_id()
    terms = [{"t": t, "on": True} if isinstance(t, str) else t for t in (n.get("terms") or [])]
    x("INSERT INTO need(id,lead_id,pos,text,label,type,priority,from_sentence,source,terms,shareable,status)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
      (nid, lid, pos, n.get("text", ""), n.get("label") or (n.get("type") or "research").capitalize(),
       n.get("type", "research"), n.get("priority") or pos + 1, n.get("from_sentence", ""), n.get("source", "text"),
       J(terms), 1 if n.get("type") in ("research", "infrastructure", None) else 0, "active"))
    return nid


def update_need(nid, **kw):
    cols = ",".join(f"{k}=?" for k in kw)
    x(f"UPDATE need SET {cols} WHERE id=?", (*[J(v) if isinstance(v, (dict, list)) else v for v in kw.values()], nid))


# ---------------------------------------------------------------- decisions

def decide(lid, need_id, entity_id, decision, match=None, added_by="officer", who=None):
    if decision is None:
        x("DELETE FROM decision WHERE lead_id=? AND need_id=? AND entity_id=?", (lid, need_id, entity_id))
        return
    x("INSERT OR REPLACE INTO decision(lead_id,need_id,entity_id,decision,added_by,match,who,at) VALUES(?,?,?,?,?,?,?,?)",
      (lid, need_id, entity_id, decision, added_by, J(match) if match else None, who, now()))


def decisions(lid):
    rows = q("SELECT * FROM decision WHERE lead_id=? ORDER BY at", (lid,))
    for r in rows:
        r["match"] = L(r["match"], None)
    return rows


def accepted(lid):
    return [d for d in decisions(lid) if d["decision"] == "accept"]


# ---------------------------------------------------------------- pitch

def pitch_draft(lid):
    r = one("SELECT * FROM pitch WHERE lead_id=?", (lid,))
    return (L(r["draft"], None), r["saved_at"]) if r else (None, None)


def save_pitch(lid, draft):
    x("INSERT OR REPLACE INTO pitch(lead_id,draft,saved_at) VALUES(?,?,?)", (lid, J(draft), now()))


def versions(lid):
    rows = q("SELECT * FROM pitch_version WHERE lead_id=? ORDER BY n", (lid,))
    for r in rows:
        r["pitch_json"] = L(r["pitch_json"], {})
        r["built_from"] = L(r["built_from"], [])
    return rows


def add_version(lid, pitch, built_from):
    n = (one("SELECT COALESCE(MAX(n),0)+1 AS n FROM pitch_version WHERE lead_id=?", (lid,)) or {"n": 1})["n"]
    x("INSERT INTO pitch_version(lead_id,n,pitch_json,built_from,sent_at) VALUES(?,?,?,?,?)",
      (lid, n, J(pitch), J(built_from), now()))
    return n


# ---------------------------------------------------------------- timeline, outreach, mutz

def add_entry(lid, kind, text, confidential=1, shareable=0, added_by="officer", who=None):
    return x("INSERT INTO entry(lead_id,kind,text,confidential,shareable,added_by,who,at) VALUES(?,?,?,?,?,?,?,?)",
             (lid, kind, text, confidential, shareable, added_by, who, now()))


def entries(lid):
    return q("SELECT * FROM entry WHERE lead_id=? ORDER BY id", (lid,))


def outreach(lid):
    return q("SELECT * FROM outreach WHERE lead_id=? ORDER BY id", (lid,))


def thread(key):
    r = one("SELECT messages FROM mutz_thread WHERE key=?", (key,))
    return L(r["messages"], []) if r else []


def save_thread(key, messages):
    x("INSERT OR REPLACE INTO mutz_thread(key,messages,at) VALUES(?,?,?)", (key, J(messages[-40:]), now()))


# ---------------------------------------------------------------- LeadSignal (anonymous)

def lead_hash(lid):
    return hashlib.sha256(("mutz-lead:" + lid).encode()).hexdigest()[:16]


def write_signals(lid, sector, rows, stage, archives, demo=0):
    """One row per need. No company name, no one pager text: concept id, type, coverage, archives, stage."""
    h = lead_hash(lid)
    x("DELETE FROM lead_signal WHERE lead_id_hash=?", (h,))
    year = datetime.now().year
    for r in rows:
        x("INSERT INTO lead_signal(lead_id_hash,year,sector,need_concept_id,need_type,priority,coverage,archives_searched,stage,demo,at)"
          " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (h, year, sector or "unknown", r["concept"], r["type"], r.get("priority") or 0, r["coverage"], J(archives), stage, demo, now()))


def update_signal_stage(lid, stage, lost_reason=None):
    x("UPDATE lead_signal SET stage=?, lost_reason=? WHERE lead_id_hash=?", (stage, lost_reason, lead_hash(lid)))


def signals():
    rows = q("SELECT * FROM lead_signal ORDER BY id")
    for r in rows:
        r["archives_searched"] = L(r["archives_searched"], [])
    return rows


def kv_get(key, default=None):
    r = one("SELECT value FROM kv WHERE key=?", (key,))
    return L(r["value"], default) if r else default


def kv_set(key, value):
    if value is None:
        x("DELETE FROM kv WHERE key=?", (key,))
    else:
        x("INSERT OR REPLACE INTO kv(key,value,at) VALUES(?,?,?)", (key, J(value), now()))
