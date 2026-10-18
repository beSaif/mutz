# Keera gateway: smoke test results

Measured 2026-10-18 ~15:05 from the venue. Raw output: `logs/gateway_smoke.json` (gitignored).

- Base URL: `https://gateway.keera.ch/api/v1` (OpenAI compatible). Key from `KEERA_API_KEY` in `.env`, read via `os.environ`.
- Models served (use these exact ids):
  - `apertus-v1-5-8b`: forced `tool_choice` works. ~0.4 s for a short extraction.
  - `apertus-v1-5-70b`: forced `tool_choice` works. ~1.9 s for a short extraction.
  - `apertus-v1-5-8b-thinking`, `apertus-v1-5-70b-thinking`: forced tool_choice **fails** with
    400 `tool_choice=function ... requires --tool-call-parser to be set`. Use plain prompt in, JSON text out, validate with Pydantic.
- Concurrency: 2, 4, 8 and 16 parallel calls all succeeded on both 8B and 70B, no 429 seen.
  The gateway is shared with every team, so:
  - **Bulk extraction (agent B only): 8 concurrent requests**, back off on any 429/5xx (exponential, honour Retry-After).
  - Interactive app calls: no explicit limit, but keep at most 4 in flight per user action.
- The 8B model sometimes shortens quotes (dropped the subject of the sentence). The verifier must check quotes against page text, never trust them.
- Cache every call on disk by sha256(model + messages + tools), log model, tokens in, tokens out to `logs/gateway.jsonl`.
