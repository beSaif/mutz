"""Text helpers for search: DE/EN/FR aware tokenisation, quote picking, deep links.

Pure Python, no models, so it is cheap to import and easy to test.
"""
import re
import unicodedata
from urllib.parse import quote as _urlquote

# Small stop word lists for DE, EN, FR. BM25 idf would down weight these anyway; dropping them keeps
# postings small and stops "der" or "the" from matching every chunk.
STOP = set("""
a an the and or of to in on for with by from at is are was were be been as this that these those it its
we our their they he she his her you your not no can may also into than then there which who whom what
how when where why all any each more most other some such only own same so very s t will just about
der die das den dem des ein eine einer eines einem einen und oder zu zum zur von vom mit fur fuer auf aus bei
im in ist sind war wird werden wurde als auch an am um uber ueber unter nach vor durch sich nicht kein keine
wir unser unsere ihr ihre sie er es man dass wie was wo wer bzw sowie oder dieser diese dieses jedes
le la les l un une des du de d et en pour par sur au aux dans est sont avec qui que qu ce cette ces son sa ses
leur leurs nous vous il elle ils elles ne pas plus ou y
""".split())

_TOKEN = re.compile(r"[a-z0-9]+")
_SUFFIXES = ("ungen", "ung", "en", "er", "es", "e", "s", "n")  # longest first


def fold(text: str) -> str:
    """Lowercase, strip accents (ä -> a, é -> e), so DE/FR spellings meet."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def stem(tok: str) -> str:
    """Very light suffix stripping shared by EN/DE/FR: sensors -> sensor, Sensoren -> sensor,
    Implantate -> implantat. Deliberately conservative: only tokens longer than 4 chars, one suffix."""
    if len(tok) <= 4 or tok.isdigit():
        return tok
    for suf in _SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 4:
            return tok[: -len(suf)]
    return tok


def tokenize(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(fold(text)):
        if len(t) < 2 or t in STOP:
            continue
        out.append(stem(t))
    return out


def decompound(tok: str, vocab) -> list[str]:
    """German compounds: 'knochenimplantat' -> ['knoch', 'implantat'] when both halves are in the
    index vocabulary (stemmed). Only used on the query side, as extra tokens."""
    if len(tok) < 9:
        return []
    best = []
    for i in range(4, len(tok) - 3):
        left, right = tok[:i], tok[i:]
        for l in (left, left[:-1] if left.endswith(("s", "n")) else None):
            if not l or len(l) < 4:
                continue
            ls, rs = stem(l), stem(right)
            if ls in vocab and rs in vocab:
                cand = [ls, rs]
                if len("".join(cand)) > len("".join(best)):
                    best = cand
    return best


# ---------------------------------------------------------------- quotes

_MD_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s+)")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜÉÈÀ0-9\"'(])")


def sentences(text: str) -> list[str]:
    """Verbatim spans of the chunk text: one per sentence, markdown markers stripped from the start.
    Every returned span is a substring of `text`."""
    out = []
    for line in (text or "").split("\n"):
        m = _MD_PREFIX.match(line)
        line = line[m.end():] if m else line
        line = line.strip()
        if not line:
            continue
        for s in _SENT_SPLIT.split(line):
            s = s.strip()
            if s:
                out.append(s)
    return out


def best_quote(text: str, terms: set[str], min_words=6, max_words=45) -> str:
    """Pick the sentence of the chunk that shares the most (stemmed) query terms. Verbatim.
    Sentences that are too long are cut at a word boundary (still a verbatim prefix)."""
    sents = sentences(text)
    if not sents:
        return (text or "").strip()[:300]
    best, best_score = None, -1.0
    for i, s in enumerate(sents):
        toks = tokenize(s)
        if not toks:
            continue
        hits = len(set(toks) & terms)
        n = len(s.split())
        score = hits * 2 + (0.5 if n >= min_words else 0) - 0.01 * i  # prefer early ties
        if n < 3:
            score -= 1
        if score > best_score:
            best, best_score = s, score
    best = best or sents[0]
    words = best.split()
    if len(words) > max_words:
        # cut verbatim at the end of the max_words-th word
        cut = 0
        for _ in range(max_words):
            m = re.search(r"\S+", best[cut:])
            if not m:
                break
            cut += m.end()
        best = best[:cut]
    return best


def _frag(s: str) -> str:
    # Text fragment syntax reserves '-', ',' and '&': percent encode everything except letters/digits.
    return _urlquote(s, safe="").replace("-", "%2D")


def deep_link(url: str, quote: str) -> str:
    """URL with a #:~:text= fragment that scrolls to and highlights the quote in modern browsers.
    Long quotes use textStart,textEnd so small whitespace differences in the middle do not break it."""
    base = url.split("#", 1)[0]
    words = quote.split()
    if not words:
        return base
    if len(words) <= 10:
        frag = _frag(" ".join(words))
    else:
        frag = _frag(" ".join(words[:5])) + "," + _frag(" ".join(words[-4:]))
    return f"{base}#:~:text={frag}"
