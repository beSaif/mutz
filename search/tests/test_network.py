"""Routing rule (gap 1): one full query with the real local models makes zero Hugging Face calls.
Skipped until both models are in the local cache."""
import json, os, subprocess, sys
from pathlib import Path

import pytest

from search.index import EMBED_MODEL, RERANK_MODEL, cached_snapshot

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not cached_snapshot(EMBED_MODEL), reason="BGE-M3 not cached")
def test_query_makes_no_hf_calls():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MUTZ_NO_", "HF_HUB_OFFLINE"))}
    p = subprocess.run([sys.executable, "-m", "search.netcheck", "glucose sensors"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    rep = json.loads(p.stdout.strip().splitlines()[-1])
    assert rep["hf_calls"] == [], rep
    assert rep["hf_offline"] == "1"
    dns = [h for h in rep["hosts"] if h.startswith("dns:")]
    assert all("keera" in h for h in dns), dns   # only the gateway (expansions), if anything
    assert rep["results"] > 0
