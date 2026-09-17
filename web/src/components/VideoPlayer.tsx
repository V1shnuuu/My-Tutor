import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { transcript as fetchTranscript, type Lang, type Video } from "../lib/api";
import { t } from "../lib/i18n";
import QuizPanel from "./QuizPanel";

export interface PlayerHandle {
  jump: (videoId: string, seconds: number) => void;
}
interface Props {
  token: string;
  videos: Video[];
  lang: Lang;
  activeId: string | null;
  onActiveChange: (id: string) => void;
}
interface Cue { start: number; end: number; text: string }

/* ---- minimal YouTube IFrame API typing ---- */
interface YTPlayer {
  playVideo(): void; pauseVideo(): void; seekTo(s: number, allow?: boolean): void; getCurrentTime(): number; getDuration(): number;
  setPlaybackRate(r: number): void; mute(): void; unMute(): void; isMuted(): boolean; getPlayerState(): number; loadVideoById(id: string, start?: number): void; destroy(): void;
}
interface YTNamespace { Player: new (el: HTMLElement, opts: unknown) => YTPlayer; PlayerState: { PLAYING: number; ENDED: number } }
declare global { interface Window { YT?: YTNamespace; onYouTubeIframeAPIReady?: () => void } }

let ytReady: Promise<YTNamespace> | null = null;
function loadYT(): Promise<YTNamespace> {
  if (ytReady) return ytReady;
  ytReady = new Promise((resolve) => {
    if (window.YT?.Player) return resolve(window.YT);
    window.onYouTubeIframeAPIReady = () => resolve(window.YT!);
    const s = document.createElement("script");
    s.src = "https://www.youtube.com/iframe_api";
    document.head.appendChild(s);
  });
  return ytReady;
}

const fmt = (s: number) => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};
const SPEEDS = [0.75, 1, 1.25, 1.5, 2];

/**
 * Custom "VLC-like" chrome over the YouTube IFrame API (controls=0) or a plain <video>.
 * Captions come from our transcript cues, not YouTube's, so they match the RAG citations.
 */
