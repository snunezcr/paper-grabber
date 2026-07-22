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
from html import escape as html_escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi import Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel

from .drive import DriveClient, DriveError
from .google_auth import DRIVE_SCOPES, AuthError, load_credentials
from .refresh import RefreshRunner, make_refresh_job
from .filename import deduplicate_filename, pdf_filename
from .staging import StagingArea
from .uploader import UploadRunner, make_upload_job
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


class UploadIn(BaseModel):
    keys: list[str]


class NoteIn(BaseModel):
    note: str


class BulkDecisionIn(BaseModel):
    keys: list[str]
    decision: Decision


def create_app(
    ledger_path: Path,
    *,
    drive_factory=None,
    oauth: WebOAuth | None = None,
    refresh_runner: RefreshRunner | None = None,
    cache_path: Path | None = None,
    mailto: str | None = None,
    refresh_days: int = 7,
    staging_path: Path | None = None,
    upload_runner: UploadRunner | None = None,
    pdf_fetcher=None,
) -> FastAPI:
    app = FastAPI(title="Research Stream", docs_url=None, redoc_url=None)
    auth = oauth or WebOAuth()

    def default_source():
        """Mail source for a manual check: the signed-in account, else IMAP.

        The Gmail path is used only when the token really carries the Gmail
        scope. A Drive-only token would otherwise be handed to the Gmail API
        and fail with "insufficient authentication scopes" at call time.
        """
        from .gmail import GmailClient
        from .imap_source import ImapAlertSource, ImapConfig
        from .oauth_web import GMAIL_READONLY

        creds = auth.credentials()
        if creds is not None and auth.has_scope(GMAIL_READONLY):
            client = GmailClient(creds)

            class _Adapter:
                @staticmethod
                def fetch_alerts(*, since_days=2, skip=None, limit=None):
                    return client.fetch_alerts(
                        newer_than_days=since_days, skip=skip, limit=limit
                    )

            return _Adapter()
        return ImapAlertSource(ImapConfig.from_env())

    _upload: UploadRunner | None = upload_runner

    runner = refresh_runner or RefreshRunner(
        make_refresh_job(
            ledger_path=ledger_path,
            cache_path=cache_path,
            mailto=mailto,
            days=refresh_days,
            source_factory=default_source,
        )
    )

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

    @app.put("/api/papers/{key}/note")
    def set_note(key: str, body: NoteIn) -> dict[str, Any]:
        """Save a note.

        Editable at any point, including after upload. Reading a paper happens
        *after* it is filed, which is exactly when there is something worth
        writing down -- so an already-uploaded paper syncs the note onto the
        Drive file's description instead of refusing the edit.
        """
        with open_ledger() as led:
            paper = led.get(key)
            if paper is None:
                raise HTTPException(status_code=404, detail="no such paper")
            led.set_note(key, body.note)
            saved = led.get(key)

        synced = False
        if saved.drive_file_id:
            try:
                open_drive().set_description(saved.drive_file_id, saved.note)
                synced = True
            except Exception:
                # The note is safe in the ledger; Drive can be retried. Failing
                # the request would suggest nothing was saved, which is wrong,
                # so every failure mode here is reported rather than raised.
                synced = False

        return {"key": key, "note": saved.note, "synced_to_drive": synced}

    @app.post("/api/decisions")
    def bulk_decision(body: BulkDecisionIn) -> dict[str, Any]:
        """Apply one decision to several papers at once."""
        if not body.keys:
            raise HTTPException(status_code=400, detail="no papers given")
        with open_ledger() as led:
            updated = [k for k in body.keys if led.decide_by_key(k, body.decision)]
            if not updated:
                raise HTTPException(status_code=404, detail="no such papers")
            return {
                "updated": updated,
                "decision": body.decision.value,
                "counts": led.counts(),
            }

    @app.get("/api/papers/{key}/pdf")
    def paper_pdf(key: str):
        """Stream a paper's PDF for the in-app reader.

        Served from staging when the file is still local, and pulled from
        Drive once it has been uploaded and the local copy removed. Proxied
        rather than linked directly because the browser holds no Google
        credentials -- the server is the one that is signed in.
        """
        from .uploader import pdf_candidates_for

        with open_ledger() as led:
            paper = led.get(key)
            if paper is None:
                raise HTTPException(status_code=404, detail="no such paper")
            drive_file_id = paper.drive_file_id
            staged_name = paper.staged_name
            title = paper.title
            candidates = pdf_candidates_for(paper)

        filename = f"{title[:80]}.pdf".replace('"', "")
        headers = {"Content-Disposition": f'inline; filename="{filename}"'}

        if staged_name:
            path = StagingArea(staging_path or (ledger_path.parent / "staging")).path_for(
                staged_name
            )
            if path.exists():
                return FileResponse(path, media_type="application/pdf", headers=headers)

        if not drive_file_id:
            # Nothing held locally or in Drive, so fetch it from its
            # open-access location for reading. Deliberately not staged: this
            # is a read, and staging would put the paper in the upload queue
            # without anyone asking for that.
            if not candidates:
                raise HTTPException(
                    status_code=404,
                    detail="no open-access PDF is known for this paper",
                )

            if pdf_fetcher is not None:
                fetched = pdf_fetcher(candidates)
            else:
                from .fetch import download_first_available, make_client

                with make_client() as http:
                    fetched = download_first_available(candidates, client=http)
            if not fetched.ok:
                raise HTTPException(
                    status_code=502,
                    detail=f"could not fetch the PDF: {fetched.reason}",
                )
            return Response(
                content=fetched.content, media_type="application/pdf", headers=headers
            )

        try:
            buffer = open_drive().download(drive_file_id)
        except DriveError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        def stream():
            try:
                while chunk := buffer.read(256 * 1024):
                    yield chunk
            finally:
                buffer.close()

        return StreamingResponse(
            stream(), media_type="application/pdf", headers=headers
        )

    @app.get("/api/papers/{key}/bibtex")
    def bibtex(key: str) -> dict[str, Any]:
        """A BibTeX entry for one paper."""
        from .bibtex import to_bibtex

        with open_ledger() as led:
            paper = led.get(key)
            if paper is None:
                raise HTTPException(status_code=404, detail="no such paper")
            return {"key": key, "bibtex": to_bibtex(paper_view(paper))}

    @app.get("/api/rejected")
    def rejected() -> dict[str, Any]:
        """Papers that were rejected, so they can be recovered."""
        with open_ledger() as led:
            return {
                "papers": [paper_view(p) for p in led.rejected()],
                "counts": led.counts(),
            }

    @app.get("/api/processed")
    def processed() -> dict[str, Any]:
        """Papers already in Drive."""
        with open_ledger() as led:
            return {
                "papers": [paper_view(p) for p in led.processed()],
                "counts": led.counts(),
            }

    @app.post("/api/papers/{key}/local-pdf")
    async def upload_local_pdf(key: str, file: UploadFile = File(...)) -> dict[str, Any]:
        """Attach a PDF the user picked from their device.

        For papers with no open-access copy the pipeline cannot fetch: the
        user supplies the file, it is staged, and the normal upload-to-Drive
        then sends that instead of trying a download. Validated by its leading
        bytes -- a browser's file type is only a hint, and a mislabelled
        upload would otherwise reach Drive as a broken paper.
        """
        from .fetch import DEFAULT_MAX_BYTES, looks_like_pdf

        with open_ledger() as led:
            paper = led.get(key)
            if paper is None:
                raise HTTPException(status_code=404, detail="no such paper")
            if paper.drive_file_id:
                raise HTTPException(
                    status_code=409, detail="already in Drive; nothing to attach"
                )
            existing_name = paper.staged_name
            view = paper_view(paper)

        # Bounded read: one byte over the cap is enough to know it is too big,
        # without pulling a huge file into the VM's memory.
        data = await file.read(DEFAULT_MAX_BYTES + 1)
        if len(data) > DEFAULT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds the {DEFAULT_MAX_BYTES // (1024*1024)} MB limit",
            )
        if not looks_like_pdf(data):
            raise HTTPException(status_code=400, detail="that file is not a PDF")

        staging = StagingArea(staging_path or (ledger_path.parent / "staging"))
        # Reuse the existing name when replacing, so a second attach overwrites
        # rather than leaving an orphan staged file behind.
        name = existing_name or deduplicate_filename(
            pdf_filename(view["title"], view["year"]),
            {p.name for p in staging.pending()},
        )
        staging.stage(name, data)

        with open_ledger() as led:
            led.set_staged(key, name)

        return {"key": key, "staged": True, "name": name}

    @app.post("/api/papers/{key}/verify")
    def verify(key: str) -> dict[str, Any]:
        """Check a processed paper is still in Drive; requeue it if not."""
        with open_ledger() as led:
            paper = led.get(key)
            if paper is None:
                raise HTTPException(status_code=404, detail="no such paper")
            if not paper.drive_file_id:
                raise HTTPException(status_code=409, detail="not uploaded yet")

            drive = open_drive()
            try:
                status = drive.file_status(paper.drive_file_id)
            except DriveError as exc:
                # Could not reach Drive. Say so and change nothing: treating a
                # network failure as deletion would undo a good upload.
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            if status["present"]:
                return {
                    "key": key,
                    "present": True,
                    "detail": f"still in Drive as {status['name']}"
                    if status.get("name")
                    else "still in Drive",
                    "counts": led.counts(),
                }

            led.clear_uploaded(key)
            return {
                "key": key,
                "present": False,
                "trashed": status["trashed"],
                "detail": (
                    "in the Drive bin; returned to Filing"
                    if status["trashed"]
                    else "no longer in Drive; returned to Filing"
                ),
                "counts": led.counts(),
            }

    @app.post("/api/papers/{key}/unfile")
    def unfile(key: str) -> dict[str, Any]:
        """Return a filed paper to the queue by clearing its destination."""
        with open_ledger() as led:
            paper = led.get(key)
            if paper is None:
                raise HTTPException(status_code=404, detail="no such paper")
            if paper.drive_file_id:
                raise HTTPException(
                    status_code=409,
                    detail="already uploaded to Drive; unfiling would not remove it",
                )
            led.set_destination(key, None, None)
            return {"key": key, "counts": led.counts()}

    @app.post("/api/upload")
    def start_upload(body: UploadIn) -> JSONResponse:
        """Fetch (if needed) and upload the given papers."""
        if not body.keys:
            raise HTTPException(status_code=400, detail="no papers given")

        runner = upload_runner or UploadRunner(
            make_upload_job(
                ledger_path=ledger_path,
                staging_path=staging_path or (ledger_path.parent / "staging"),
                keys=list(body.keys),
                drive_factory=open_drive,
            )
        )
        # Rebuilt per request when not injected, because the key list differs
        # each time; the guard against concurrent runs lives in the shared
        # runner below.
        nonlocal _upload
        if upload_runner is None:
            if _upload is not None and _upload.state().running:
                return JSONResponse(
                    {"started": False, **_upload.state().to_dict()}, status_code=202
                )
            _upload = runner
        started, state = runner.start()
        return JSONResponse({"started": started, **state.to_dict()}, status_code=202)

    @app.get("/api/upload")
    def upload_status() -> dict[str, Any]:
        active = upload_runner or _upload
        if active is None:
            return {"running": False, "started_at": None, "last": None}
        return active.state().to_dict()

    @app.get("/api/suggestions")
    def suggestions() -> dict[str, Any]:
        """Suggested destinations for papers awaiting one.

        Candidates are the subfolders of the base folder, plus every folder
        already used -- a folder that has been filed into is a candidate even
        if it now lives elsewhere.
        """
        from .suggest import build_idf, suggest_folder

        with open_ledger() as led:
            base = led.get_setting(SETTING_BASE_FOLDER_ID)
            unfiled = led.accepted(filed=False)
            # The corpus is the whole queue, not just the unfiled papers: a
            # word's rarity is a property of the collection.
            corpus = [
                f"{v['title']} {v['abstract'] or ''}"
                for v in (paper_view(p) for p in led.pending() + led.accepted())
            ]
            used: dict[str, str] = {}
            history: dict[str, list[str]] = {}
            for entry in led.processed() + led.accepted(filed=True):
                if entry.dest_folder_id:
                    used[entry.dest_folder_id] = entry.dest_folder_name or ""
                    history.setdefault(entry.dest_folder_id, []).append(entry.title)

        if not unfiled:
            return {"suggestions": {}, "folders": []}

        folders = [{"id": fid, "name": name} for fid, name in used.items()]
        if base:
            try:
                for child in open_drive().list_child_folders(base):
                    if child["id"] not in used:
                        folders.append(child)
            except (DriveError, HTTPException):
                # Suggestions are a convenience; an unreachable Drive should
                # not break the Filing tab.
                pass

        idf = build_idf(corpus)
        out: dict[str, Any] = {}
        for entry in unfiled:
            view = paper_view(entry)
            found = suggest_folder(
                title=view["title"],
                abstract=view["abstract"],
                venue=view["venue"],
                folders=folders,
                history=history,
                idf=idf,
            )
            if found:
                out[entry.key] = {
                    "folder_id": found.folder_id,
                    "folder_name": found.folder_name,
                    "score": found.score,
                    "reason": found.reason,
                }
        return {"suggestions": out, "folders": folders}

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

    @app.post("/api/refresh")
    def start_refresh() -> JSONResponse:
        """Check mail now, without waiting for the daily run."""
        started, state = runner.start()
        # 202 whether or not we started one: a second tap while a check is in
        # flight is not an error, it just joins the one already running.
        return JSONResponse(
            {"started": started, **state.to_dict()}, status_code=202
        )

    @app.get("/api/refresh")
    def refresh_status() -> dict[str, Any]:
        return runner.state().to_dict()

    @app.get("/api/version")
    def version() -> dict[str, Any]:
        """What code is actually serving this request.

        Exists because a stale uvicorn process is indistinguishable from a bad
        fix: both look like "the change did not work".
        """
        from . import __version__
        from .oauth_web import _allow_loopback_http  # noqa: F401  (presence check)

        return {
            "version": __version__,
            "loopback_http_supported": True,
            # The page checks these on load: a browser holding a newer page
            # against an older server produces 404s that look like broken
            # buttons, which has been mistaken for a code fault more than once.
            "capabilities": sorted(
                {
                    "refresh",
                    "upload",
                    "unfile",
                    "drive-browse",
                    "processed",
                    "verify",
                    "scope-check",
                    "bibtex",
                    "suggestions",
                    "reader",
                    "local-pdf",
                    "bulk-decision",
                    "rejected",
                    "notes",
                }
            ),
        }

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

    @app.get(CALLBACK_PATH)
    def auth_callback(request: Request, state: str = "", error: str = ""):
        """Land the user back in the app rather than on an interstitial.

        Sign-in is a same-tab navigation, so there is no opener to postMessage
        and nothing to close -- a "you may now close this tab" page simply
        replaces the application. On success we redirect straight back; only a
        failure gets a page, because a failure has something worth reading.
        """
        if error:
            return HTMLResponse(_failure_page(f"Google reported: {error}"))
        try:
            auth.finish(state=state, full_url=str(request.url))
        except OAuthError as exc:
            return HTMLResponse(_failure_page(str(exc)))
        # 303: the callback was a GET, and this must not be re-submitted if the
        # user reloads -- the authorization code is single-use.
        return RedirectResponse("/?signed_in=1", status_code=303)

    @app.post("/api/auth/signout")
    def auth_signout() -> dict[str, bool]:
        return {"signed_out": auth.sign_out()}

    # Vendored PDF.js. Mounted rather than inlined -- 2.7 MB has no business in
    # the page -- but still same-origin, so the no-external-hosts rule holds.
    app.mount("/vendor", StaticFiles(directory=STATIC / "vendor"), name="vendor")

    return app


