"""Triage web app.

A small local server the tablet points at. It serves one self-contained page --
no CDN, no external fonts -- because the Tab S10 may be on a flaky connection
and because the page must install to the home screen as a PWA, which needs
everything to work offline-ish and over a secure context.

The API is deliberately tiny: list what is pending, record a decision. Anything
heavier (downloading, uploading) is the CLI's job and runs on the laptop, so a
tap never waits on a 30 MB download.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .ledger import Decision, Ledger

STATIC = Path(__file__).parent / "static"


class DecisionIn(BaseModel):
    decision: Decision


def _paper_json(p) -> dict[str, Any]:
    """Flatten a ledger row into what the page needs.

    The abstract falls back to Scholar's snippet, and says which it is: the
    user asked that a paper never be hidden for want of metadata, but a
    two-line snippet should not masquerade as an abstract.
    """
    d = p.payload
    enrichment = d.get("enrichment") or {}
    abstract = enrichment.get("abstract")
    return {
        "key": p.key,
        "title": p.title,
        "authors": d.get("authors") or [],
        "venue": d.get("venue"),
        "year": enrichment.get("year") or d.get("year"),
        "abstract": abstract or d.get("snippet"),
        "abstract_is_snippet": not abstract,
        "url": d.get("url"),
        "alert_query": d.get("alert_query"),
        "has_pdf": bool(enrichment.get("pdf_url")) or bool(d.get("has_pdf_badge")),
        "doi": enrichment.get("doi"),
    }


def create_app(ledger_path: Path) -> FastAPI:
    app = FastAPI(title="paper-grabber", docs_url=None, redoc_url=None)

    def open_ledger() -> Ledger:
        # SQLite connections are not shareable across threads, so each request
        # opens its own. At this volume the cost is irrelevant.
        return Ledger(ledger_path)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text()

    @app.get("/sw.js")
    def service_worker() -> PlainTextResponse:
        return PlainTextResponse(
            (STATIC / "sw.js").read_text(), media_type="application/javascript"
        )

    @app.get("/manifest.webmanifest")
    def manifest() -> JSONResponse:
        return JSONResponse(json.loads((STATIC / "manifest.webmanifest").read_text()))

    @app.get("/api/pending")
    def pending() -> dict[str, Any]:
        with open_ledger() as led:
            return {
                "papers": [_paper_json(p) for p in led.pending()],
                "counts": led.counts(),
            }

    @app.post("/api/papers/{key}/decision")
    def decide(key: str, body: DecisionIn) -> dict[str, Any]:
        with open_ledger() as led:
            if not led.decide_by_key(key, body.decision):
                raise HTTPException(status_code=404, detail="no such paper")
            return {"key": key, "decision": body.decision.value, "counts": led.counts()}

    @app.get("/api/papers/{key}")
    def get_paper(key: str) -> dict[str, Any]:
        with open_ledger() as led:
            paper = led.get(key)
            if paper is None:
                raise HTTPException(status_code=404, detail="no such paper")
            return _paper_json(paper)

    return app