const VideoPlayer = forwardRef<PlayerHandle, Props>(function VideoPlayer({ token, videos, lang, activeId, onActiveChange }, ref) {
  const video = useMemo(() => videos.find((v) => v.id === activeId) || videos[0] || null, [videos, activeId]);
  const root = useRef<HTMLDivElement>(null);
  const ytHost = useRef<HTMLDivElement>(null);
  const yt = useRef<YTPlayer | null>(null);
  const html5 = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [cc, setCc] = useState(true);
  const [muted, setMuted] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [cues, setCues] = useState<Cue[]>([]);
  const [showQuiz, setShowQuiz] = useState(false);
  const pendingSeek = useRef<number | null>(null);
  const isYT = video?.source === "youtube" && !!video.youtube_id;

  // A new video means a new quiz, not the previous one's questions left on screen.
  useEffect(() => { setShowQuiz(false); }, [video?.id]);

  // Captions are always English or Arabic, never the video's own spoken language — an
  // admin-connected YouTube playlist can be in anything (Tamil, French, whatever); the
  // French UI still gets English captions rather than a third caption language nobody asked
  // to support. See core/app/captions_translate.py for the actual translation.
  const capLang: "en" | "ar" = lang === "ar" ? "ar" : "en";

  // transcript cues for the caption overlay
  useEffect(() => {
    if (!video) return;
    let alive = true;
    fetchTranscript(token, video.id, capLang).then((c) => alive && setCues(c));
    return () => { alive = false; };
  }, [token, video, capLang]);

  // YouTube player lifecycle
  useEffect(() => {
    if (!isYT || !video?.youtube_id || !ytHost.current) return;
    let disposed = false;
    const host = ytHost.current;
    const mount = document.createElement("div");
    host.appendChild(mount);
    loadYT().then((YT) => {
      if (disposed) return;
      yt.current = new YT.Player(mount, {
        videoId: video.youtube_id,
        playerVars: { controls: 0, rel: 0, modestbranding: 1, playsinline: 1, disablekb: 1, cc_load_policy: 0, iv_load_policy: 3, origin: location.origin },
        events: {
          onReady: () => {
            setDuration(yt.current?.getDuration() || video.duration || 0);
            yt.current?.setPlaybackRate(speed);
            if (pendingSeek.current != null) { yt.current?.seekTo(pendingSeek.current, true); yt.current?.playVideo(); pendingSeek.current = null; }
          },
          onStateChange: (e: { data: number }) => setPlaying(e.data === YT.PlayerState.PLAYING),
        },
      });
    });
    return () => { disposed = true; try { yt.current?.destroy(); } catch { /* */ } yt.current = null; host.innerHTML = ""; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isYT, video?.youtube_id]);

  // time polling (250 ms) — drives scrubber + caption overlay for both backends
  useEffect(() => {
    const id = setInterval(() => {
      if (isYT && yt.current) {
        try { setTime(yt.current.getCurrentTime() || 0); const d = yt.current.getDuration(); if (d) setDuration(d); } catch { /* not ready */ }
      } else if (html5.current) {
        setTime(html5.current.currentTime);
        if (html5.current.duration) setDuration(html5.current.duration);
      }
    }, 250);
    return () => clearInterval(id);
  }, [isYT]);

  const seek = useCallback((s: number, play = true) => {
    if (isYT) {
      if (!yt.current) { pendingSeek.current = s; return; }
      yt.current.seekTo(s, true);
      if (play) yt.current.playVideo();
    } else if (html5.current) {
      html5.current.currentTime = s;
      if (play) void html5.current.play();
    }
  }, [isYT]);

  useImperativeHandle(ref, () => ({
    jump: (videoId, seconds) => {
      if (video?.id !== videoId) {
        pendingSeek.current = seconds;
        onActiveChange(videoId);
      } else seek(seconds, true);
    },
  }), [video?.id, onActiveChange, seek]);

  // pending seek after switching videos (HTML5 path; YT path handled in onReady)
  useEffect(() => {
    if (!isYT && pendingSeek.current != null && html5.current) {
      const s = pendingSeek.current; pendingSeek.current = null;
      const el = html5.current;
      const go = () => { el.currentTime = s; void el.play(); };
      if (el.readyState >= 1) go(); else el.addEventListener("loadedmetadata", go, { once: true });
    }
  }, [video?.id, isYT]);

  const toggle = () => {
    if (isYT) { if (!yt.current) return; playing ? yt.current.pauseVideo() : yt.current.playVideo(); }
    else if (html5.current) { html5.current.paused ? void html5.current.play() : html5.current.pause(); setPlaying(html5.current.paused === false); }
  };
  const changeSpeed = (r: number) => { setSpeed(r); if (isYT) yt.current?.setPlaybackRate(r); else if (html5.current) html5.current.playbackRate = r; };
  const toggleMute = () => {
    const m = !muted; setMuted(m);
    if (isYT) { m ? yt.current?.mute() : yt.current?.unMute(); } else if (html5.current) html5.current.muted = m;
  };
  const toggleFullscreen = () => {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void root.current?.requestFullscreen();
  };
  useEffect(() => {
    const onChange = () => setFullscreen(document.fullscreenElement === root.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);
  const onKey = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).tagName === "SELECT" || (e.target as HTMLElement).tagName === "INPUT") return;
    const map: Record<string, () => void> = {
      " ": toggle, k: toggle, j: () => seek(Math.max(0, time - 10), playing), l: () => seek(time + 10, playing),
      ",": () => changeSpeed(SPEEDS[Math.max(0, SPEEDS.indexOf(speed) - 1)]), ".": () => changeSpeed(SPEEDS[Math.min(SPEEDS.length - 1, SPEEDS.indexOf(speed) + 1)]),
      c: () => setCc((v) => !v), m: toggleMute, f: toggleFullscreen,
    };
    const fn = map[e.key.toLowerCase()] || map[e.key];
    if (fn) { e.preventDefault(); fn(); }
  };

  const cue = cc ? cues.find((c) => time >= c.start && time <= c.end) : undefined;

  if (!video) return (
    <div className="video-empty">
      <span className="placeholder-icon" aria-hidden="true">🎬</span>
      <span>{t("no_video", lang)}</span>
    </div>
  );

  return (
    <div ref={root} className={`video${videos.length > 1 ? " has-picker" : ""}`} tabIndex={0} onKeyDown={onKey} aria-label={video.title}>
      {videos.length > 1 && (
        <div className="video-picker">
          <select value={video.id} onChange={(e) => onActiveChange(e.target.value)} aria-label="Video">
            {videos.map((v) => <option key={v.id} value={v.id}>{v.title}</option>)}
          </select>
        </div>
      )}
      <div className="video-stage">
        {isYT ? (
          <>
            <div ref={ytHost} style={{ position: "absolute", inset: 0 }} />
            {/* blocks pointer events before they reach the iframe, so YouTube's own hover chrome (share, watch-on-youtube, end screen) never renders */}
            <div className="yt-shield" onClick={toggle} />
          </>
        ) : (
          <video ref={html5} src={video.url || undefined} playsInline preload="metadata" onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}>
            <track kind="captions" src={`${import.meta.env.VITE_API_URL || "http://127.0.0.1:8000"}/videos/${video.id}/captions.vtt?lang=${capLang}`} srcLang={capLang} default />
          </video>
        )}
        {cue && <div className="captions" aria-live="off"><span dir="auto">{cue.text}</span></div>}
      </div>
      <div className="controls" role="group" aria-label="Player controls">
        <button onClick={toggle} aria-label={playing ? t("pause", lang) : t("play", lang)}>{playing ? "❚❚" : "▶"}</button>
        <span className="time">{fmt(time)} / {fmt(duration)}</span>
        <input type="range" min={0} max={Math.max(1, duration)} step={0.5} value={Math.min(time, duration || 0)} onChange={(e) => seek(Number(e.target.value), playing)} aria-label="Seek" />
        <select value={speed} onChange={(e) => changeSpeed(Number(e.target.value))} aria-label={t("speed", lang)}>
          {SPEEDS.map((s) => <option key={s} value={s}>{s}×</option>)}
        </select>
        <button onClick={() => setCc((v) => !v)} aria-pressed={cc} aria-label="Captions">{t("captions", lang)}</button>
        <button onClick={toggleMute} aria-pressed={muted} aria-label="Mute">{muted ? "🔇" : "🔊"}</button>
        <button onClick={toggleFullscreen} aria-pressed={fullscreen} aria-label={fullscreen ? "Exit fullscreen" : "Fullscreen"}>{fullscreen ? "⤡" : "⛶"}</button>
        <button onClick={() => setShowQuiz((v) => !v)} aria-pressed={showQuiz}>📝 {t("practice_questions", lang)}</button>
      </div>
      {showQuiz && <QuizPanel videoId={video.id} lang={lang} onJump={(s) => seek(s, true)} />}
    </div>
  );
});

export default VideoPlayer;