def _failure_page(message: str) -> str:
    """Shown only when sign-in failed, with a way back.

    Self-contained like the rest of the app, and light/dark aware so it does
    not flash white on a tablet at night.
    """
    safe = html_escape(message)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research Stream</title>
<style>
  :root {{ color-scheme: light dark; --bg:#fbfbfa; --ink:#1a1a1a;
           --muted:#6b6b6b; --bad:#a33; --accent:#2b5fd9; --line:#e4e4e1; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#17181a; --ink:#eceff1; --muted:#9aa0a6;
             --bad:#e2726e; --accent:#7aa2f7; --line:#303338; }}
  }}
  body {{ margin:0; background:var(--bg); color:var(--ink); padding:1.5rem;
          font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
          display:grid; place-items:center; min-height:100vh; }}
  .box {{ max-width:32rem; text-align:center; }}
  h1 {{ font-size:1.05rem; margin:0 0 .75rem; color:var(--bad); }}
  p.detail {{ color:var(--muted); font-size:.92rem;
              word-break:break-word; margin:0 0 1.5rem; }}
  a.back {{ display:inline-flex; align-items:center; min-height:48px;
            padding:0 1.2rem; border:1px solid var(--accent); border-radius:10px;
            color:var(--accent); text-decoration:none; font-weight:600; }}
</style></head>
<body><div class="box">
  <h1>Sign-in did not complete</h1>
  <p class="detail">{safe}</p>
  <a class="back" href="/">Back to Research Stream</a>
</div></body></html>"""
