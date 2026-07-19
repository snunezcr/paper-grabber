"""Command line entry point -- parse and enrich, for verification."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .cache import LookupCache
from .enrich import Enrichment, OpenAlexClient, RateLimited, direct_pdf_url
from .fetch import download_first_available, make_client
from .filename import deduplicate_filename, pdf_filename
from .parse import dedupe, parse_alert_email
from .staging import StagingArea


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

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
