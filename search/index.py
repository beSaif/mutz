"""Hybrid index over data/chunks.jsonl: BM25 (DE/EN/FR aware tokens) + BGE-M3 dense vectors.

    python -m search.index                     # BM25 + embed new chunk_ids (MPS on a Mac, else CPU)
    python -m search.index --download-models   # fetch open weights once (BGE-M3 + reranker), then exit
    python -m search.index --no-embed          # BM25 only
    python -m search.index --stats

Writes data/index/: bm25.pkl, emb.npy (float16, L2 normalised), emb_ids.json, meta.json.
Incremental: only chunk_ids without a vector are embedded; vectors of chunks that disappeared are dropped.

Routing rule (gap 1): models are loaded from the local Hugging Face cache by path, with
HF_HUB_OFFLINE=1 once they are cached. Nothing here calls a hosted inference API.
"""
import argparse, json, math, os, pickle, subprocess, sys, time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from search.text import tokenize

DATA = Path(os.environ.get("MUTZ_DATA", "data"))
CHUNKS = DATA / "chunks.jsonl"
PAGES = DATA / "pages.jsonl"
IDX = DATA / "index"

EMBED_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")


# ---------------------------------------------------------------- model cache (no network)

def hf_cache_dir() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    return home / "hub"


def cached_snapshot(repo: str) -> Path | None:
    """Local snapshot dir of a model if config + weights are on disk. Pure filesystem check."""
    d = hf_cache_dir() / f"models--{repo.replace('/', '--')}"
    ref = d / "refs" / "main"
    snaps = []
    if ref.exists():
        snaps.append(d / "snapshots" / ref.read_text().strip())
    if (d / "snapshots").exists():
        snaps += sorted((d / "snapshots").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for s in snaps:
        if (s / "config.json").exists() and any((s / w).exists() for w in _WEIGHT_FILES) \
                and (s / "tokenizer.json").exists():
            return s
    return None


def download_models(repos=None):
    """Open weights download (allowed); never inference. Skips files we do not use (onnx, colbert)."""
    os.environ.pop("HF_HUB_OFFLINE", None)
    from huggingface_hub import snapshot_download
    ignore = ["onnx/*", "*.onnx", "*.onnx_data", "colbert_linear.pt", "sparse_linear.pt",
              "imgs/*", "assets/*", "*.jpg", "*.png", "*.webp"]
    for repo in repos or (EMBED_MODEL, RERANK_MODEL):
        if cached_snapshot(repo):
            print(f"{repo}: cached at {cached_snapshot(repo)}", flush=True)
            continue
        p = snapshot_download(repo, ignore_patterns=ignore)
        print(f"{repo}: downloaded to {p}", flush=True)


_bg_download = {}


def ensure_models(repos=(EMBED_MODEL, RERANK_MODEL), wait=True):
    """Make sure the given models are on disk, then switch the HF libraries to offline mode.
    A missing model is fetched in a child process so this process never imports the hub online.
    wait=False (query time): start the download in the background once and raise at once, so the
    caller degrades instead of blocking a query for the length of a multi GB download."""
    missing = [r for r in repos if not cached_snapshot(r)]
    if missing:
        if os.environ.get("MUTZ_ALLOW_MODEL_DOWNLOAD", "1") == "0":
            raise RuntimeError(f"models not cached: {missing}. Run: python -m search.index --download-models")
        env = {k: v for k, v in os.environ.items() if k != "HF_HUB_OFFLINE"}
        cmd = [sys.executable, "-m", "search.index", "--download-models", *missing]
        if not wait:
            key = tuple(missing)
            p = _bg_download.get(key)
            if p is None or p.poll() is not None:
                _bg_download[key] = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL, start_new_session=True)
            raise RuntimeError(f"models not cached yet, downloading in the background: {missing}")
        print(f"[search] downloading open weights once: {missing}", file=sys.stderr, flush=True)
        subprocess.run(cmd, check=True, env=env, stdout=sys.stderr)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def pick_device(want: str | None = None) -> str:
    want = want or os.environ.get("MUTZ_DEVICE")
    if want:
        return want
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _cpu_dtype():
    """Query time on CPU (the container): bfloat16 halves memory (two ~2.2 GB models -> ~1.1 GB each),
    so the app and a CLI query fit in the Docker VM together. MUTZ_CPU_DTYPE=float32 to turn it off."""
    import torch
    return getattr(torch, os.environ.get("MUTZ_CPU_DTYPE", "bfloat16"))


