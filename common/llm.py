"""Shared Keera gateway client: every Apertus call in mutz goes through chat().

- Disk cache keyed by sha256(model + messages + tools + tool_choice + params), so reruns cost nothing.
- Logs model, tokens in, tokens out, latency and cache hits to logs/gateway.jsonl.
- Retries 429 / 5xx with exponential backoff, honouring Retry-After.
- The key is read from KEERA_API_KEY (.env); it is never printed or logged.
"""
import hashlib, json, os, threading, time
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI

load_dotenv()
BASE_URL = os.environ.get("KEERA_BASE_URL", "https://gateway.keera.ch/api/v1")
CACHE = Path(os.environ.get("MUTZ_LLM_CACHE", "data/cache/gateway"))
LOG = Path("logs/gateway.jsonl")

SMALL = "apertus-v1-5-8b"            # bulk extraction, forced tool calls work
LARGE = "apertus-v1-5-70b"           # needs, expansions, reasons, Mutz; forced tool calls work
THINKING = "apertus-v1-5-70b-thinking"  # no tool_choice: prompt in, JSON text out

_client = None
_lock = threading.Lock()


def client():
    global _client
    if _client is None:
        _client = OpenAI(base_url=BASE_URL, api_key=os.environ["KEERA_API_KEY"], max_retries=0, timeout=120)
    return _client


def _key(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _log(rec):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def chat(model, messages, tools=None, tool_choice=None, cache=True, retries=6, tag=None, **params):
    """Call the gateway and return a plain dict:
    {"content": str, "tool_calls": [{"name", "arguments" (parsed dict or raw str)}], "usage": {...}, "cached": bool}
    """
    payload = {"model": model, "messages": messages, "tools": tools, "tool_choice": tool_choice, "params": params}
    key = _key(payload)
    path = CACHE / key[:2] / f"{key}.json"
    if cache and path.exists():
        out = json.loads(path.read_text())
        out["cached"] = True
        _log({"model": model, "cached": True, "tag": tag, "key": key[:16]})
        return out

    kwargs = dict(model=model, messages=messages, **params)
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    delay, t0 = 2.0, time.time()
    for attempt in range(retries + 1):
        try:
            r = client().chat.completions.create(**kwargs)
            break
        except APIStatusError as e:
            if e.status_code not in (429, 500, 502, 503, 504) or attempt == retries:
                _log({"model": model, "error": e.status_code, "tag": tag})
                raise
            retry = e.response.headers.get("retry-after", "") if e.response is not None else ""
            time.sleep(float(retry) if retry.replace(".", "", 1).isdigit() else delay)
            delay = min(delay * 2, 60)
        except APIConnectionError:
            if attempt == retries:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 60)

    msg = r.choices[0].message
    calls = []
    for tc in msg.tool_calls or []:
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            args = tc.function.arguments
        calls.append({"name": tc.function.name, "arguments": args})
    usage = {"in": r.usage.prompt_tokens, "out": r.usage.completion_tokens} if r.usage else {}
    out = {"content": msg.content or "", "tool_calls": calls, "usage": usage, "finish_reason": r.choices[0].finish_reason}
    if cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False))
    _log({"model": model, "cached": False, "tag": tag, "key": key[:16], "tokens_in": usage.get("in"),
          "tokens_out": usage.get("out"), "secs": round(time.time() - t0, 2)})
    out["cached"] = False
    return out


def tool(name, description, parameters):
    """OpenAI tool definition helper."""
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def force(name):
    return {"type": "function", "function": {"name": name}}
