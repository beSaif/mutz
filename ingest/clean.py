#!/usr/bin/env python3
"""Clean and chunk the raw crawl snapshot. No network access, ever.

Reads   data/raw/index.jsonl + data/raw/<source>/<hash>.html   (written by crawl.py)
Writes  data/pages.jsonl   {page_id, url, source, group, lang, title, text, emails[], siblings[], fetched_at,
                            page_type, breadcrumbs[], sibling_group, ...extras}
        data/chunks.jsonl  {chunk_id, page_id, url, source, lang, heading, text, ...extras}

    python -m ingest.clean                 # incremental: unchanged pages come from data/cache/clean/
    python -m ingest.clean --force         # reprocess every page
    python -m ingest.clean --check         # print coverage of every cached 200 page

Per page work is cached by content_hash + PARSER_VERSION, so a rerun only parses new pages.
Output files are written to a .tmp file and renamed, so readers never see a half file.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

PARSER_VERSION = "12"
RAW = Path("data/raw")
OUT_DIR = Path("data")
CACHE = Path("data/cache/clean")
LANGS = ("de", "en", "fr", "it")
URL_LANG = {"ger": "de", "eng": "en", "fra": "fr", "ita": "it"}
LANG_URL = {v: k for k, v in URL_LANG.items()}
DROP_WARN = 0.30  # gap 16: warn when a source loses more than 30% of its pages

CHUNK_MIN, CHUNK_TARGET, CHUNK_MAX = 200, 300, 400

# ---------------------------------------------------------------- emails

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
# "name [at] unibe.ch", "name (at) unibe [dot] ch", "name at unibe dot ch" (the last only with a dot word)
AT_RE = re.compile(
    r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9._%+-]*)\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s@\s|\[ät\]|\(ät\))\s*"
    r"([A-Za-z0-9-]+(?:\s*(?:\.|\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+|\[\s*punkt\s*\])\s*[A-Za-z0-9-]+)+)",
    re.I,
)
DOT_RE = re.compile(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+|\[\s*punkt\s*\])\s*", re.I)
CHARCODE_RE = re.compile(r"String\.fromCharCode\(\s*([0-9,\s]+)\)")
# <a href="javascript:...fromCharCode(...)">..</a> and <a href="mailto:..">..</a>
ANCHOR_RE = re.compile(r"<a\b([^>]*?)\bhref\s*=\s*([\"'])(.*?)\2([^>]*)>(.*?)</a>", re.I | re.S)
BAD_EMAIL_TLD = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$", re.I)


def decode_charcodes(s: str) -> str:
    """'...String.fromCharCode(103,117,...)' -> the decoded string (all calls concatenated)."""
    out = []
    for m in CHARCODE_RE.finditer(s):
        try:
            out.append("".join(chr(int(x)) for x in m.group(1).split(",") if x.strip()))
        except ValueError:
            pass
    return "".join(out)


def normalize_email(e: str) -> str | None:
    e = htmllib.unescape(e).strip().strip(".,;:<>()[]\"'").lower()
    e = e.removeprefix("mailto:").split("?", 1)[0]
    if not EMAIL_RE.fullmatch(e) or BAD_EMAIL_TLD.search(e):
        return None
    return e


def href_email(href: str) -> str | None:
    """Email hidden in a link target: mailto:, JavaScript fromCharCode, or %-encoded."""
    h = htmllib.unescape(href)
    if "fromCharCode" in h:
        dec = decode_charcodes(h)
        return normalize_email(dec) if dec else None
    if h.lower().startswith("mailto:"):
        from urllib.parse import unquote
        return normalize_email(unquote(h[7:]))
    return None


def deobfuscate_anchors(raw: str) -> str:
    """Rewrite email links so the address is visible plain text: <span data-email="x">x</span>.

    unibe hides addresses in href="javascript:void(location.href='mailto:'+String.fromCharCode(..))" with an
    entity-encoded label; DCB uses mailto links labelled "Mail". After this the address is in the page text,
    next to the person it belongs to, where trafilatura and the extractor's verifier can see it.
    """

    def sub(m):
        email = href_email(m.group(3))
        if not email:
            return m.group(0)
        label = m.group(5)
        label_txt = htmllib.unescape(re.sub(r"<[^>]+>", "", label)).strip().lower()
        # plain text, not a link: trafilatura drops short link-only paragraphs ("Mail: <a>..</a>")
        span = f'<span data-email="{email}">{email}</span>'
        if email in label_txt or not label_txt or len(label_txt) >= 40:
            return span
        return f"{label} {span}"

    return ANCHOR_RE.sub(sub, raw)


DL_PAIR = re.compile(r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>", re.I | re.S)


def flatten_dl(raw: str) -> str:
    """<dt>Function</dt><dd>Group Head</dd> -> <p>Function: Group Head</p>, so the label and value stay on one
    line in the text (trafilatura otherwise renders them as two unrelated list items)."""
    def sub(m):
        label = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return f"<p>{label}: {m.group(2).strip()}</p>" if label else f"<p>{m.group(2)}</p>"
    return DL_PAIR.sub(sub, raw)


def emails_in_text(text: str) -> list[str]:
    found = []
    for m in EMAIL_RE.finditer(text):
        e = normalize_email(m.group(0))
        if e:
            found.append(e)
    for m in AT_RE.finditer(text):
        e = normalize_email(f"{m.group(1)}@{DOT_RE.sub('.', m.group(2))}")
        if e:
            found.append(e)
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------- language

def lang_from_url(url: str) -> str | None:
    m = re.search(r"index_(ger|eng|fra|ita)\.html", url)
    if m:
        return URL_LANG[m.group(1)]
    m = re.match(r"/(de|en|fr|it)(/|$)", urlparse(url).path)
    return m.group(1) if m else None


def detect_lang(html_lang: str | None, url: str, text: str) -> tuple[str, str]:
    if html_lang:
        code = html_lang.strip().lower()[:2]
        if code in LANGS:
            return code, "html"
    u = lang_from_url(url)
    if u:
        return u, "url"
    if text and len(text.split()) >= 5:
        try:
            import py3langid as langid
            code, _ = langid.classify(text[:5000])
            return code, "langid"
        except Exception:
            pass
    return "und", "none"


# ---------------------------------------------------------------- urls and siblings

def clean_url(url: str) -> str | None:
    """Same normalisation as crawl.clean: absolute http(s), no fragment, no query, lower case host."""
    url, _ = urldefrag(url)
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return p._replace(query="", netloc=p.netloc.lower()).geturl()


def url_key(url: str) -> str:
    """Key that ignores http/https, a trailing index.html and a trailing slash."""
    p = urlparse(url)
    path = re.sub(r"/index\.html?$", "/", p.path) or "/"
    if not path.endswith("/") and not re.search(r"\.\w{2,5}$", path):
        path += "/"
    return p.netloc.removeprefix("www.") + path


def url_variants(url: str) -> list[str]:
    """Other language URLs implied by the URL pattern alone (unibe index_ger/eng/fra, /de/ <-> /en/)."""
    out = []
    m = re.search(r"index_(ger|eng|fra|ita)\.html", url)
    if m:
        for code in ("ger", "eng", "fra"):
            if code != m.group(1):
                out.append(url[: m.start(1)] + code + url[m.end(1):])
    p = urlparse(url)
    m = re.match(r"/(de|en|fr)(/.*|$)", p.path)
    if m:
        for code in ("de", "en", "fr"):
            if code != m.group(1):
                out.append(p._replace(path=f"/{code}{m.group(2)}").geturl())
    return out


LANG_LABELS = {
    "de": "de", "deutsch": "de", "german": "de", "ger": "de", "allemand": "de",
    "en": "en", "english": "en", "englisch": "en", "eng": "en", "anglais": "en",
    "fr": "fr", "français": "fr", "francais": "fr", "french": "fr", "französisch": "fr", "fra": "fr",
}


def lang_links(tree, base: str) -> dict[str, str]:
    """{lang: url} from hreflang links and language switchers on the page."""
    found: dict[str, str] = {}

    def add(lang, href):
        lang = (lang or "").strip().lower()[:2]
        u = clean_url(urljoin(base, href or ""))
        if lang in LANGS and u and lang not in found:
            found[lang] = u

    for n in tree.css("link[hreflang], a[hreflang]"):
        add(n.attributes.get("hreflang"), n.attributes.get("href"))
    for sel in ("#nav-lang a", "nav.nav-lang a", ".wpml-ls a", ".language_switcher a", ".lang-switch a",
                ".language-switcher a", ".languages a", "[class*=lang] a"):
        for n in tree.css(sel):
            label = n.text(strip=True).lower()
            if label in LANG_LABELS:
                add(LANG_LABELS[label], n.attributes.get("href"))
    return found


# ---------------------------------------------------------------- text extraction

SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "form", "button", "select", "head"}
CHROME_SEL = ["header", "footer", "nav", "aside", "#nav-lang", ".breadcrumb", "#cookie-notice", ".cookie",
              "[class*=cookie]", "[id*=cookie]", ".share", ".social", "[role=navigation]", "[role=banner]",
              "[role=contentinfo]", ".skip-link", ".visually-hidden", "#top-header", "#main-header", "#main-footer",
              ".et-social-icons", ".wpml-ls", "[class*=language_switcher]", ".et_pb_social_media_follow"]
PRESTRIP_SEL = ["footer", "[class*=et-l--footer]", "[class*=et-l--header]", "#main-footer", "#top-header",
                "#main-header", "body > header", "#cookie-notice", "[class*=cookie-banner]", "[id*=cookie-law]"]
BLOCK_TAGS = {"p", "div", "section", "article", "li", "ul", "ol", "dl", "dt", "dd", "tr", "table", "tbody",
              "thead", "blockquote", "pre", "figure", "figcaption", "main", "address", "td", "th", "hr"}
HEAD_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def _walk(node, out: list[str]):
    for ch in node.iter(include_text=True):
        tag = ch.tag
        if tag == "-text":
            out.append(ch.text(deep=False) or "")
        elif tag in SKIP_TAGS or tag == "-comment":
            continue
        elif tag in HEAD_TAGS:
            t = " ".join(ch.text(separator=" ").split())
            if t:
                out.append(f"\n\n{'#' * HEAD_TAGS[tag]} {t}\n\n")
        elif tag == "br":
            out.append("\n")
        elif tag in BLOCK_TAGS:
            out.append("\n")
            _walk(ch, out)
            out.append("\n")
        elif tag in ("td", "th"):
            out.append(" ")
            _walk(ch, out)
        else:
            _walk(ch, out)


def tidy(text: str) -> str:
    lines = [" ".join(l.split()) for l in text.replace("​", "").replace("\xa0", " ").split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def dom_text(tree) -> str:
    """Markdown-ish text of the main content: headings as '#', one block per line, chrome removed."""
    root = (tree.css_first("main") or tree.css_first("[role=main]") or tree.css_first("#main-content")
            or tree.css_first("#content") or tree.body)
    if root is None:
        return ""
    for sel in CHROME_SEL:
        for n in root.css(sel):
            n.decompose()
    out: list[str] = []
    _walk(root, out)
    text = tidy("".join(out))
    # drop lines that are only link labels left from cards ("Mail", "More")
    text = re.sub(r"(?m)^(Mail|E-Mail|More|Mehr|Read more|Weiterlesen)$\n?", "", text)
    return tidy(text)


def traf_text(raw: str, url: str) -> str:
    import trafilatura
    try:
        out = trafilatura.extract(raw, url=url, output_format="markdown", include_formatting=True,
                                  include_links=False, include_tables=True, include_comments=False,
                                  favor_recall=True, deduplicate=False)
    except Exception:
        out = None
    if not out:
        return ""
    out = out.replace("**", "")  # bold markers add noise to quotes
    return tidy(out)


def words(s: str) -> int:
    return len(s.split())


# ---------------------------------------------------------------- page metadata

def breadcrumbs_of(tree) -> list[str]:
    m = tree.css_first('meta[name="breadcrumb"]')
    if m and m.attributes.get("content"):
        try:
            v = ast.literal_eval(m.attributes["content"])
            if isinstance(v, (list, tuple)):
                return [str(x).replace("​", "").strip() for x in v if str(x).strip()]
        except Exception:
            pass
    nav = tree.css_first("nav.breadcrumb") or tree.css_first(".breadcrumb") or tree.css_first("[class*=breadcrumb]")
    if nav:
        items = [" ".join(li.text(separator=" ").split()) for li in nav.css("li")]
        items = [i.replace("​", "") for i in items if i]
        if items:
            return items
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.text())
        except Exception:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                if d.get("@type") == "BreadcrumbList":
                    names = []
                    for it in sorted(d.get("itemListElement", []), key=lambda x: x.get("position", 0)):
                        nm = it.get("name") or (it.get("item") or {}).get("name") if isinstance(it.get("item"), dict) else it.get("name")
                        if nm:
                            names.append(htmllib.unescape(str(nm)).strip())
                    if names:
                        return names
                stack.extend(d.get("@graph", []) if isinstance(d.get("@graph"), list) else [])
    return []


RX = lambda *parts: re.compile("|".join(parts), re.I)
T_PUBS = RX(r"/publi[ck]", r"/publications?/", r"/publikationen/", r"/bibliograph")
T_NEWS = RX(r"/news", r"/aktuell", r"/events?/", r"/veranstaltung", r"/medien", r"/media/", r"/press", r"/blog/",
            r"/agenda", r"/termine", r"/actualit")
T_ARCHIVE = RX(r"/category/", r"/tag/", r"/author/", r"/\d{4}/(\d{2}/)?$", r"/page/\d+/?$")
T_PROJ = RX(r"/projects?(_[a-z0-9]+)*/", r"/projekte?/", r"/forschungsprojekte", r"/research_projects", r"/projets?/")
T_INFRA = RX(r"/infrastruct", r"/infrastruktur", r"/facilit", r"/core_facilit", r"/plattform", r"/platforms?/",
             r"/services?/", r"/dienstleistung", r"/equipment", r"/geraete", r"/technolog", r"/laborato")
T_GROUP = RX(r"/research/[^/]+/(index_\w+\.html)?$", r"/(forschungsgruppen?|research_groups?|groups?|labs?|"
             r"arbeitsgruppen?|teams?|group_members|mitarbeitende|personen|people|persons|staff|team)/(index_\w+\.html)?$",
             r"/(forschungsgruppen?|research_groups?|arbeitsgruppen?|research_programs?|forschungsprogramme?)/[^/]+",
             r"[_-]lab(_\d+)?(/|$)", r"/labs?/[^/]+")
T_PERSON_URL = RX(r"/(persons?|personen|people|mitarbeitende|staff|team)/[^/]+/(index_\w+\.html)?$")
T_UNIT = RX(r"^/(index_\w+\.html)?$", r"/profil_seiten/", r"/(kliniken|clinics|institutes|institute)/[^/]+/(index_\w+\.html)?$", r"^/(de|en|fr)/?$", r"/(about|about_us|ueber_uns|institut|institute|"
            r"departement|department|organisation|organization|kontakt|contact)(/|_)")


def page_type_of(url: str, tree, title: str, crumbs: list[str]) -> str:
    path = urlparse(url).path or "/"
    if not path.endswith("/") and not re.search(r"\.\w{2,5}$", path):
        path += "/"
    person_schema = tree.css_first('[itemtype*="schema.org/Person"]') is not None and tree.css_first(
        'article.team-detail, [itemtype*="schema.org/Person"] h1, [itemtype*="schema.org/Person"] [itemprop=name]') is not None
    many_persons = len(tree.css('[itemtype*="schema.org/Person"]')) > 2
    if (person_schema and not many_persons) or (T_PERSON_URL.search(path) and not many_persons
                                                 and not re.search(r"/(team|people|personen)/?(index_\w+\.html)?$", path)):
        return "person"
    body = tree.css_first("body")
    bclass = set(((body.attributes.get("class") if body else "") or "").split())
    if "single-project" in bclass:
        return "project"
    if T_PUBS.search(path) or re.search(r"^(publications?|publikationen)\b", title, re.I):
        return "publications"
    if T_NEWS.search(path) or T_ARCHIVE.search(path) or bclass & {"single-post", "archive", "blog", "date", "category", "tag"}:
        return "news"
    if T_PROJ.search(path):
        return "project"
    if T_INFRA.search(path):
        return "infrastructure"
    if T_GROUP.search(path) or many_persons:
        return "group"
    if T_UNIT.search(path):
        return "unit"
    return "other"


# ---------------------------------------------------------------- per page

def process(rec: dict) -> dict:
    """Parse one cached page. Pure function of the raw file; result is cached by content hash."""
    from selectolax.lexbor import LexborHTMLParser as HTMLParser

    url = rec.get("final_url") or rec["url"]
    raw = (Path(rec.get("_raw", RAW)) / rec["file"]).read_text(errors="ignore")
    raw = deobfuscate_anchors(raw)
    raw = flatten_dl(raw)
    tree = HTMLParser(raw)

    html_node = tree.css_first("html")
    html_lang = None
    if html_node:
        html_lang = html_node.attributes.get("lang") or html_node.attributes.get("xml:lang")
    t = tree.css_first("title")
    title = " ".join(t.text().split()) if t else ""
    h1n = tree.css_first("h1")
    h1 = " ".join(h1n.text(separator=" ").split()) if h1n else ""
    crumbs = breadcrumbs_of(tree)
    links = lang_links(tree, url)
    ptype = page_type_of(url, tree, h1 or title, crumbs)

    # anchors that still carry an address the text may not show (mailto outside main text)
    href_emails = [n.attributes.get("data-email") for n in tree.css("span[data-email]")]

    # contact boxes sit in <aside>, which trafilatura drops; they name the group head and their email
    boxes = []
    for n in tree.css("aside.contact-box, .contact-box, aside[itemtype]"):
        out: list[str] = []
        _walk(n, out)
        bt = tidy("".join(out))
        if bt and bt not in boxes:
            boxes.append(bt)

    # site chrome out before text extraction (after reading the language switcher and breadcrumbs)
    for sel in PRESTRIP_SEL:
        for n in tree.css(sel):
            n.decompose()
    raw = tree.html or raw

    ttext = traf_text(raw, url)
    dtext = dom_text(HTMLParser(raw))
    # trafilatura drops card grids (the DCB team page loses ~50 names); fall back to the DOM walk then
    if words(ttext) >= 0.6 * words(dtext) and words(ttext) > 0:
        text, extractor = ttext, "trafilatura"
    else:
        text, extractor = dtext, "dom"
    for bt in boxes:
        body_lines = [l for l in bt.split("\n") if l.strip() and not l.startswith("#")]
        if body_lines and not all(l in text for l in body_lines):
            text = f"{text}\n\n{bt if bt.startswith('#') else '## Contact' + chr(10) + chr(10) + bt}"
    if not text.strip() and (h1 or title):
        text, extractor = f"# {h1 or title}", "title_only"  # empty page: keep it findable by its title
    if h1 and h1 not in text[:500]:
        text = f"# {h1}\n\n{text}"

    emails = emails_in_text(text)  # only addresses visible in the main text; chrome addresses stay out

    lang, lang_src = detect_lang(html_lang, url, text)
    return {
        "url": url, "title": title, "h1": h1, "text": text, "emails": emails, "lang": lang, "lang_source": lang_src,
        "lang_links": links, "breadcrumbs": crumbs, "page_type": ptype, "extractor": extractor,
        "n_words": words(text), "page_emails": list(dict.fromkeys(href_emails)),
    }


# ---------------------------------------------------------------- chunking

HEAD_LINE = re.compile(r"^(#{1,6})\s+(.*)$", re.M)


def sections_of(text: str, title: str) -> list[tuple[str, list[str]]]:
    secs: list[tuple[str, list[str]]] = []
    heading, buf = title, []
    for line in text.split("\n"):
        m = HEAD_LINE.match(line)
        if m:
            if any(l.strip() for l in buf):
                secs.append((heading, buf))
            heading, buf = m.group(2).strip(), [line]
        else:
            buf.append(line)
    if any(l.strip() for l in buf):
        secs.append((heading, buf))
    return secs


def _split_long(heading: str, body: str) -> list[str]:
    """Split a section over CHUNK_MAX words into ~CHUNK_TARGET word pieces on paragraph, then word, bounds."""
    paras = [p for p in re.split(r"\n\s*\n|\n(?=- )", body) if p.strip()]
    pieces, cur = [], []
    for p in paras:
        pw = p.split()
        if len(pw) > CHUNK_MAX:
            if cur:
                pieces.append("\n\n".join(cur)); cur = []
            for i in range(0, len(pw), CHUNK_TARGET):
                pieces.append(" ".join(pw[i:i + CHUNK_TARGET]))
            continue
        if cur and words("\n\n".join(cur)) + len(pw) > CHUNK_TARGET:
            pieces.append("\n\n".join(cur)); cur = []
        cur.append(p)
    if cur:
        pieces.append("\n\n".join(cur))
    head_line = f"# {heading}"
    return [pc if pc.lstrip().startswith("#") else f"{head_line}\n\n{pc}" for pc in pieces]


def chunk_text(text: str, title: str) -> list[tuple[str, str]]:
    """[(heading, text)]: one chunk per heading section, small sections merged up to ~CHUNK_MIN words,
    long ones split; every chunk starts with its heading line."""
    out: list[tuple[str, str]] = []
    pend_head, pend = None, []
    for heading, lines in sections_of(text, title):
        body = "\n".join(lines).strip()
        if not body.lstrip().startswith("#"):
            body = f"# {heading}\n\n{body}"
        w = words(body)
        if w > CHUNK_MAX:
            if pend:
                out.append((pend_head, "\n\n".join(pend))); pend_head, pend = None, []
            for piece in _split_long(heading, body):
                out.append((heading, piece))
            continue
        if pend and words("\n\n".join(pend)) + w > CHUNK_MAX:
            out.append((pend_head, "\n\n".join(pend))); pend_head, pend = None, []
        if not pend:
            pend_head = heading
        pend.append(body)
        if words("\n\n".join(pend)) >= CHUNK_MIN:
            out.append((pend_head, "\n\n".join(pend))); pend_head, pend = None, []
    if pend:
        out.append((pend_head, "\n\n".join(pend)))
    return [(h or title, t) for h, t in out if words(t) > 0]


# ---------------------------------------------------------------- sibling groups

class DSU:
    def __init__(self, langs: dict[str, str]):
        self.p = {k: k for k in langs}
        self.langs = {k: {v} for k, v in langs.items()}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        # one page per language per group: never merge two German pages into one entity
        if self.langs[ra] & self.langs[rb]:
            return False
        self.p[rb] = ra
        self.langs[ra] |= self.langs.pop(rb)
        return True


def link_siblings(pages: list[dict]) -> None:
    """Fill siblings[], sibling_urls[] and sibling_group in place.

    Two pages are siblings when (a) the URL pattern says so (index_ger <-> index_eng, /de/ <-> /en/) or
    (b) language links on the pages point at each other (hreflang or switcher) in both directions, or one
    way when the target links to no other page in the source's language. Never two pages of one language.
    """
    by_key: dict[str, str] = {}
    for p in pages:
        for u in [p["url"]] + p.get("alt_urls", []):
            by_key.setdefault(url_key(u), p["page_id"])
    lang_of = {p["page_id"]: p["lang"] for p in pages}
    dsu = DSU(lang_of)
    out_links: dict[str, set[str]] = defaultdict(set)
    for p in pages:
        for lang, u in p.get("lang_links", {}).items():
            t = by_key.get(url_key(u))
            if t and t != p["page_id"]:
                out_links[p["page_id"]].add(t)

    pairs = []
    for p in pages:
        a = p["page_id"]
        for u in url_variants(p["url"]):
            t = by_key.get(url_key(u))
            if t and t != a:
                pairs.append((a, t))
        for t in out_links[a]:
            if a in out_links.get(t, ()):  # reciprocal
                pairs.append((a, t))
            else:
                # one-way: accept only if the target's own switcher has no link back to another page in a's language
                back = [x for x in out_links.get(t, ()) if lang_of.get(x) == lang_of[a]]
                if not back and lang_of[a] != lang_of[t]:
                    pairs.append((a, t))
    for a, b in sorted(set(pairs)):
        if lang_of[a] != lang_of[b]:
            dsu.union(a, b)

    groups: dict[str, list[dict]] = defaultdict(list)
    for p in pages:
        groups[dsu.find(p["page_id"])].append(p)
    for members in groups.values():
        gid = "sg_" + min(m["page_id"] for m in members)
        for m in members:
            others = sorted((o for o in members if o is not m), key=lambda o: o["lang"])
            m["siblings"] = [o["page_id"] for o in others]
            m["sibling_urls"] = [o["url"] for o in others]
            m["sibling_group"] = gid


# ---------------------------------------------------------------- boilerplate

BOILER_SHARE, BOILER_MIN_PAGES = 0.30, 15


def _blocks(text: str) -> list[str]:
    return [b for b in re.split(r"\n\s*\n", text) if b.strip()]


def trim_boilerplate(pages: list[dict]) -> None:
    """Drop leading and trailing text blocks that repeat on many pages of one source (site footers and
    headers that sit inside the content area, like the DCB address block with its generic email).
    Only the head and tail are trimmed, so repeated lines inside a page ("Function: PhD Student") stay."""
    by_src: dict[str, list[dict]] = defaultdict(list)
    for p in pages:
        by_src[(p["source"], p["lang"])].append(p)
    for src, ps in by_src.items():
        if len(ps) < BOILER_MIN_PAGES:
            continue
        freq = Counter()
        for p in ps:
            freq.update(set(_blocks(p["text"])))
        limit = max(BOILER_MIN_PAGES, BOILER_SHARE * len(ps))
        common = {b for b, n in freq.items() if n >= limit}
        if not common:
            continue
        for p in ps:
            bl = _blocks(p["text"])
            i, j = 0, len(bl)
            while i < j and bl[i] in common and not bl[i].startswith("# "):
                i += 1
            while j > i and bl[j - 1] in common:
                j -= 1
            if (i, j) != (0, len(bl)) and j > i:
                p["text"] = "\n\n".join(bl[i:j])
                p["emails"] = emails_in_text(p["text"])
                p["n_words"] = words(p["text"])
                p["trimmed_blocks"] = len(bl) - (j - i)


# ---------------------------------------------------------------- driver

def load_index(raw_dir: Path) -> list[dict]:
    """Latest record per URL (same rule as crawl.load_index), keeping only cached 200 HTML pages."""
    idx: dict[str, dict] = {}
    f = raw_dir / "index.jsonl"
    if not f.exists():
        return []
    with open(f) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # the crawler may be mid-write on the last line
            idx[rec["url"]] = rec
    return [r for r in idx.values() if r.get("status") == 200 and r.get("file") and (raw_dir / r["file"]).exists()]


def pref_key(r: dict):
    """Which URL represents a deduped page: an explicit language URL (index_eng.html, /en/), then the shortest."""
    u = r.get("final_url") or r["url"]
    bare = not u.endswith("/") and not re.search(r"\.\w{2,5}$", urlparse(u).path)
    return (lang_from_url(u) is None, bare, len(u), u)


def cache_path(content_hash: str, cache: Path | None = None) -> Path:
    return (cache or CACHE) / content_hash[:2] / f"{content_hash[:16]}_v{PARSER_VERSION}.json"


def _process_cached(rec: dict) -> tuple[str, dict | None, str | None]:
    try:
        res = process(rec)
    except Exception as e:  # never lose a page: an unparsable page still gets a record
        return rec["content_hash"], None, f"{type(e).__name__}: {e}"
    cp = cache_path(rec["content_hash"], Path(rec["_cache"]) if rec.get("_cache") else None)
    cp.parent.mkdir(parents=True, exist_ok=True)
    tmp = cp.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False))
    os.replace(tmp, cp)
    return rec["content_hash"], res, None


def atomic_write_jsonl(path: Path, rows) -> int:
    tmp = path.with_name(path.name + ".tmp")
    n = 0
    with open(tmp, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    os.replace(tmp, path)
    return n


def run(raw_dir: Path = RAW, out_dir: Path = OUT_DIR, force: bool = False, workers: int | None = None,
        quiet: bool = False, cache_dir: Path | None = None) -> dict:
    cache_dir = cache_dir or (CACHE if out_dir == OUT_DIR else out_dir / "cache" / "clean")
    t0 = time.time()
    log = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    recs = load_index(raw_dir)
    latest: dict[str, dict] = {}
    aliases: dict[str, set[str]] = defaultdict(set)
    for r in recs:  # /en/team and /en/team/ are one page: keep the latest fetch
        k = url_key(r.get("final_url") or r["url"])
        aliases[k].update(x for x in (r["url"], r.get("final_url")) if x)
        if k not in latest or (r.get("fetched_at") or "") > (latest[k].get("fetched_at") or ""):
            latest[k] = r
    n_records = len(recs)
    recs = list(latest.values())
    for r in recs:
        r["_raw"], r["_cache"] = str(raw_dir), str(cache_dir)
        if not r.get("content_hash"):
            r["content_hash"] = hashlib.sha256((raw_dir / r["file"]).read_bytes()).hexdigest()

    # dedupe identical content: one page per content_hash, extra URLs kept as alt_urls
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_hash[r["content_hash"]].append(r)

    results: dict[str, dict] = {}
    todo = []
    for h, rs in by_hash.items():
        cp = cache_path(h, cache_dir)
        if not force and cp.exists():
            try:
                results[h] = json.loads(cp.read_text())
                continue
            except Exception:
                pass
        rs.sort(key=pref_key)
        todo.append(rs[0])
    errors = {}
    if todo:
        log(f"[clean] parsing {len(todo)} new pages ({len(results)} from cache)")
        if len(todo) > 20 and workers != 1:
            with ProcessPoolExecutor(max_workers=workers or min(8, os.cpu_count() or 2)) as ex:
                for h, res, err in ex.map(_process_cached, todo, chunksize=8):
                    if res is not None:
                        results[h] = res
                    else:
                        errors[h] = err
        else:
            for r in todo:
                h, res, err = _process_cached(r)
                if res is not None:
                    results[h] = res
                else:
                    errors[h] = err

    pages = []
    for h, rs in by_hash.items():
        rs.sort(key=pref_key)
        r0 = rs[0]
        res = results.get(h)
        url = min(aliases[url_key(r0.get("final_url") or r0["url"])] | {r0.get("final_url") or r0["url"]},
                  key=lambda u: pref_key({"url": u}))
        if res is None:
            res = {"url": url, "title": "", "h1": "", "text": "", "emails": [], "lang": r0.get("lang") or "und",
                   "lang_source": "crawl", "lang_links": {}, "breadcrumbs": [], "page_type": "other",
                   "extractor": "error", "n_words": 0, "page_emails": [], "error": errors.get(h)}
        alt = sorted({x for r in rs for x in aliases[url_key(r.get("final_url") or r["url"])] if x != url})
        pages.append({
            "page_id": h[:16], "url": url, "source": r0["source"], "group": r0.get("group", r0["source"]),
            "lang": res["lang"], "title": res["title"], "text": res["text"], "emails": res["emails"],
            "siblings": [], "fetched_at": r0.get("fetched_at"),
            "page_type": res["page_type"], "breadcrumbs": res["breadcrumbs"], "sibling_group": None,
            "h1": res["h1"], "content_hash": h, "alt_urls": alt, "lang_source": res["lang_source"],
            "lang_links": res["lang_links"], "extractor": res["extractor"], "n_words": res["n_words"],
            "depth": r0.get("depth"),
        })
    pages.sort(key=lambda p: (p["source"], p["url"]))
    trim_boilerplate(pages)
    link_siblings(pages)

    chunks = []
    for p in pages:
        for i, (heading, text) in enumerate(chunk_text(p["text"], p["h1"] or p["title"])):
            chunks.append({"chunk_id": f"{p['page_id']}_{i:03d}", "page_id": p["page_id"], "url": p["url"],
                           "source": p["source"], "lang": p["lang"], "heading": heading, "text": text,
                           "title": p["h1"] or p["title"], "page_type": p["page_type"],
                           "headings": [m.group(2).strip() for m in HEAD_LINE.finditer(text)] or [heading],
                           "sibling_group": p["sibling_group"], "n_words": words(text)})

    out_dir.mkdir(parents=True, exist_ok=True)
    n_chunks = atomic_write_jsonl(out_dir / "chunks.jsonl", chunks)
    n_pages = atomic_write_jsonl(out_dir / "pages.jsonl", pages)

    # gap 16: page counts per source against the last run
    per_src = Counter(p["source"] for p in pages)
    stats_f = cache_dir / "stats.json"
    warnings = []
    if stats_f.exists():
        try:
            prev = json.loads(stats_f.read_text()).get("pages_per_source", {})
        except Exception:
            prev = {}
        for src, n_prev in prev.items():
            n_now = per_src.get(src, 0)
            if n_prev and (n_prev - n_now) / n_prev > DROP_WARN:
                warnings.append(f"WARNING: source {src} dropped from {n_prev} to {n_now} pages "
                                f"({100 * (n_prev - n_now) / n_prev:.0f}%), check the include patterns / site redesign")
    for w in warnings:
        print(w, file=sys.stderr, flush=True)
    stats = {"run_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pages": n_pages, "chunks": n_chunks,
             "raw_records": n_records, "parsed_now": len(todo), "errors": len(errors),
             "pages_per_source": dict(sorted(per_src.items())),
             "chunks_per_source": dict(sorted(Counter(c["source"] for c in chunks).items())),
             "sibling_groups_multi": sum(1 for g in Counter(p["sibling_group"] for p in pages).values() if g > 1),
             "warnings": warnings, "seconds": round(time.time() - t0, 1)}
    stats_f.parent.mkdir(parents=True, exist_ok=True)
    tmp = stats_f.with_suffix(".tmp")
    tmp.write_text(json.dumps(stats, indent=2))
    os.replace(tmp, stats_f)
    log(f"[clean] {n_pages} pages, {n_chunks} chunks from {n_records} cached 200 records "
        f"({len(todo)} parsed, {len(errors)} errors) in {stats['seconds']}s")
    return stats


def check(raw_dir: Path = RAW, out_dir: Path = OUT_DIR) -> bool:
    """Every cached 200 HTML page maps to a page in pages.jsonl (directly or as a deduped alt_url)."""
    recs = load_index(raw_dir)
    pages = [json.loads(l) for l in open(out_dir / "pages.jsonl")]
    covered = {p["content_hash"] for p in pages}
    urls = {u for p in pages for u in [p["url"], *p.get("alt_urls", [])]}
    chunked = {json.loads(l)["page_id"] for l in open(out_dir / "chunks.jsonl")}
    missing = [r["url"] for r in recs if r.get("content_hash") not in covered
               and r["url"] not in urls and r.get("final_url") not in urls]
    empty = [p["url"] for p in pages if p["page_id"] not in chunked]
    print(f"cached 200 records: {len(recs)}  pages: {len(pages)}  missing: {len(missing)}  "
          f"pages without chunks: {len(empty)}")
    for u in missing[:10]:
        print("  missing", u)
    for u in empty[:10]:
        print("  no chunks", u)
    return not missing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default=str(RAW))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--force", action="store_true", help="reparse every page, ignore the cache")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--check", action="store_true", help="only check coverage of the existing output")
    a = ap.parse_args(argv)
    if a.check:
        sys.exit(0 if check(Path(a.raw), Path(a.out)) else 1)
    run(Path(a.raw), Path(a.out), force=a.force, workers=a.workers)


if __name__ == "__main__":
    main()
