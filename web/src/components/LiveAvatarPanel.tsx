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
// How long before a session's cap to start its replacement connecting in the background.
// A cold connect (mint token → start session → LiveKit join → wait for the agent) easily
// takes several seconds, so starting only after the old session closes is what makes the
// panel look "slow" every ~60s in sandbox mode. Pre-warming means the swap is a track
// hand-off instead of a full reconnect from a blank panel.
const PREWARM_LEAD_MS = 8000;

const LiveAvatarPanel = forwardRef<LiveAvatarHandle, Props>(function LiveAvatarPanel({ token, lang, label, onState, onReady, onUnavailable }, ref) {
  // Two video elements, cross-faded: the "live" slot is on screen, the other pre-warms the
  // next session silently in the background and is swapped in once it's ready.
  const videoARef = useRef<HTMLVideoElement>(null);
  const videoBRef = useRef<HTMLVideoElement>(null);
  const [activeSlot, setActiveSlot] = useState<"a" | "b">("a");
  const sessionRef = useRef<LiveAvatarSession | null>(null);   // the on-screen session
  const warmRef = useRef<LiveAvatarSession | null>(null);      // the pre-warming replacement, if any
  const [status, setStatus] = useState<"connecting" | "live" | "error">("connecting");
  const [sandbox, setSandbox] = useState(false);
  // A sandbox session stops itself after ~60s, which is ordinary rather than exceptional:
  // reconnect instead of handing the panel back. The counter only guards against a hard
  // failure (bad key, their cloud down) turning into an endless connect loop.
  const failures = useRef(0);
  const closedReconnects = useRef(0);
  const MAX_FAILURES = 4;

  useImperativeHandle(ref, () => ({
    speakText: (text) => sessionRef.current?.speakText(text),
    interrupt: () => sessionRef.current?.interrupt(),
  }), []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let prewarmTimer: number | undefined;

    const videoFor = (slot: "a" | "b") => (slot === "a" ? videoARef : videoBRef).current!;
    const other = (slot: "a" | "b") => (slot === "a" ? "b" : "a");

    // Normal cold connect for the on-screen slot: used on mount, and as a fallback if a
    // session closes before its pre-warmed replacement was ready to take over.
    const connect = (slot: "a" | "b") => {
      const session = new LiveAvatarSession(token);
      sessionRef.current = session;
      session.onState = (s) => {
        if (sessionRef.current !== session) return; // a stale session's late event
        onState(s);
        if (s === "closed" && !cancelled) {
          closedReconnects.current += 1;
          if (closedReconnects.current >= MAX_FAILURES) {
            setStatus("error");
            onUnavailable();
            return;
          }
          setStatus("connecting");
          timer = window.setTimeout(() => connect(slot), 300 * closedReconnects.current);
        }
      };
      (async () => {
        try {
          const { sandbox: isSandbox, maxSessionDuration } = await session.start(lang, videoFor(slot));
          if (cancelled || sessionRef.current !== session) return;
          failures.current = 0;
          closedReconnects.current = 0;   // a session that started proves the lane works
          setSandbox(isSandbox);
          setActiveSlot(slot);
          setStatus("live");
          onReady(isSandbox);
          schedulePrewarm(slot, maxSessionDuration);
        } catch (e) {
          if (cancelled) return;
          failures.current += 1;
          console.error(`LiveAvatarPanel: session failed (${failures.current}/${MAX_FAILURES}) —`, e);
          if (failures.current >= MAX_FAILURES) {
            // Not an expiry any more: the lane itself is down, so stop burning attempts.
            setStatus("error");
            onUnavailable();
            return;
          }
          setStatus("connecting");
          timer = window.setTimeout(() => connect(slot), 500 * failures.current);   // back off
        }
      })();
    };

    // Connects the replacement muted, in the other slot, well before the current session's
    // cap. On success it becomes the visible session with no gap; the old one is torn down
    // only after the swap. On failure it's simply dropped — the active session's own
    // onState("closed") handler above is still there as the fallback path.
    const schedulePrewarm = (activeSlotAtSchedule: "a" | "b", maxSessionDuration: number) => {
      // HeyGen's docs don't pin the unit; treat anything under 1000 as seconds.
      const durMs = maxSessionDuration > 0 ? (maxSessionDuration < 1000 ? maxSessionDuration * 1000 : maxSessionDuration) : 0;
      if (!durMs) return; // unknown duration (e.g. billed, uncapped) — nothing to pre-warm for
      const lead = Math.min(PREWARM_LEAD_MS, Math.max(1000, durMs / 4));
      const delay = Math.max(0, durMs - lead);
      prewarmTimer = window.setTimeout(() => {
        if (cancelled || sessionRef.current === null) return;
        const outgoing = sessionRef.current;
        const slot = other(activeSlotAtSchedule);
        const warm = new LiveAvatarSession(token);
        warmRef.current = warm;
        (async () => {
          try {
            const { sandbox: isSandbox, maxSessionDuration: nextDuration } = await warm.start(lang, videoFor(slot), true);
            if (cancelled || warmRef.current !== warm) return; // superseded or torn down
            warm.unmute();
            sessionRef.current = warm;
            warmRef.current = null;
            setSandbox(isSandbox);
            setActiveSlot(slot);
            warm.onState = (s) => {
              if (sessionRef.current !== warm) return;
              onState(s);
              if (s === "closed" && !cancelled) { setStatus("connecting"); timer = window.setTimeout(() => connect(slot), 300); }
            };
            void outgoing.stop();
            schedulePrewarm(slot, nextDuration);
          } catch (e) {
            // Pre-warm failed silently; the outgoing session's own close handler covers it.
            console.error("LiveAvatarPanel: pre-warm failed —", e);
            if (warmRef.current === warm) warmRef.current = null;
          }
        })();
      }, delay);
    };

    connect("a");
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (prewarmTimer) clearTimeout(prewarmTimer);
      const session = sessionRef.current;
      sessionRef.current = null;
      void session?.stop();
      const warm = warmRef.current;
      warmRef.current = null;
      void warm?.stop();
    };
    // lang/token intentionally not re-run on every render — a new value starts a fresh session
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  return (
    <div className="avatar-wrap live-avatar">
      <video ref={videoARef} autoPlay playsInline muted={activeSlot !== "a"} className={activeSlot === "a" ? "on" : "off"} aria-label={label} />
      <video ref={videoBRef} autoPlay playsInline muted={activeSlot !== "b"} className={activeSlot === "b" ? "on" : "off"} aria-hidden={activeSlot !== "b"} />
      {status === "connecting" && <span className="avatar-state" aria-hidden="true">connecting…</span>}
      {status === "error" && <span className="avatar-state danger" aria-hidden="true">live avatar unavailable</span>}
      {sandbox && status === "live" && <span className="mute-badge" title="No production credits — showing HeyGen's sandbox demo avatar">SANDBOX</span>}
    </div>
  );
});

export default LiveAvatarPanel;
