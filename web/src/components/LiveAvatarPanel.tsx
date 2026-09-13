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
  /** The session is live and can speak. */
  onReady: () => void;
  /** It failed to start, or ended (a sandbox session stops itself after ~60s). The caller
   *  must fall back to local speech — otherwise the tutor goes silent for good. */
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

  useImperativeHandle(ref, () => ({
    speakText: (text) => sessionRef.current?.speakText(text),
    interrupt: () => sessionRef.current?.interrupt(),
  }), []);

  useEffect(() => {
    let cancelled = false;
    const session = new LiveAvatarSession(token);
    sessionRef.current = session;
    session.onState = (s) => {
      onState(s);
      // "closed" is both the ~60s sandbox expiry and any mid-session drop. Either way the
      // session can no longer speak, so hand speech back before the next sentence is queued.
      if (s === "closed" && !cancelled) onUnavailable();
    };
    (async () => {
      try {
        const video = videoRef.current!;
        const { sandbox: isSandbox } = await session.start(lang, video);
        if (cancelled) return;
        setSandbox(isSandbox);
        setStatus("live");
        onReady();
      } catch (e) {
        console.error("LiveAvatarPanel: failed to start session —", e);
        if (!cancelled) { setStatus("error"); onUnavailable(); }
      }
    })();
    return () => { cancelled = true; sessionRef.current = null; void session.stop(); };
    // lang/token intentionally not re-run on every render — a new value starts a fresh session
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

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