def load_embedder(device=None, half=False, wait=True):
    ensure_models((EMBED_MODEL,), wait=wait)
    import torch
    from sentence_transformers import SentenceTransformer
    dev = pick_device(device)
    m = SentenceTransformer(str(cached_snapshot(EMBED_MODEL)), device=dev, local_files_only=True)
    if half and dev != "cpu":
        m = m.half()
    elif dev == "cpu" and _cpu_dtype() != torch.float32:
        m = m.to(_cpu_dtype())
    m.max_seq_length = int(os.environ.get("MUTZ_MAX_SEQ", "512"))
    return m


def load_reranker(device=None, wait=True):
    ensure_models((RERANK_MODEL,), wait=wait)
    import torch
    from sentence_transformers import CrossEncoder
    dev = pick_device(device)
    m = CrossEncoder(str(cached_snapshot(RERANK_MODEL)), device=dev, max_length=384, local_files_only=True)
    if dev == "cpu" and _cpu_dtype() != torch.float32:
        m.model.to(_cpu_dtype())
    return m


def encode(model, texts, batch=16):
    """L2 normalised float32 numpy vectors, whatever dtype the model runs in."""
    t = model.encode(texts, batch_size=batch, normalize_embeddings=True, convert_to_tensor=True,
                     show_progress_bar=False)
    return t.float().cpu().numpy()


def rerank_logits(model, pairs, batch=16):
    import torch
    kw = dict(batch_size=batch, show_progress_bar=False, convert_to_tensor=True)
    try:
        t = model.predict(pairs, activation_fn=torch.nn.Identity(), **kw)
    except TypeError:   # sentence-transformers 3.x
        t = model.predict(pairs, activation_fct=torch.nn.Identity(), **kw)
    return t.float().cpu().numpy().reshape(-1)


# ---------------------------------------------------------------- data

def read_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # a line being written right now
    return out


def load_chunks():
    """Current chunks, later lines win per chunk_id, order stable."""
    by_id = {}
    for c in read_jsonl(CHUNKS):
        if c.get("chunk_id") and c.get("text"):
            by_id[c["chunk_id"]] = c
    return list(by_id.values())


def load_pages():
    pages = {}
    for p in read_jsonl(PAGES):
        p = dict(p)
        p.pop("text", None)
        pages[p["page_id"]] = p
    return pages


def chunk_text_for_index(c, page=None):
    title = (page or {}).get("title") or ""
    head = c.get("heading") or ""
    return f"{title}\n{head}\n{c['text']}".strip()


def file_sig(path: Path):
    if not path.exists():
        return None
    st = path.stat()
    return [st.st_size, int(st.st_mtime)]


# ---------------------------------------------------------------- BM25

class BM25:
    """Okapi BM25 with an inverted index (numpy postings), fast for a few 10k chunks."""

    def __init__(self, k1=1.2, b=0.75):
        self.k1, self.b = k1, b

    def build(self, ids, token_lists):
        self.ids = list(ids)
        n = len(token_lists)
        self.doc_len = np.array([len(t) for t in token_lists], dtype=np.float32)
        self.avgdl = float(self.doc_len.mean()) if n else 1.0
        post = defaultdict(lambda: ([], []))
        for i, toks in enumerate(token_lists):
            for tok, tf in Counter(toks).items():
                post[tok][0].append(i)
                post[tok][1].append(tf)
        self.post = {t: (np.array(d, dtype=np.int32), np.array(f, dtype=np.float32)) for t, (d, f) in post.items()}
        self.idf = {t: math.log(1 + (n - len(d) + 0.5) / (len(d) + 0.5)) for t, (d, _) in self.post.items()}
        self.n = n
        return self

    @property
    def vocab(self):
        return self.post.keys()

    def scores(self, tokens):
        s = np.zeros(self.n, dtype=np.float32)
        norm = self.k1 * (1 - self.b + self.b * self.doc_len / self.avgdl)
        for tok in set(tokens):
            if tok not in self.post:
                continue
            d, tf = self.post[tok]
            s[d] += self.idf[tok] * tf * (self.k1 + 1) / (tf + norm[d])
        return s


def build_bm25(chunks, pages):
    toks = [tokenize(chunk_text_for_index(c, pages.get(c["page_id"]))) for c in chunks]
    return BM25().build([c["chunk_id"] for c in chunks], toks)


