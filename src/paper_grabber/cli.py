"""Command line entry point -- currently just the parser, for verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .parse import dedupe, parse_alert_email


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paper-grabber")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="parse .eml Scholar alerts to JSON")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--no-dedupe", action="store_true")

    args = ap.parse_args(argv)

    papers = []
    for path in args.paths:
        papers.extend(parse_alert_email(path.read_bytes()))
    if not args.no_dedupe:
        papers = dedupe(papers)

    json.dump([p.to_dict() for p in papers], sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
