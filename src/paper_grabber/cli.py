"""Command line entry point -- parse and enrich, for verification."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .enrich import OpenAlexClient
from .parse import dedupe, parse_alert_email


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


def cmd_enrich(args) -> int:
    papers = _load(args.paths)
    out = []
    with OpenAlexClient(mailto=args.mailto) as client:
        for i, paper in enumerate(papers):
            if i:
                # OpenAlex's polite pool allows far more than this; the pause
                # is courtesy, not a limit we are near.
                time.sleep(args.delay)
            enrichment = client.enrich(paper)
            out.append({"paper": paper.to_dict(), "enrichment": enrichment.to_dict()})

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
    e.set_defaults(func=cmd_enrich)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
