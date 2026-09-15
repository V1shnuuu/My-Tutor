import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { LiveAvatarSession, type LiveAvatarState } from "../lib/liveavatar";
import type { Lang } from "../lib/api";

export interface LiveAvatarHandle {
  speakText: (text: string) => void;
  interrupt: () => void;
}

interface Props {
  token: string;
  lang: Lang;
  label: string;
  onState: (s: LiveAvatarState) => void;
  /** The session is live and can speak. `sandbox` is HeyGen's fixed demo persona, which
   *  speaks English only — the caller needs that to decide what it may hand over. */
  onReady: (sandbox: boolean) => void;
  /** Every attempt to hold a session has failed. Only then does the caller fall back to
   *  local speech — a single expiry is reconnected through, not surrendered to. */
  onUnavailable: () => void;
}

/**
 * Video-based avatar renderer backed by a real HeyGen LiveAvatar session — local/dev-only
 * (see core/app/liveavatar.py). Sits alongside, not inside, the 2D/3D Avatar switcher: those
 * render synchronously from a local viseme clock, this owns an async network session with
 * its own connect/error/teardown lifecycle, so App.tsx swaps the whole panel rather than
 * asking Avatar.tsx to grow a fourth local renderer for it.
 */
const LiveAvatarPanel = forwardRef<LiveAvatarHandle, Props>(function LiveAvatarPanel({ token, lang, label, onState, onReady, onUnavailable }, ref) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const sessionRef = useRef<LiveAvatarSession | null>(null);
  const [status, setStatus] = useState<"connecting" | "live" | "error">("connecting");
  const [sandbox, setSandbox] = useState(false);
  // A sandbox session stops itself after ~60s, which is ordinary rather than exceptional:
  // reconnect instead of handing the panel back. The counter only guards against a hard
  // failure (bad key, their cloud down) turning into an endless connect loop.
  const failures = useRef(0);
  const MAX_FAILURES = 4;

  useImperativeHandle(ref, () => ({
    speakText: (text) => sessionRef.current?.speakText(text),
    interrupt: () => sessionRef.current?.interrupt(),
  }), []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const connect = () => {
      const session = new LiveAvatarSession(token);
      sessionRef.current = session;
      session.onState = (s) => {
        onState(s);
        // The sandbox expiry arrives here as "closed". Reconnect rather than surrender:
        // a fresh session is what keeps one continuous avatar across the cap.
        if (s === "closed" && !cancelled && sessionRef.current === session) {
          setStatus("connecting");
          timer = window.setTimeout(connect, 300);
        }
      };
      (async () => {
        try {
          const video = videoRef.current!;
          const { sandbox: isSandbox } = await session.start(lang, video);
          if (cancelled) return;
          failures.current = 0;      // a session that spoke proves the lane works
          setSandbox(isSandbox);
          setStatus("live");
          onReady(isSandbox);
        } catch (e) {
          if (cancelled) return;
          failures.current += 1;
          console.error(`LiveAvatarPanel: session failed (${failures.current}/${MAX_FAILURES}) —`, e);
          if (failures.current >= MAX_FAILURES) {
            // Not a expiry any more: the lane itself is down, so stop burning attempts.
            setStatus("error");
            onUnavailable();
            return;
          }
          setStatus("connecting");
          timer = window.setTimeout(connect, 500 * failures.current);   // back off
        }
      })();
    };

    connect();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      const session = sessionRef.current;
      sessionRef.current = null;
      void session?.stop();
    };
    // HeyGen's language is a session-level persona property (core/app/liveavatar.py), not a
    // per-message one, so a language switch must tear down and reconnect with the new lang —
    // otherwise the avatar keeps speaking whatever language it first connected with.
  }, [token, lang]);

  return (
    <div className="avatar-wrap live-avatar">
      <video ref={videoRef} autoPlay playsInline aria-label={label} />
      {status === "connecting" && <span className="avatar-state" aria-hidden="true">connecting…</span>}
      {status === "error" && <span className="avatar-state danger" aria-hidden="true">live avatar unavailable</span>}
      {sandbox && status === "live" && <span className="mute-badge" title="No production credits — showing HeyGen's sandbox demo avatar">SANDBOX</span>}
    </div>
  );
});

export default LiveAvatarPanel;
