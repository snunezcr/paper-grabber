"""Command line entry point -- parse and enrich, for verification."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .cache import LookupCache
from .drive import DriveClient, DriveError
from .imap_source import (
    GMAIL_ALL_MAIL,
    ImapAlertSource,
    ImapConfig,
    ImapError,
    check_login,
)
from .ledger import SETTING_BASE_FOLDER_ID, Decision, Ledger, paper_view
from .models import AlertPaper
from .google_auth import (
    DEFAULT_CREDENTIALS,
    DEFAULT_TOKEN,
    DRIVE_SCOPES,
    AuthError,
    load_credentials,
)
from .enrich import Enrichment, OpenAlexClient, RateLimited, direct_pdf_url
from .fetch import download_first_available, make_client
from .filename import deduplicate_filename, pdf_filename
from .parse import dedupe, parse_alert_email
from .staging import StagingArea, VerificationError


def _load(paths: list[Path], *, do_dedupe: bool = True):
    papers = []
    for path in paths:
        papers.extend(parse_alert_email(path.read_bytes()))
    return dedupe(papers) if do_dedupe else papers


def cmd_parse(args) -> int:
    papers = _load(args.paths, do_dedupe=not args.no_dedupe)
    json.dump([p.to_dict() for p in papers], sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


DEFAULT_CACHE = Path.home() / ".cache" / "paper-grabber" / "openalex.db"


def _enrich_all(papers, args):
    """Enrich every paper, stopping cleanly if the OpenAlex budget runs out.

    Returns (pairs, exhausted). Papers looked up before the limit keep their
    results; the rest come back bare rather than being lost.
    """
    cache = None if args.no_cache else LookupCache(args.cache)
    pairs, exhausted = [], False
    try:
        with OpenAlexClient(mailto=args.mailto, cache=cache) as oa:
            for i, paper in enumerate(papers):
                if i:
                    time.sleep(args.delay)
                try:
                    pairs.append((paper, oa.enrich(paper)))
                except RateLimited as exc:
                    exhausted = True
                    wait = f" (retry in {exc.retry_after}s)" if exc.retry_after else ""
                    print(
                        f"\nOpenAlex budget exhausted after {len(pairs)} lookups{wait}:"
                        f" {exc}\nContinuing with what was already resolved.",
                        file=sys.stderr,
                    )
                    pairs.extend((p, Enrichment(note="not looked up: budget exhausted"))
                                 for p in papers[i:])
                    break
    finally:
        if cache is not None:
            cache.close()
    return pairs, exhausted


def cmd_enrich(args) -> int:
    papers = _load(args.paths)
    pairs, _ = _enrich_all(papers, args)
    out = [{"paper": p.to_dict(), "enrichment": e.to_dict()} for p, e in pairs]

    if args.summary:
        _print_summary(out)
    else:
        json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0


def _print_summary(rows: list[dict]) -> None:
    matched = sum(1 for r in rows if r["enrichment"]["matched"])
    with_pdf = sum(1 for r in rows if r["enrichment"]["pdf_url"])
    with_abs = sum(
        1 for r in rows if r["enrichment"]["abstract"] or r["paper"]["snippet"]
    )

    for r in rows:
        p, e = r["paper"], r["enrichment"]
        year = e["year"] or p["year"] or "????"
        flag = "PDF" if e["pdf_url"] else "   "
        print(f"{year}  [{flag}]  {p['title'][:66]}")
        if e["note"]:
            print(f"            note: {e['note']}")

    n = len(rows)
    print(f"\n{n} papers | matched {matched} | fetchable {with_pdf} | with text {with_abs}")


def cmd_download(args) -> int:
    """Parse, enrich, and stage every retrievable PDF.

    Files land in the staging directory and stay there. They are removed only
    once Drive confirms receipt with a matching size and MD5 -- see
    StagingArea.confirm -- so an upload that fails, or half-succeeds, never
    costs us the only copy.
    """
    staging = StagingArea(args.dest)
    # An interrupted previous run may have left half-written files.
    for leftover in staging.sweep_partials():
        print(f"swept incomplete download: {leftover.name}")

    papers = _load(args.paths)

    enriched, _ = _enrich_all(papers, args)

    taken = {p.name for p in staging.pending()}
    saved = failed = skipped = 0

    with make_client() as http:
        for paper, e in enriched:
            title = e.title or paper.title
            year = e.year or paper.year

            # Scholar's own link is independent of OpenAlex, so it still
            # works when enrichment was rate-limited, unmatched, or offline.
            candidates = e.pdf_candidates or [
                u for u in (direct_pdf_url(paper),) if u
            ]

            if not candidates:
                skipped += 1
                where = e.landing_url or paper.url
                print(f"SKIP  {title[:58]}")
                print(f"        no PDF location{f' -- open: {where}' if where else ''}")
                continue

            result = download_first_available(candidates, client=http)
            if not result.ok:
                failed += 1
                print(f"FAIL  {title[:58]}")
                print(f"        {result.reason}")
                continue

            name = deduplicate_filename(pdf_filename(title, year), taken)
            taken.add(name)
            # Staged, not filed: the local copy is the only one until Drive
            # confirms receipt, at which point staging.confirm() removes it.
            staging.stage(name, result.content)
            saved += 1
            print(f"STAGED {result.size / 1024:7.0f} KB  {name}")

    print(f"\nstaged {saved} | failed {failed} | no PDF location {skipped}")
    if saved:
        print(f"awaiting Drive upload in {staging.root}")
    return 0


DEFAULT_LEDGER = Path.home() / ".local" / "share" / "paper-grabber" / "state.db"
DEFAULT_STAGING = Path.home() / ".local" / "share" / "paper-grabber" / "staging"


def cmd_sync(args) -> int:
    """Pull new Scholar alerts from the mailbox into the ledger.

    Messages already processed are skipped, and papers already decided are not
    resurrected, so running this twice in a row is a no-op.
    """
    try:
        config = ImapConfig.from_env(user=args.user, mailbox=args.mailbox)
    except ImapError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    source = ImapAlertSource(config)
    with Ledger(args.ledger) as ledger:
        seen = ledger.seen_message_ids()
        new_papers = repeats = messages = 0

        try:
            for msg in source.fetch_alerts(
                since_days=args.days, skip=seen, limit=args.limit
            ):
                messages += 1
                for paper in dedupe(parse_alert_email(msg.raw)):
                    if ledger.record(paper):
                        new_papers += 1
                    else:
                        repeats += 1
                # Marked only after its papers are recorded, so an interruption
                # re-reads the message rather than losing it.
                ledger.mark_message(msg.message_id)
        except ImapError as exc:
            print(f"mail error: {exc}", file=sys.stderr)
            return 1

        counts = ledger.counts()

    print(f"{messages} new messages | {new_papers} new papers | {repeats} already known")
    print(f"pending {counts.get('pending', 0)} | "
          f"accepted {counts.get('accepted', 0)} | rejected {counts.get('rejected', 0)}")
    return 0


def cmd_check_mail(args) -> int:
    """Confirm the app password works before wiring up a scheduled run."""
    try:
        config = ImapConfig.from_env(user=args.user, mailbox=args.mailbox)
        print(check_login(config))
    except ImapError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    return 0


def cmd_enrich_pending(args) -> int:
    """Look up pending papers on OpenAlex and store the results in the ledger.

    Separate from sync because OpenAlex is metered: syncing mail is free and
    should always work, while enrichment can be budget-limited or deferred.
    """
    with Ledger(args.ledger) as ledger:
        todo = ledger.needing_enrichment()
        if args.limit:
            todo = todo[: args.limit]
        if not todo:
            print("nothing to enrich")
            return 0

        papers = [AlertPaper(**{k: v for k, v in p.payload.items()
                                if k in AlertPaper.__dataclass_fields__}) for p in todo]
        pairs, exhausted = _enrich_all(papers, args)

        done = 0
        for entry, (_, enrichment) in zip(todo, pairs):
            if enrichment.note == "not looked up: budget exhausted":
                continue
            ledger.attach_enrichment(entry.key, enrichment.to_dict())
            done += 1

        matched = sum(1 for _, e in pairs if e.matched)
        with_pdf = sum(1 for _, e in pairs if e.pdf_url)
        print(f"enriched {done}/{len(todo)} | matched {matched} | fetchable {with_pdf}")
        if exhausted:
            print("stopped early: OpenAlex budget exhausted", file=sys.stderr)
    return 0


def cmd_serve(args) -> int:
    """Run the triage web app."""
    import uvicorn

    from .server import create_app

    # 0.0.0.0 by default: the whole point is reaching this from the tablet.
    # On an untrusted network, bind 127.0.0.1 and reach it over Tailscale.
    print(f"triage UI on http://{args.host}:{args.port}  (ledger: {args.ledger})")
    uvicorn.run(create_app(args.ledger), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_pending(args) -> int:
    """List papers awaiting triage."""
    with Ledger(args.ledger) as ledger:
        rows = ledger.pending()
        for p in rows:
            v = paper_view(p)
            year = v["year"] or "????"
            authors = ", ".join(v["authors"]) or "(no authors)"
            flag = "PDF" if v["has_pdf"] else "   "
            print(f"{year} [{flag}] {v['title']}")
            print(f"           {authors}")
            if v["abstract"]:
                kind = "snippet" if v["abstract_is_snippet"] else "abstract"
                print(f"           ({kind}) {v['abstract'][:110]}")
        print(f"\n{len(rows)} pending")
    return 0


def cmd_decide(args) -> int:
    """Accept or reject a paper by title."""
    with Ledger(args.ledger) as ledger:
        if not ledger.known(args.title):
            print(f"no such paper: {args.title!r}", file=sys.stderr)
            return 1
        ledger.decide(args.title, Decision(args.decision))
        print(f"{args.decision}: {args.title}")
    return 0


def cmd_fetch(args) -> int:
    """Download PDFs for accepted papers into staging.

    Driven by the ledger rather than by .eml files, so it fetches exactly what
    triage said was interesting. Papers with no open-access PDF are left alone
    and reported, not retried endlessly.
    """
    staging = StagingArea(args.staging)
    for leftover in staging.sweep_partials():
        print(f"swept incomplete download: {leftover.name}")

    with Ledger(args.ledger) as ledger:
        todo = ledger.awaiting_download()
        if args.limit:
            todo = todo[: args.limit]
        if not todo:
            print("nothing to fetch")
            return 0

        taken = {p.name for p in staging.pending()}
        got = skipped = failed = 0

        with make_client() as http:
            for entry in todo:
                view = paper_view(entry)
                candidates = _candidates_for(entry)
                if not candidates:
                    skipped += 1
                    print(f"SKIP  no PDF location: {view['title'][:56]}")
                    continue

                result = download_first_available(candidates, client=http)
                if not result.ok:
                    failed += 1
                    print(f"FAIL  {result.reason}: {view['title'][:56]}")
                    continue

                name = deduplicate_filename(
                    pdf_filename(view["title"], view["year"]), taken
                )
                taken.add(name)
                staging.stage(name, result.content)
                ledger.set_staged(entry.key, name)
                got += 1
                print(f"STAGED {result.size / 1024:7.0f} KB  {name}")

    print(f"\nstaged {got} | no PDF {skipped} | failed {failed}")
    return 0


def _candidates_for(entry) -> list[str]:
    """PDF URLs for a ledger row: enrichment first, then Scholar's own link."""
    enrichment = entry.payload.get("enrichment") or {}
    candidates = list(enrichment.get("pdf_candidates") or [])
    if not candidates:
        url = enrichment.get("pdf_url")
        if url:
            candidates.append(url)
    if not candidates:
        paper = AlertPaper(**{k: v for k, v in entry.payload.items()
                              if k in AlertPaper.__dataclass_fields__})
        direct = direct_pdf_url(paper)
        if direct:
            candidates.append(direct)
    return candidates


