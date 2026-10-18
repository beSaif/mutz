"""Placeholder app so the container runs from phase 0. Agent F replaces this."""
from fastapi import FastAPI

app = FastAPI(title="mutz")


@app.get("/health")
def health():
    return {"status": "ok"}
