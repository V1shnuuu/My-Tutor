"""Admin CLI: python -m app.cli codes 400 [--labels roster.csv]"""
from __future__ import annotations

import argparse
import csv
import sys

import httpx

from . import auth
from .db import connect


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("codes", help="generate enrollment codes")
    c.add_argument("n", type=int, nargs="?", default=0)
    c.add_argument("--labels", help="one-column CSV of student labels")
    v = sub.add_parser("voices", help="download Piper voices (ar/en/fr) into DATA_DIR/voices")
    v.add_argument("langs", nargs="*", default=["ar", "en", "fr"])
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


    elif args.cmd == "voices":
        from . import tts

        base = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
        for lang in args.langs:
            name = tts.VOICES[lang]
            loc, voice, quality = name.split("-")[0], name.split("-")[1], name.split("-")[2]
            for ext in (".onnx", ".onnx.json"):
                target = tts.voice_dir() / f"{name}{ext}"
                if target.exists():
                    print("have", target.name); continue
                url = f"{base}/{loc[:2]}/{loc}/{voice}/{quality}/{name}{ext}"
                print("downloading", url)
                with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r, open(target, "wb") as f:
                    r.raise_for_status()
                    for chunk in r.iter_bytes():
                        f.write(chunk)
        print(tts.availability())


if __name__ == "__main__":
    main()