def cmd_upload(args) -> int:
    """Upload staged PDFs to their chosen destinations.

    Each paper is deleted locally only once Drive confirms a matching size and
    MD5, and is then marked uploaded so a re-run does not send it twice.
    """
    staging = StagingArea(args.staging)

    with Ledger(args.ledger) as ledger:
        todo = ledger.awaiting_upload()
        unrouted = len([p for p in ledger.accepted(filed=False) if p.staged_name])
        if not todo:
            if unrouted:
                print(f"nothing to upload; {unrouted} staged paper(s) have no "
                      "destination yet -- choose one in the Filing tab")
            else:
                print("nothing to upload")
            return 0

        try:
            creds = load_credentials(
                credentials_path=args.credentials,
                token_path=args.token,
                scopes=DRIVE_SCOPES,
                allow_interactive=not args.non_interactive,
            )
        except AuthError as exc:
            print(f"authorisation failed: {exc}", file=sys.stderr)
            return 2

        drive = DriveClient(creds)
        uploaded = kept = 0

        for entry in todo:
            path = staging.path_for(entry.staged_name)
            if not path.exists():
                # The file vanished; forget the staging claim so a later fetch
                # can download it again rather than silently skipping forever.
                ledger.set_staged(entry.key, None)
                print(f"MISSING  re-queued for download: {entry.staged_name}")
                continue

            try:
                if not args.allow_duplicates and drive.exists_in_folder(
                    path.name, entry.dest_folder_id
                ):
                    print(f"SKIP  already in Drive: {path.name}")
                    ledger.set_staged(entry.key, None)
                    path.unlink(missing_ok=True)
                    continue

                remote = drive.upload(path, folder_id=entry.dest_folder_id)
                staging.confirm(path, remote)
                ledger.set_uploaded(entry.key, remote.file_id)
                uploaded += 1
                print(f"UPLOADED  {path.name}  ->  {entry.dest_folder_name}")
            except VerificationError as exc:
                # Drive's copy could not be proven identical, so the local file
                # stays. This is the safe outcome, not a lost paper.
                kept += 1
                print(f"UNVERIFIED (kept locally)  {exc}", file=sys.stderr)
            except DriveError as exc:
                kept += 1
                print(f"FAILED (kept locally)  {exc}", file=sys.stderr)

    print(f"\nuploaded {uploaded} | kept locally {kept} | still staged {len(staging.pending())}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paper-grabber")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="parse .eml Scholar alerts to JSON")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--no-dedupe", action="store_true")
    p.set_defaults(func=cmd_parse)

    e = sub.add_parser("enrich", help="parse, then look up each paper on OpenAlex")
    e.add_argument("paths", nargs="+", type=Path)
    e.add_argument("--mailto", help="contact address for OpenAlex's polite pool")
    e.add_argument("--delay", type=float, default=0.15, help="seconds between requests")
    e.add_argument("--summary", action="store_true", help="human-readable table")
    e.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    e.add_argument("--no-cache", action="store_true")
    e.set_defaults(func=cmd_enrich)

    d = sub.add_parser("download", help="parse, enrich, and save PDFs to a directory")
    d.add_argument("paths", nargs="+", type=Path)
    d.add_argument("--dest", type=Path, required=True, help="staging directory")
    d.add_argument("--mailto", help="contact address for OpenAlex's polite pool")
    d.add_argument("--delay", type=float, default=0.15)
    d.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    d.add_argument("--no-cache", action="store_true")
    d.set_defaults(func=cmd_download)

    ft = sub.add_parser("fetch", help="download PDFs for accepted papers")
    ft.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ft.add_argument("--staging", type=Path, default=DEFAULT_STAGING)
    ft.add_argument("--limit", type=int)
    ft.set_defaults(func=cmd_fetch)

    u = sub.add_parser("upload", help="upload staged PDFs to Drive, then remove them")
    u.add_argument("--staging", type=Path, default=DEFAULT_STAGING, help="staging directory")
    u.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    u.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    u.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    u.add_argument("--non-interactive", action="store_true",
                   help="never open a browser; fail if no token exists")
    u.add_argument("--allow-duplicates", action="store_true",
                   help="upload even if a file of that name is already there")
    u.set_defaults(func=cmd_upload)

    sy = sub.add_parser("sync", help="pull new Scholar alerts from Gmail")
    sy.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    sy.add_argument("--days", type=int, default=2, help="how far back to search")
    sy.add_argument("--limit", type=int, help="cap messages fetched")
    sy.add_argument("--user", help="mailbox address (default: $PAPER_GRABBER_IMAP_USER)")
    sy.add_argument("--mailbox", default=GMAIL_ALL_MAIL, help="IMAP folder to search")
    sy.set_defaults(func=cmd_sync)

    ck = sub.add_parser("check-mail", help="verify IMAP credentials work")
    ck.add_argument("--user")
    ck.add_argument("--mailbox", default=GMAIL_ALL_MAIL)
    ck.set_defaults(func=cmd_check_mail)

    ep = sub.add_parser("enrich-pending", help="look up pending papers on OpenAlex")
    ep.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ep.add_argument("--limit", type=int, help="cap how many to look up")
    ep.add_argument("--mailto", help="contact address for OpenAlex's polite pool")
    ep.add_argument("--delay", type=float, default=0.15)
    ep.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ep.add_argument("--no-cache", action="store_true")
    ep.set_defaults(func=cmd_enrich_pending)

    sv = sub.add_parser("serve", help="run the triage web app")
    sv.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8823)
    sv.set_defaults(func=cmd_serve)

    pd = sub.add_parser("pending", help="list papers awaiting triage")
    pd.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    pd.set_defaults(func=cmd_pending)

    dc = sub.add_parser("decide", help="accept or reject a paper")
    dc.add_argument("title")
    dc.add_argument("decision", choices=[d.value for d in Decision])
    dc.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    dc.set_defaults(func=cmd_decide)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
