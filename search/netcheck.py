"""Network proof for the routing rule (gap 1): run one full query and log every host contacted.

    python -m search.netcheck "glucose sensors"

The socket layer is patched BEFORE anything else is imported: every DNS lookup and connect is recorded,
and any connection to a Hugging Face host raises, so a hidden hub call would fail loudly instead of
passing quietly. Prints one JSON line: {"hosts": [...], "hf_calls": [...], "hf_offline": "1", ...}.
"""
import json, socket, sys, time

HF_MARKERS = ("huggingface", "hf.co", "hf-mirror")
_seen, _hf = [], []
_gai, _conn, _cc = socket.getaddrinfo, socket.socket.connect, socket.create_connection


def _check(host, how):
    host = str(host)
    _seen.append(f"{how}:{host}")
    if any(m in host.lower() for m in HF_MARKERS):
        _hf.append(f"{how}:{host}")
        raise OSError(f"netcheck: blocked call to Hugging Face host {host}")


def gai(host, *a, **kw):
    _check(host, "dns")
    return _gai(host, *a, **kw)


def conn(self, addr):
    _check(addr[0] if isinstance(addr, tuple) else addr, "connect")
    return _conn(self, addr)


def cc(addr, *a, **kw):
    _check(addr[0], "create_connection")
    return _cc(addr, *a, **kw)


socket.getaddrinfo, socket.socket.connect, socket.create_connection = gai, conn, cc


def main():
    import os
    text = " ".join(sys.argv[1:]) or "glucose sensors"
    from search.query import query
    t0 = time.time()
    out = query(text)
    rep = {"query": text, "hosts": sorted(set(_seen)), "hf_calls": _hf,
           "hf_offline": os.environ.get("HF_HUB_OFFLINE"), "transformers_offline": os.environ.get("TRANSFORMERS_OFFLINE"),
           "expansion_source": out.get("expansion_source"), "results": len(out["results"]),
           "top": [r["name"] for r in out["results"][:3]], "secs": round(time.time() - t0, 2)}
    print(json.dumps(rep, ensure_ascii=False))
    return 0 if not _hf else 1


if __name__ == "__main__":
    sys.exit(main())
