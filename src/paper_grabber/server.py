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
from fastapi import Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from pydantic import BaseModel

from .drive import DriveClient, DriveError
from .google_auth import DRIVE_SCOPES, AuthError, load_credentials
from .oauth_web import (
    CALLBACK_PATH,
    OAuthError,
    WebOAuth,
    callback_url,
    is_valid_redirect,
    redirect_hint,
)
from .ledger import (
    SETTING_BASE_FOLDER_ID,
    SETTING_BASE_FOLDER_NAME,
    Decision,
    Ledger,
    paper_view,
)

STATIC = Path(__file__).parent / "static"


class DecisionIn(BaseModel):
    decision: Decision


class DestinationIn(BaseModel):
    folder_id: str
    folder_name: str
    keys: list[str]


class BaseFolderIn(BaseModel):
    folder_id: str
    folder_name: str


class NewFolderIn(BaseModel):
    name: str
    parent_id: str


def create_app(ledger_path: Path, *, drive_factory=None, oauth: WebOAuth | None = None) -> FastAPI:
    app = FastAPI(title="Research Stream", docs_url=None, redoc_url=None)
    auth = oauth or WebOAuth()

    def open_ledger() -> Ledger:
        # SQLite connections are not shareable across threads, so each request
        # opens its own. At this volume the cost is irrelevant.
        return Ledger(ledger_path)

    def open_drive() -> DriveClient:
        """Build a Drive client, or explain why browsing is unavailable.

        Injected in tests. Authorisation is deliberately lazy: the triage half
        of the app must work with no Google credentials at all, so a missing
        token only breaks folder browsing.
        """
        if drive_factory is not None:
            return drive_factory()

        # Prefer the token the browser sign-in wrote; fall back to the CLI's,
        # so an existing desktop-flow token keeps working.
        creds = auth.credentials()
        if creds is None:
            try:
                creds = load_credentials(scopes=DRIVE_SCOPES, allow_interactive=False)
            except AuthError as exc:
                raise HTTPException(
                    status_code=503, detail=f"Not signed in to Google: {exc}"
                ) from exc
        return DriveClient(creds)

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
                "papers": [paper_view(p) for p in led.pending()],
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
            return paper_view(paper)

    # --- filing ---------------------------------------------------------------

    @app.get("/api/accepted")
    def accepted() -> dict[str, Any]:
        """Accepted papers, split into those awaiting a destination and those with one."""
        with open_ledger() as led:
            return {
                "unfiled": [paper_view(p) for p in led.accepted(filed=False)],
                "filed": [paper_view(p) for p in led.accepted(filed=True)],
                "base": {
                    "folder_id": led.get_setting(SETTING_BASE_FOLDER_ID),
                    "folder_name": led.get_setting(SETTING_BASE_FOLDER_NAME),
                },
            }

    @app.post("/api/destination")
    def set_destination(body: DestinationIn) -> dict[str, Any]:
        """Assign one destination to any number of papers at once."""
        if not body.keys:
            raise HTTPException(status_code=400, detail="no papers given")
        with open_ledger() as led:
            updated = [
                k for k in body.keys
                if led.set_destination(k, body.folder_id, body.folder_name)
            ]
            if not updated:
                raise HTTPException(status_code=404, detail="no such papers")
            return {"updated": updated, "folder_name": body.folder_name}

    # --- settings and browsing --------------------------------------------------

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        with open_ledger() as led:
            return {
                "base_folder_id": led.get_setting(SETTING_BASE_FOLDER_ID),
                "base_folder_name": led.get_setting(SETTING_BASE_FOLDER_NAME),
            }

    @app.put("/api/settings/base-folder")
    def set_base_folder(body: BaseFolderIn) -> dict[str, Any]:
        with open_ledger() as led:
            led.set_setting(SETTING_BASE_FOLDER_ID, body.folder_id)
            led.set_setting(SETTING_BASE_FOLDER_NAME, body.folder_name)
            return {"base_folder_id": body.folder_id, "base_folder_name": body.folder_name}

    @app.get("/api/drive/folders")
    def browse(parent: str | None = None) -> dict[str, Any]:
        """List subfolders of `parent`, defaulting to the configured base.

        The breadcrumb stops at the base folder so the picker never offers a
        route out of it.
        """
        with open_ledger() as led:
            base = led.get_setting(SETTING_BASE_FOLDER_ID)

        target = parent or base or "root"
        drive = open_drive()
        try:
            return {
                "parent": target,
                "breadcrumb": drive.breadcrumb(target, stop_at=base),
                "folders": drive.list_child_folders(target),
                "base_folder_id": base,
            }
        except DriveError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/drive/folders")
    def new_folder(body: NewFolderIn) -> dict[str, str]:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="folder name is empty")
        drive = open_drive()
        try:
            return drive.create_folder(name, parent_id=body.parent_id)
        except DriveError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # --- sign-in ----------------------------------------------------------------

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        base = str(request.base_url)
        redirect = callback_url(base)
        status = auth.status()
        status["redirect_uri"] = redirect
        status["redirect_ok"] = is_valid_redirect(redirect)
        if not status["redirect_ok"]:
            status["redirect_problem"] = redirect_hint(redirect)
        return status

    @app.get("/auth/google/start")
    def auth_start(request: Request):
        redirect = callback_url(str(request.base_url))
        if not is_valid_redirect(redirect):
            raise HTTPException(status_code=400, detail=redirect_hint(redirect))
        try:
            return RedirectResponse(auth.start(redirect), status_code=307)
        except OAuthError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get(CALLBACK_PATH, response_class=HTMLResponse)
    def auth_callback(request: Request, state: str = "", error: str = "") -> str:
        if error:
            return _closing_page(f"Google reported: {error}", ok=False)
        try:
            auth.finish(state=state, full_url=str(request.url))
        except OAuthError as exc:
            return _closing_page(str(exc), ok=False)
        return _closing_page("Signed in. You can close this tab.", ok=True)

    @app.post("/api/auth/signout")
    def auth_signout() -> dict[str, bool]:
        return {"signed_out": auth.sign_out()}

    return app


def _closing_page(message: str, *, ok: bool) -> str:
    """A minimal result page for the OAuth round trip.

    Self-contained like the rest of the app: no CDN, and it tells the opener to
    refresh so the sign-in state updates without the user hunting for a button.
    """
    colour = "#1a7f4b" if ok else "#a33"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research Stream</title></head>
<body style="font:16px/1.6 system-ui,sans-serif;margin:0;display:grid;
place-items:center;height:100vh;padding:1.5rem;text-align:center">
<div><p style="color:{colour};font-weight:600">{message}</p>
<p style="color:#6b6b6b;font-size:.9rem">You can return to the app.</p></div>
<script>
  try {{ if (window.opener) {{ window.opener.postMessage('auth-changed', '*'); }} }} catch (e) {{}}
  setTimeout(() => {{ try {{ window.close(); }} catch (e) {{}} }}, {1200 if ok else 6000});
</script>
</body></html>"""
