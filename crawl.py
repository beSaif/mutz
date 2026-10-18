#!/usr/bin/env python3
"""Polite snapshot crawler for mutz.

Fetches HTML only and saves raw pages to data/raw/. No LLM calls.
Extraction, embedding and search all read from this snapshot, so the
live sites are hit once and reruns cost nothing.

    pip install httpx selectolax
    export CRAWL_CONTACT="you@example.com"     # goes into the User-Agent
    python crawl.py                            # all sources
    python crawl.py artorg dcb                 # only some
    python crawl.py unibe                      # every site in inventory/unibe_seeds.txt
    python crawl.py unibe_dbmr                 # one unibe site
    python crawl.py --config my_sources.json   # override the source list

Outputs
    data/raw/<source>/<hash>.html   raw page
    data/raw/index.jsonl            one record per URL tried
    data/raw/pdf_links.txt          in-scope PDFs seen (not downloaded)
    data/raw/stats.json             per-source counts, compared on each run
"""
import argparse, asyncio, hashlib, json, os, re, sys, time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

OUT = Path("data/raw")
DELAY = 1.0          # seconds between requests to the same host
MAX_DEPTH = 4
UA = f"mutz-hackathon-crawler/0.1 (Hack Apertus Liebefeld; contact: {os.environ.get('CRAWL_CONTACT', 'unset')})"

SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|ico|zip|docx?|xlsx?|pptx?|mp4|mp3|css|js|xml|ics)$", re.I)

SOURCES = [
    {
        "id": "artorg",
        "seeds": ["https://www.artorg.unibe.ch/research/index_eng.html"],
        "allow": ["www.artorg.unibe.ch"],
        "include": [r"/research/"],
        "exclude": [r"/news/", r"/open_positions/"],
        "max_pages": 800,
    },
    {
        "id": "dcb",
        "seeds": ["https://www.dcberne.com/en/"],
        "allow": ["www.dcberne.com", "dcberne.com"],
        "include": [],
        "exclude": [r"/news/", r"/blog/", r"/jobs?/"],
        "max_pages": 600,
    },
    {
        # expanded at startup into one source per site, all in group "unibe"
        "id": "unibe",
        "seeds_file": "inventory/unibe_seeds.txt",
        "include": [],
        "exclude": [r"/news/", r"/suche", r"/search", r"/print", r"/veranstaltungen/", r"/events/"],
        "max_pages": 200,          # per site
    },
]
PER_DOMAIN = 4       # max requests in flight to one registrable domain (all of unibe.ch)


def expand(sources):
    """Turn a source with seeds_file into one source per listed site, so sites crawl in parallel."""
    out = []
    for src in sources:
        if "seeds_file" not in src:
            out.append(src)
            continue
        path = Path(src["seeds_file"])
        if not path.exists():
            print(f"[{src['id']}] seeds file {path} missing, skipping this source", flush=True)
            continue
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            url = clean(line if line.startswith("http") else f"https://{line}/")
            if not url:
                continue
            host = urlparse(url).netloc
            sub = host.split(":")[0].removeprefix("www.").split(".")[0]
            site = {k: v for k, v in src.items() if k != "seeds_file"}
            site.update(id=f"{src['id']}_{sub}", group=src["id"], seeds=[url], allow=[host])
            out.append(site)
    return out


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(url):
    """Absolute http(s) URL without fragment or query (queries are mostly calendar traps)."""
    url, _ = urldefrag(url)
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return p._replace(query="", netloc=p.netloc.lower()).geturl()


def in_scope(url, src):
    p = urlparse(url)
    if p.netloc not in src["allow"] or SKIP_EXT.search(p.path):
        return False
    if src.get("include") and not any(re.search(rx, p.path) for rx in src["include"]):
        return False
    return not any(re.search(rx, p.path) for rx in src.get("exclude", []))


def guess_lang(url, tree):
    node = tree.css_first("html")
    lang = (node.attributes.get("lang") or "") if node else ""
    if lang:
        return lang[:2].lower()
    m = re.search(r"index_(ger|eng|fra)|/(de|en|fr)/", url)
    if m:
        return {"ger": "de", "eng": "en", "fra": "fr"}.get(m.group(1), m.group(2))
    return None


def links(tree, base):
    for a in tree.css("a[href]"):
        u = clean(urljoin(base, a.attributes.get("href") or ""))
        if u:
            yield u


class Polite:
    """Robots.txt per host, one request at a time per host, DELAY between them."""

    def __init__(self, client):
        self.client = client
        self.robots, self.locks, self.next_ok, self.delay, self.sems = {}, {}, {}, {}, {}

    def _sem(self, host):
        domain = ".".join(host.split(":")[0].split(".")[-2:])
        return self.sems.setdefault(domain, asyncio.Semaphore(PER_DOMAIN))

    async def _wait_turn(self, host):
        wait = self.next_ok.get(host, 0) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def allowed(self, url):
        p = urlparse(url)
        host = p.netloc
        if host not in self.robots:
            rp = robotparser.RobotFileParser()
            async with self.locks.setdefault(host, asyncio.Lock()):
                await self._wait_turn(host)
                try:
                    r = await self.client.get(f"{p.scheme}://{host}/robots.txt")
                    if r.status_code in (401, 403):
                        rp.disallow_all = True
                    else:
                        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                except httpx.HTTPError:
                    rp.parse([])
                self.next_ok[host] = time.monotonic() + DELAY
            self.robots[host] = rp
        return self.robots[host].can_fetch(UA, url)

    async def get(self, url):
        host = urlparse(url).netloc
        async with self.locks.setdefault(host, asyncio.Lock()):
            await self._wait_turn(host)
            try:
                async with self._sem(host):
                    r = await self.client.get(url)
            finally:
                self.next_ok[host] = time.monotonic() + self.delay.get(host, DELAY)
            if r.status_code in (429, 503):     # back off hard, double the delay for this host
                self.delay[host] = min(self.delay.get(host, DELAY) * 2, 30)
                retry = r.headers.get("retry-after", "")
                self.next_ok[host] = time.monotonic() + (int(retry) if retry.isdigit() else 30)
                print(f"  ! {host} said {r.status_code}, slowing to {self.delay[host]}s", flush=True)
            return r