def load_or_build_bm25(chunks, pages, save=True):
    f = IDX / "bm25.pkl"
    sig = [file_sig(CHUNKS), file_sig(PAGES), len(chunks)]
    if f.exists():
        try:
            with open(f, "rb") as fh:
                obj = pickle.load(fh)
            if obj.get("sig") == sig:
                return obj["bm25"]
        except Exception:
            pass
    bm = build_bm25(chunks, pages)
    if save:
        IDX.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump({"sig": sig, "bm25": bm}, fh)
        os.replace(tmp, f)
    return bm


# ---------------------------------------------------------------- dense

def load_dense():
    """(ids list, float16 matrix) or ([], None)."""
    ef, idf = IDX / "emb.npy", IDX / "emb_ids.json"
    if not (ef.exists() and idf.exists()):
        return [], None
    ids = json.loads(idf.read_text())
    emb = np.load(ef, mmap_mode="r")
    if len(ids) != emb.shape[0]:
        return [], None
    return ids, emb


def embed_new(chunks, pages, device=None, batch=16, save_every=1024):
    """Embed only chunk_ids that do not have a vector yet. Saves progress as it goes."""
    ids, emb = load_dense()
    have = {cid: i for i, cid in enumerate(ids)}
    current = {c["chunk_id"] for c in chunks}
    keep = [cid for cid in ids if cid in current]
    vecs = [np.asarray(emb[have[cid]]) for cid in keep] if keep else []
    todo = [c for c in chunks if c["chunk_id"] not in have]
    print(f"[index] dense: {len(keep)} kept, {len(ids) - len(keep)} dropped, {len(todo)} to embed", flush=True)
    if not todo:
        _save_dense(keep, vecs)
        return len(keep)
    model = load_embedder(device, half=True)
    print(f"[index] embedding on {model.device}", flush=True)
    t0 = time.time()
    out_ids, out_vecs = list(keep), list(vecs)
    for start in range(0, len(todo), save_every):
        part = todo[start:start + save_every]
        texts = [chunk_text_for_index(c, pages.get(c["page_id"])) for c in part]
        v = encode(model, texts, batch).astype(np.float16)
        out_ids += [c["chunk_id"] for c in part]
        out_vecs += list(v)
        _save_dense(out_ids, out_vecs)
        done = start + len(part)
        rate = done / max(time.time() - t0, 1e-6)
        print(f"[index] {done}/{len(todo)} embedded, {rate:.1f} chunks/s", flush=True)
    return len(out_ids)


def _save_dense(ids, vecs):
    IDX.mkdir(parents=True, exist_ok=True)
    mat = np.stack(vecs).astype(np.float16) if vecs else np.zeros((0, 1024), dtype=np.float16)
    tmp = IDX / "emb.tmp.npy"
    np.save(tmp, mat)
    (IDX / "emb_ids.tmp.json").write_text(json.dumps(ids))
    os.replace(tmp, IDX / "emb.npy")
    os.replace(IDX / "emb_ids.tmp.json", IDX / "emb_ids.json")


def build(embed=True, device=None, batch=16):
    t0 = time.time()
    chunks, pages = load_chunks(), load_pages()
    if not chunks:
        print(f"[index] no chunks in {CHUNKS}", flush=True)
        return
    bm = load_or_build_bm25(chunks, pages)
    print(f"[index] bm25: {bm.n} chunks, {len(bm.post)} terms ({time.time() - t0:.1f}s)", flush=True)
    n_dense = embed_new(chunks, pages, device, batch) if embed else len(load_dense()[0])
    meta = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "chunks": len(chunks), "pages": len(pages),
            "bm25_terms": len(bm.post), "dense_vectors": n_dense, "embed_model": EMBED_MODEL,
            "rerank_model": RERANK_MODEL, "secs": round(time.time() - t0, 1)}
    (IDX / "meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta), flush=True)


def stats():
    meta = IDX / "meta.json"
    print(meta.read_text() if meta.exists() else "{}")
    for f in sorted(IDX.glob("*")):
        print(f"{f.name}\t{f.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--download-models", nargs="*", metavar="REPO", default=None,
                    help="fetch open weights (default: both models)")
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--device")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.download_models is not None:
        download_models(a.download_models)
    elif a.stats:
        stats()
    else:
        build(embed=not a.no_embed, device=a.device, batch=a.batch)
