"""Resolve gold targets (stable URL keys) to KG entity ids at test time, and match results against them.

A target is {"label", "keys": [key, ...]}; a key is {"source", "path", "exact"?, "names"?, "types"?}.
A key covers every KG entity with evidence, a profile/unit/project URL or a page_entity link on a page of
that source whose URL path starts with `path` (or equals it, when exact), plus people affiliated to, and
projects/infrastructure of, units covered that way. `names` narrows it to entities whose name (or unit name)
contains one of the names; `types` narrows by entity type.

The gold file never stores contact data: emails and phones are only read from the KG, at test time.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from eval import data
from eval.data import norm_name, url_path


def key_matches_url(key: dict, url: str, source: str | None = None) -> bool:
    if not url:
        return False
    src = source or data.source_of_url(url)
    if src != key["source"]:
        return False
    path, want = url_path(url), key["path"]
    if path.rstrip("/") == want.rstrip("/"):
        return True
    return False if key.get("exact") else path.startswith(want)


def names_match(names: list[str] | None, *labels: str | None) -> bool:
    if not names:
        return True
    hay = " | ".join(norm_name(x) for x in labels if x)
    return any(norm_name(n) in hay for n in names)


def _json_list(s):
    try:
        v = json.loads(s) if s else []
        return v if isinstance(v, list) else [v]
    except (TypeError, ValueError):
        return [s]


class KG:
    """In memory view of data/kg.sqlite (read only). Empty when the KG does not exist yet."""

    def __init__(self):
        self.available = False
        self.ent: dict[str, dict] = {}            # id -> {type, name, sources:set, urls:set, unit_ids:set}
        self.merged: dict[str, str] = {}
        self.unit_members: dict[str, set] = defaultdict(set)
        conn = data.kg_conn()
        if conn is None:
            return
        try:
            self._load(conn)
            self.available = True
        except sqlite3.Error as e:  # half written or older schema: degrade to URL matching
            print(f"[eval] kg.sqlite not usable ({e}); falling back to URL matching")
        finally:
            conn.close()

    def _rows(self, conn, sql):
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.Error:
            return []

    def _add(self, eid, typ, name, source=None, url=None, unit_id=None):
        e = self.ent.setdefault(eid, {"type": typ, "name": name, "sources": set(), "urls": set(), "unit_ids": set(),
                                      "emails": [], "phones": [], "functions": set()})
        if source:
            e["sources"].add(source)
        if url:
            e["urls"].add(url)
        if unit_id:
            e["unit_ids"].add(unit_id)
        return e

    def _load(self, conn):
        for uid, name, typ, parent, source, url in self._rows(conn, "SELECT id,name,type,parent_id,source,url FROM unit"):
            self._add(uid, "unit", name, source, url)
            if parent:
                self.ent[uid]["parent_id"] = parent
        for pid, name, emails, phones, purl, sources, unit_id, func in self._rows(
                conn, "SELECT id,name,emails,phones,profile_url,sources,primary_unit_id,primary_function FROM person"):
            e = self._add(pid, "person", name, None, purl, unit_id)
            e["sources"].update(_json_list(sources))
            e["emails"], e["phones"] = _json_list(emails), _json_list(phones)
            if func:
                e["functions"].add(func)
        for pid, uid, func, source, url in self._rows(conn, "SELECT person_id,unit_id,function,source,url FROM affiliation"):
            e = self._add(pid, "person", None, source, url, uid)
            if func:
                e["functions"].add(func)
            self.unit_members[uid].add(pid)
        for eid, title, uid, url, source, status in self._rows(conn, 'SELECT id,title,unit_id,url,source,status FROM project'):
            self._add(eid, "project", title, source, url, uid)["status"] = status
        for eid, name, uid, url, source in self._rows(conn, "SELECT id,name,unit_id,url,source FROM infrastructure"):
            self._add(eid, "infrastructure", name, source, url, uid)
        for eid, url, source in self._rows(conn, "SELECT DISTINCT entity_id,url,source FROM evidence WHERE verified=1"):
            if eid in self.ent:
                self.ent[eid]["urls"].add(url)
                if source:
                    self.ent[eid]["sources"].add(source)
        page_url = {p["page_id"]: p["url"] for p in data.pages().values()}
        for page_id, eid in self._rows(conn, "SELECT page_id,entity_id FROM page_entity"):
            if eid in self.ent and page_id in page_url:
                self.ent[eid]["urls"].add(page_url[page_id])
        for kept, merged in self._rows(conn, "SELECT kept_id,merged_id FROM merge_log"):
            self.merged[merged] = kept

    def resolve_id(self, eid: str | None) -> str | None:
        seen = set()
        while eid in self.merged and eid not in seen:
            seen.add(eid)
            eid = self.merged[eid]
        return eid

    def unit_names(self, e: dict) -> list[str]:
        return [self.ent[u]["name"] for u in e["unit_ids"] if u in self.ent and self.ent[u].get("name")]

    def resolve_target(self, target: dict) -> set[str]:
        out: set[str] = set()
        for key in target["keys"]:
            direct = {eid for eid, e in self.ent.items() if any(key_matches_url(key, u) for u in e["urls"])}
            units = {eid for eid in direct if self.ent[eid]["type"] == "unit"}
            extended = set(direct)
            for eid, e in self.ent.items():
                if e["unit_ids"] & units:
                    extended.add(eid)
            for eid in extended:
                e = self.ent[eid]
                if key.get("types") and e["type"] not in key["types"]:
                    continue
                if not names_match(key.get("names"), e.get("name"), *self.unit_names(e)):
                    continue
                out.add(eid)
        return out


def result_matches(result: dict, target: dict, resolved: set[str], kg: KG) -> str | None:
    """How a result hits a target: 'kg' (entity id resolved from the keys) or 'reason_url' (a cited page is
    under a key and the name/type filters hold). None when it does not hit."""
    eid = kg.resolve_id(result.get("entity_id"))
    if eid and eid in resolved:
        return "kg"
    for key in target["keys"]:
        if key.get("types") and result.get("type") not in key["types"]:
            continue
        if not names_match(key.get("names"), result.get("name"), result.get("unit")):
            continue
        if any(key_matches_url(key, r.get("url")) for r in result.get("reasons") or []):
            return "reason_url"
        if key_matches_url(key, result.get("url")):   # the entity's own page, when the result carries it
            return "entity_url"
    return None


def key_exists_in_crawl(key: dict) -> bool:
    """A gold key is only valid if at least one crawled page sits under it (checked against data/, not the web)."""
    return any(key_matches_url(key, u) for u in data.crawled_urls())
