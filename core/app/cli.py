"""Admin CLI: python -m app.cli codes 400 [--labels roster.csv]"""
from __future__ import annotations

import argparse
import csv
import sys

from . import auth
from .db import connect


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("codes", help="generate enrollment codes")
    c.add_argument("n", type=int, nargs="?", default=0)
    c.add_argument("--labels", help="one-column CSV of student labels")
    args = ap.parse_args()
    connect()
    if args.cmd == "codes":
        labels = None
        if args.labels:
            with open(args.labels, encoding="utf-8-sig") as f:
                labels = [r[0].strip() for r in csv.reader(f) if r and r[0].strip()]
        n = args.n or (len(labels) if labels else 0)
        if n <= 0:
            sys.exit("give n or --labels")
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(["label", "code"])
        for r in auth.create_students(n, labels):
            w.writerow([r["label"], r["code"]])


if __name__ == "__main__":
    main()