def load_index():
    index = {}
    f = OUT / "index.jsonl"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                index[rec["url"]] = rec          # later lines win
    return index



async def crawl(src, polite, index, index_f, pdf_f):
    seeds = src["seeds"]
    group = src.get("group", src["id"])
    stats = {"fetched": 0, "cached": 0, "robots_blocked": 0, "errors": 0, "skipped": 0}
    queue, seen, pages = deque((s, 0) for s in seeds), set(), 0

    while queue and pages < src["max_pages"]:
        url, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        if depth > 0 and not in_scope(url, src):
            continue

        rec = index.get(url)
        if rec and rec.get("status") == 200 and (OUT / rec["file"]).exists():
            html = (OUT / rec["file"]).read_text(errors="ignore")
            base = rec.get("final_url", url)
            stats["cached"] += 1
        else:
            if not await polite.allowed(url):
                stats["robots_blocked"] += 1
                continue
            try:
                r = await polite.get(url)
            except httpx.HTTPError as e:
                stats["errors"] += 1
                index_f.write(json.dumps({"url": url, "source": src["id"], "group": group, "status": None,
                                          "error": type(e).__name__, "fetched_at": now()}) + "\n")
                continue
            final = clean(str(r.url)) or url
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or "html" not in ctype or urlparse(final).netloc not in src["allow"]:
                stats["skipped"] += 1
                index_f.write(json.dumps({"url": url, "final_url": final, "source": src["id"], "group": group,
                                          "status": r.status_code, "content_type": ctype,
                                          "fetched_at": now()}) + "\n")
                continue
            html = r.text
            base = final
            rel = f"{src['id']}/{hashlib.sha1(url.encode()).hexdigest()[:16]}.html"
            (OUT / rel).parent.mkdir(parents=True, exist_ok=True)
            (OUT / rel).write_text(html)
            rec = {
                "url": url, "final_url": final, "source": src["id"], "group": group, "status": 200, "file": rel,
                "fetched_at": now(), "depth": depth,
                "etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified"),
                "content_hash": hashlib.sha256(html.encode()).hexdigest(),
                "lang": guess_lang(url, HTMLParser(html)),
            }
            index[url] = rec
            index_f.write(json.dumps(rec) + "\n")
            index_f.flush()
            stats["fetched"] += 1

        pages += 1
        if pages % 25 == 0:
            print(f"[{src['id']}] {pages} pages ({stats['fetched']} new, {stats['cached']} cached), queue {len(queue)}", flush=True)
        if depth >= MAX_DEPTH:
            continue
        for link in links(HTMLParser(html), base):
            if link in seen:
                continue
            if link.lower().endswith(".pdf") and urlparse(link).netloc in src["allow"]:
                pdf_f.add(f"{src['id']}\t{link}")
                seen.add(link)
            elif in_scope(link, src):
                queue.append((link, depth + 1))

    stats["pages"] = pages
    print(f"[{src['id']}] done: {stats}", flush=True)
    return src["id"], stats


def compare_stats(new):
    f = OUT / "stats.json"
    if f.exists():
        old = json.loads(f.read_text())
        for sid, s in new.items():
            before = old.get(sid, {}).get("pages", 0)
            if before and s["pages"] < 0.7 * before:
                print(f"WARNING [{sid}] page count dropped {before} -> {s['pages']}. "
                      f"Site redesign or block? Check before trusting this crawl.", flush=True)
    f.write_text(json.dumps(new, indent=2))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="*", help="source ids to crawl")
    ap.add_argument("--config", help="JSON file with a list of sources, replaces the built in list")
    args = ap.parse_args()

    sources = expand(json.loads(Path(args.config).read_text()) if args.config else SOURCES)
    if args.only:
        sources = [s for s in sources if s["id"] in args.only or s.get("group") in args.only]
    if "unset" in UA:
        print("Set CRAWL_CONTACT to an email so site admins can reach you instead of blocking the venue.", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    index = load_index()
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as client:
        polite = Polite(client)
        pdf_file = OUT / "pdf_links.txt"
        pdfs = set(pdf_file.read_text().splitlines()) if pdf_file.exists() else set()
        with open(OUT / "index.jsonl", "a") as index_f:
            results = await asyncio.gather(*(crawl(s, polite, index, index_f, pdfs) for s in sources))
        pdf_file.write_text("".join(f"{line}\n" for line in sorted(pdfs)))
    compare_stats(dict(results))


if __name__ == "__main__":
    asyncio.run(main())
