"""Admin CLI: python -m app.cli codes 400 [--labels roster.csv]

  codes   generate enrollment codes
  voices  download the Piper voices
  doctor  check every lane the tutor depends on and say what is missing
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time

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
    d = sub.add_parser("doctor", help="check brain, ears, voice, corpus — and say what to do about each")
    d.add_argument("--ask", action="store_true", help="also spend one real token on each live LLM lane")
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


    elif args.cmd == "doctor":
        sys.exit(doctor(ask=args.ask))


# ---------------------------------------------------------------- doctor
OK, WARN, BAD = "ok", "--", "XX"


def _row(mark: str, name: str, detail: str, fix: str = "") -> None:
    print(f"  [{mark}] {name:<22} {detail}")
    if fix:
        print(f"       {'':<22} -> {fix}")


def _check_llms(ask: bool) -> bool:
    """Every configured lane, in the order the router would reach for them."""
    from .config import load_providers, settings
    from .router import Router

    providers = load_providers()
    if not providers:
        _row(BAD, "LLM providers", "none configured",
             "set LOCAL_LLM_ENABLED=true (with Ollama running) or add one provider key to core/.env")
        return False

    r = Router()
    healthy = False
    for p in providers:
        pid, model = p["id"], p.get("model")
        base = p.get("base_url", "")
        label = f"{model} @ {base}"
        if not ask:
            _row(WARN, pid, f"{label} (configured; --ask to call it)")
            healthy = True
            continue
        prov = next((x for x in r.providers if x.id == pid), None)
        if prov is None:
            _row(BAD, pid, "configured but the router did not load it")
            continue
        t0 = time.time()
        try:
            prov.inflight += 1  # stream() decrements in its finally
            got = asyncio.run(_one_token(r, prov))
            ms = int((time.time() - t0) * 1000)
            if got.strip():
                _row(OK, pid, f"{label} — replied in {ms}ms: {got.strip()[:40]!r}")
                healthy = True
            else:
                _row(BAD, pid, f"{label} — connected but streamed nothing",
                     "for Qwen3 this is the <think> pass eating max_tokens; keep reasoning_effort: none")
        except Exception as e:
            _row(BAD, pid, f"{label} — {type(e).__name__}: {str(e)[:90]}",
                 "local lane: is the model server running? keyed lane: is the key valid and in quota?")
    return healthy


async def _one_token(r, prov) -> str:
    out = []
    async for delta in r.stream(prov, [{"role": "user", "content": "Reply with the single word: ready"}], 20):
        out.append(delta)
    return "".join(out)


def doctor(ask: bool = False) -> int:
    from . import stt, tts
    from .config import settings
    from .corpus import corpus

    print("\nBRAIN")
    brain_ok = _check_llms(ask)

    print("\nRETRIEVAL")
    corpus.load()
    if corpus.size:
        _row(OK, "corpus", f"{len(corpus.videos)} video(s), {corpus.size} chunks")
    else:
        _row(BAD, "corpus", "empty", "python pipeline/ingest.py (after pipeline/add_videos.py)")
    try:
        from .embed import embed_queries

        t0 = time.time()
        embed_queries(["ready?"])
        _row(OK, "encoder", f"{settings.embed_model} loaded in {int((time.time() - t0) * 1000)}ms")
        embed_ok = True
    except Exception as e:
        _row(BAD, "encoder", f"{settings.embed_model} — {type(e).__name__}: {str(e)[:80]}",
             "first run downloads it; needs network to huggingface.co. Without it /chat cannot answer.")
        embed_ok = False

    print("\nEARS (speech in)")
    b = stt.budget()
    if b["engine"] == "groq":
        _row(OK, "stt", f"Groq Whisper, {b['used']}/{b['cap']} used today")
    elif b["engine"] == "local":
        _row(OK, "stt", f"local faster-whisper ({settings.local_stt_model}, {settings.local_stt_device})",
             "first question downloads the model; set GROQ_API_KEY for better Egyptian accuracy")
    else:
        _row(WARN, "stt", "server lanes off — students fall back to the browser recogniser",
             "set GROQ_API_KEY, or LOCAL_STT_ENABLED=true")

    print("\nVOICE (speech out)")
    try:
        import piper  # noqa: F401

        have = tts.availability()
        missing = [k for k, v in have.items() if not v]
        if not missing:
            _row(OK, "tts", "Piper voices present for ar/en/fr")
        else:
            _row(WARN, "tts", f"piper installed, no voice for: {', '.join(missing)}",
                 f"python -m app.cli voices {' '.join(missing)}  (else those languages use the device voice)")
    except Exception:
        _row(WARN, "tts", "piper not installed — every language uses the device voice",
             "pip install piper-tts")

    print("\nAVATAR")
    if not settings.liveavatar_enabled:
        _row(WARN, "liveavatar", "off — the local 2D face is used", "LIVEAVATAR_ENABLED=true to stream a human")
    elif not settings.liveavatar_api_key:
        _row(BAD, "liveavatar", "enabled with no LIVEAVATAR_API_KEY", "add the key or set LIVEAVATAR_ENABLED=false")
    elif settings.liveavatar_sandbox:
        _row(OK, "liveavatar", "sandbox — free, fixed demo avatar, ~60s sessions, your voice/avatar ids ignored")
    else:
        _row(OK, "liveavatar", f"billed sessions, avatar={settings.liveavatar_avatar_id or '(unset)'}")

    print()
    if brain_ok and embed_ok:
        print("The tutor can answer questions.\n")
        return 0
    print("The tutor CANNOT answer questions yet — fix the [XX] rows above.\n")
    return 1


if __name__ == "__main__":
    main()
