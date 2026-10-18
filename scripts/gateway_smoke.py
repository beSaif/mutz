"""Gateway smoke test: list models, check forced tool_choice, probe concurrency. Never prints the key."""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(base_url="https://gateway.keera.ch/api/v1", api_key=os.environ["KEERA_API_KEY"], max_retries=0)
out = {}
try:
    out["models"] = sorted(m.id for m in client.models.list().data)
except Exception as e:
    out["models_error"] = f"{type(e).__name__}: {str(e)[:300]}"
print(json.dumps(out, indent=1), flush=True)

TOOL = {"type": "function", "function": {"name": "record_facts", "description": "Save facts with exact quotes",
        "parameters": {"type": "object", "properties": {"people": {"type": "array", "items": {"type": "object",
        "properties": {"name": {"type": "string"}, "function": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["name", "quote"]}}}, "required": ["people"]}}}
PAGE = "Prof. Dr. Stavroula Mougiakakou leads the AI in Health and Nutrition lab at the ARTORG Center."
cands = [m for m in out.get("models", []) if "apertus" in m.lower()] or ["apertus-v1-5-8b", "apertus-v1-5-70b"]
res = {}
for m in cands:
    t = time.time()
    try:
        r = client.chat.completions.create(model=m, messages=[{"role": "system", "content": "Extract people. Quote exactly."},
              {"role": "user", "content": PAGE}], tools=[TOOL],
              tool_choice={"type": "function", "function": {"name": "record_facts"}}, max_tokens=300)
        tc = r.choices[0].message.tool_calls
        res[m] = {"forced_tool_ok": bool(tc), "args": json.loads(tc[0].function.arguments) if tc else None,
                  "content": (r.choices[0].message.content or "")[:200], "secs": round(time.time() - t, 1),
                  "usage": r.usage.model_dump() if r.usage else None}
    except Exception as e:
        res[m] = {"error": f"{type(e).__name__}: {str(e)[:300]}", "secs": round(time.time() - t, 1)}
    print(m, json.dumps(res[m])[:600], flush=True)

def one(m):
    t = time.time()
    try:
        client.chat.completions.create(model=m, messages=[{"role": "user", "content": "Say OK."}], max_tokens=5)
        return ("ok", round(time.time() - t, 1))
    except Exception as e:
        return (getattr(e, "status_code", type(e).__name__), round(time.time() - t, 1))

conc = {}
for m in [c for c in cands if "error" not in res.get(c, {})][:2]:
    conc[m] = {}
    for n in (2, 4, 8, 16):
        with ThreadPoolExecutor(n) as ex:
            r = list(ex.map(one, [m] * n))
        conc[m][n] = r
        print(m, n, r, flush=True)
        if any(x[0] != "ok" for x in r):
            break
        time.sleep(1)
json.dump({"models": out, "tool": res, "concurrency": conc}, open("logs/gateway_smoke.json", "w"), indent=1, default=str)
