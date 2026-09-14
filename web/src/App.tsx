import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Avatar, { type AvatarState } from "./components/Avatar";
import Chat, { type LiveState } from "./components/Chat";
import LiveAvatarPanel, { type LiveAvatarHandle } from "./components/LiveAvatarPanel";
import Login from "./components/Login";
import VideoPlayer, { type PlayerHandle } from "./components/VideoPlayer";
import { ApiError, chat as chatApi, listVideos, me, type Citation, type Lang, type Video } from "./lib/api";

// Local/dev-only: HeyGen LiveAvatar bills per minute from its own cloud, so it can't be the
// free default — opt in with VITE_LIVE_AVATAR=1 in web/.env.local. See core/app/liveavatar.py.
const LIVE_AVATAR = import.meta.env.VITE_LIVE_AVATAR === "1";
import { dirOf, t } from "./lib/i18n";
import { SentenceSplitter, Speaker, hasSpeech, isUnlocked, resumeAudio, unlockAudio } from "./lib/speech";
import { startListening, type ListenSession } from "./lib/stt";
import { db, getPref, getToken, setPref, setToken, type StoredMessage } from "./lib/store";

const speaker = new Speaker();

export default function App() {
  const [token, setTok] = useState<string | null>(getToken());
  const [lang, setLang] = useState<Lang>((getPref("lang") as Lang) || (navigator.language.startsWith("ar") ? "ar" : navigator.language.startsWith("fr") ? "fr" : "en"));
  const [videos, setVideos] = useState<Video[]>([]);
  const [activeVideo, setActiveVideo] = useState<string | null>(null);
  const [messages, setMessages] = useState<StoredMessage[]>([]);
  const [live, setLive] = useState<LiveState>({ stage: "idle" });
  const [streaming, setStreaming] = useState(false);
  const [speakerState, setSpeakerState] = useState<"idle" | "speaking">("idle");
  const [speakingSentence, setSpeakingSentence] = useState<string | null>(null);
  const [voiceOn, setVoiceOn] = useState(getPref("voice") !== "off");
  const [unlocked, setUnlocked] = useState(false);
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [sttMode, setSttMode] = useState<"server" | "browser" | null>(null);
  const [sttServer, setSttServer] = useState(false);
  const [budget, setBudget] = useState<{ used: number; cap: number } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [videoCollapsed, setVideoCollapsed] = useState(false);
  const [view, setView] = useState<"notebook" | "split">((getPref("view") as "notebook" | "split") || "split");
  // LiveAvatar is an enhancement, never a dependency: a sandbox session stops itself after
  // ~60s and a failed one never starts, so speech re-checks this before every sentence and
  // falls back to Piper/the OS voice. `liveUp` is a ref because the streaming callbacks below
  // are created once per send and must see the current value, not the one they closed over.
  const liveUp = useRef(false);
  const avatarSandbox = useRef(false);
  // Hands-free: the mic re-arms itself after the tutor finishes speaking, and the energy
  // VAD in lib/stt.ts closes it when the student stops. Off by default — arming a
  // microphone is the user's decision, and the first tap is also the gesture the browser
  // needs before it will grant mic access or unlock audio.
  const [handsFree, setHandsFree] = useState(getPref("handsfree") === "on");
  // The VAD polls this every frame from inside a callback created once per session, so it
  // has to read live state rather than whatever was captured when listening began.
  const speakerStateRef = useRef<"idle" | "speaking">("idle");
  const arming = useRef(false);
  const [liveDown, setLiveDown] = useState(false);
  const player = useRef<PlayerHandle>(null);
  const session = useRef<ListenSession | null>(null);
  const sentences = useRef<Map<number, string>>(new Map());
  const liveAvatar = useRef<LiveAvatarHandle>(null);

  // ---- bootstrap after login
  useEffect(() => {
    if (!token) return;
    let alive = true;
    (async () => {
      try {
        const m = await me(token);
        if (!alive) return;
        setBudget(m.budget); setSttServer(m.stt);
        speaker.configure(token, m.tts || {});
        const vs = await listVideos(token);
        if (!alive) return;
        setVideos(vs); setActiveVideo((a) => a || vs[0]?.id || null);
        const hist = await db.messages.orderBy("ts").toArray();
        if (!alive) return;
        setMessages(hist.length ? hist : [{ role: "assistant", content: t("welcome", lang), lang, citations: [], ts: Date.now() }]);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) { setToken(null); setTok(null); }
        else setToast(t("offline", lang));
      }
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // ---- speaker wiring
  useEffect(() => { speakerStateRef.current = speakerState; }, [speakerState]);
  useEffect(() => {
    speaker.onState = setSpeakerState;
    speaker.onSentence = (id) => setSpeakingSentence(id == null ? null : sentences.current.get(id) ?? null);
    speaker.setEnabled(voiceOn);
  }, [voiceOn]);
  useEffect(() => {
    const onVis = () => { if (document.hidden) speaker.stop(); else resumeAudio(); };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);
  useEffect(() => { document.documentElement.dir = dirOf(lang); document.documentElement.lang = lang; }, [lang]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "t" && e.key !== "T") return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable || /^(input|textarea|select)$/i.test(el.tagName))) return;
      setView((v) => { const next = v === "split" ? "notebook" : "split"; setPref("view", next); return next; });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const ensureUnlocked = useCallback(() => {
    if (!isUnlocked()) setUnlocked(unlockAudio());
    else setUnlocked(true);
  }, []);

  const getViseme = useCallback(() => speaker.current_viseme(), []);

  // Which engine says this sentence out loud.
  //
  // The avatar takes every language. Sandbox drops the configured voice_id, but
  // liveavatar.py still sends persona.language, so HeyGen speaks the answer's language in
  // its own voice rather than an English one. Piper stays as the fallback for when no
  // session can be held at all.
  const avatarSpeaks = (_l: Lang) => LIVE_AVATAR && liveUp.current;
  // Interrupting an inactive engine is a no-op, so stop both rather than guessing which
  // one is mid-sentence when a session drops.
  const stopSpeech = useCallback(() => { liveAvatar.current?.interrupt(); speaker.stop(); }, []);
  const onLiveReady = useCallback((sandbox: boolean) => {
    liveUp.current = true;
    avatarSandbox.current = sandbox;
    setLiveDown(false);
  }, []);
  // Speech must not be queued at a session that is mid-reconnect; it would be dropped
  // silently. The panel reconnects on its own, so this only pauses the hand-off.
  const onLivePaused = useCallback(() => { liveUp.current = false; }, []);
  const onLiveUnavailable = useCallback(() => { liveUp.current = false; setLiveDown(true); }, []);

  // ---- send a message and stream the answer
  const send = useCallback(async (text: string) => {
    if (!token || streaming) return;
    ensureUnlocked();
    stopSpeech();
    sentences.current.clear();
    const userMsg: StoredMessage = { role: "user", content: text, lang, citations: [], ts: Date.now() };
    userMsg.id = await db.messages.add(userMsg);
    const asst: StoredMessage = { role: "assistant", content: "", lang, citations: [], ts: Date.now() };
    setMessages((m) => [...m, userMsg, asst]);
    setStreaming(true); setLive({ stage: "retrieving" });
    const history = messages.slice(-6).map((m) => ({ role: m.role, content: m.content }));
    const splitter = new SentenceSplitter((s) => {
      if (avatarSpeaks(asst.lang)) { liveAvatar.current?.speakText(s); return; }
      const id = speaker.enqueue(s, asst.lang);
      sentences.current.set(id, s);
    });
    let content = "";
    const update = (patch: Partial<StoredMessage>) => setMessages((m) => { const c = [...m]; c[c.length - 1] = { ...c[c.length - 1], ...patch }; return c; });
    try {
      // Spoken answers get a much shorter register; a muted session keeps the reading length.
      for await (const ev of chatApi(token, text, history, lang, voiceOn)) {
        if (ev.type === "meta") {
          asst.lang = ev.lang; setLang(ev.lang); setPref("lang", ev.lang); setBudget(ev.budget); update({ lang: ev.lang });
        } else if (ev.type === "status") {
          setLive({ stage: ev.stage, text: ev.text, eta: ev.eta });
        } else if (ev.type === "citations") {
          asst.citations = ev.items; update({ citations: ev.items });
        } else if (ev.type === "token") {
          content += ev.text; update({ content }); splitter.push(ev.text);
        } else if (ev.type === "replace") {
          stopSpeech();
          content = ev.text; update({ content });
          for (const s of ev.text.split(/\n+/)) {
            if (!s.trim()) continue;
            if (avatarSpeaks(asst.lang)) liveAvatar.current?.speakText(s.trim());
            else sentences.current.set(speaker.enqueue(s, asst.lang), s.trim());
          }
        } else if (ev.type === "error") {
          // The server gave up early (e.g. the encoder is unavailable). Say so in place of
          // the empty answer bubble rather than leaving the student watching a spinner.
          const msg = t("offline", lang);
          asst.content = msg; update({ content: msg });
        } else if (ev.type === "done") {
          splitter.flush();
          asst.content = ev.answer || content; asst.source = ev.source;
          update({ content: asst.content, source: ev.source });
          if (ev.source !== "refusal") setBudget((b) => (b ? { ...b, used: b.used + 1 } : b));
        }
      }
    } catch (e) {
      splitter.flush();
      const code = e instanceof ApiError ? e.code : "offline";
      const msg = code === "daily_cap" ? t("daily_cap", lang) : code === "minute_cap" ? t("minute_cap", lang) : t("offline", lang);
      if (!content) { asst.content = msg; update({ content: msg }); } else setToast(msg);
    } finally {
      setStreaming(false); setLive({ stage: "idle" });
      asst.content = asst.content || content;
      asst.id = await db.messages.add({ ...asst });
    }
  }, [token, streaming, lang, messages, ensureUnlocked]);

  // ---- voice input
  const mic = useCallback(async () => {
    if (!token) return;
    ensureUnlocked();
    stopSpeech();
    setInterim(""); setListening(true);
    session.current = await startListening({
      token, langHint: lang, serverAvailable: sttServer,
      onInterim: setInterim,
      tutorSpeaking: () => speakerStateRef.current === "speaking",
      // Barge-in: the student started talking over the answer, so drop it mid-sentence.
      onSpeechStart: () => { if (speakerStateRef.current === "speaking") stopSpeech(); },
      // A mode switch is the STT layer reporting that the server lane is done for this
      // session; drop serverAvailable too or every later attempt retries the same failure.
      onMode: (m) => { setSttMode(m); if (m === "browser") setSttServer(false); },
      onFinal: (text, l) => {
        setListening(false); setInterim(""); session.current = null;
        if (l) setLang(l);
        if (text.trim()) void send(text.trim());
      },
      onError: (code) => {
        setListening(false); setInterim(""); session.current = null;
        // Hands-free would otherwise retry a broken mic forever, one attempt per gap.
        if (code === "mic_denied" || code === "stt_unavailable") {
          setHandsFree(false);
          setPref("handsfree", "off");
        }
        setToast(code === "stt_unavailable" ? t("stt_unavailable", lang) : code === "mic_denied" ? "🎙 ✗" : t("stt_unavailable", lang));
      },
    });
  }, [token, lang, sttServer, send, ensureUnlocked]);
  const stopMic = useCallback(() => session.current?.stop(), []);

  // Re-arm only in the gaps: never while the tutor is talking (its own voice through the
  // speakers is exactly what the VAD would hear), never mid-answer, never in a hidden tab.
  useEffect(() => {
    if (!handsFree || !token) return;
    // Deliberately no longer excludes speakerState === "speaking": the mic stays open
    // through the answer so an interruption lands immediately. lib/stt.ts raises the
    // threshold for the duration so the tutor cannot interrupt itself.
    if (listening || streaming || document.hidden) return;
    if (arming.current) return;
    arming.current = true;
    // A beat after the audio stops, so the tail of the last word doesn't open the mic.
    const id = setTimeout(() => { arming.current = false; void mic(); }, 500);
    return () => { clearTimeout(id); arming.current = false; };
  }, [handsFree, token, listening, streaming, speakerState, mic]);

  const toggleHandsFree = () => {
    const on = !handsFree;
    setHandsFree(on);
    setPref("handsfree", on ? "on" : "off");
    if (on) ensureUnlocked(); else stopMic();
  };

  const jump = useCallback((c: Citation) => {
    stopSpeech();
    setVideoCollapsed(false);
    setView("split"); setPref("view", "split");
    player.current?.jump(c.video_id, c.t);
    document.querySelector(".panel-video")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, []);

  const newChat = async () => { stopSpeech(); await db.messages.clear(); setMessages([{ role: "assistant", content: t("welcome", lang), lang, citations: [], ts: Date.now() }]); };
  const toggleVoice = () => { ensureUnlocked(); const v = !voiceOn; setVoiceOn(v); setPref("voice", v ? "on" : "off"); };

  useEffect(() => { if (toast) { const id = setTimeout(() => setToast(null), 5000); return () => clearTimeout(id); } }, [toast]);

  const avatarState: AvatarState = useMemo(() => {
    if (listening) return "listening";
    if (speakerState === "speaking") return "speaking";
    if (streaming) return "thinking";
    if (!voiceOn || (!unlocked && !isUnlocked()) || (!hasSpeech() && speaker.engineFor(lang) === "none")) return "muted";
    return "idle";
  }, [listening, speakerState, streaming, voiceOn, unlocked]);

  if (!token) return <Login lang={lang} onLang={setLang} onToken={(tk) => { setToken(tk); setTok(tk); }} />;

  const engine = speaker.engineFor(lang);
  const topbar = (
    <div className="topbar">
      <span className="brand">📓 {t("appName", lang)}</span>
      {budget && <span className="pill">{t("budget", lang)}: {budget.used}/{budget.cap}</span>}
      <span className={`pill ${voiceOn ? "ok" : ""}`}><button onClick={toggleVoice}>{voiceOn ? t("voice_on", lang) : t("voice_off", lang)}</button></span>
      {voiceOn && engine === "none" && <span className="pill warn" title="No voice for this language on this device or server">{lang} voice ✗</span>}
      {voiceOn && engine === "server" && <span className="pill" title="Server voice (Piper)">🗣 piper</span>}
      <span className={`pill ${handsFree ? "ok" : ""}`}>
        <button onClick={toggleHandsFree} aria-pressed={handsFree}>
          {handsFree ? t("handsfree_on", lang) : t("handsfree_off", lang)}
        </button>
      </span>
      <span className="pill"><button onClick={newChat}>{t("new_chat", lang)}</button></span>
      <span className="viewtoggle">
        <span className="label">{t("view", lang)}</span>
        <span className="segmented" role="group" aria-label={t("view", lang)}>
          {(["notebook", "split"] as const).map((v) => (
            <button key={v} aria-pressed={view === v} onClick={() => { setView(v); setPref("view", v); }}>
              {t(v === "notebook" ? "view_notebook" : "view_split", lang)}
            </button>
          ))}
        </span>
        <span className="kbd-hint">{t("view_hint", lang)}</span>
      </span>
      <span className="pill">
        {(["ar", "en", "fr"] as Lang[]).map((l) => <button key={l} onClick={() => { setLang(l); setPref("lang", l); }} style={{ fontWeight: l === lang ? 700 : 400 }}>{l.toUpperCase()}</button>)}
      </span>
      {toast && <span className="pill danger" role="status">{toast}</span>}
    </div>
  );

  const avatar = (
    <section className="panel panel-avatar" aria-label={t("avatar_label", lang)}>
      {/* Once the session is gone, swap the dead <video> for the local face: it lip-syncs the
          Piper/OS voice that speech has already fallen back to, so the tutor keeps a talking
          head instead of an error caption. Not re-mounted afterwards — a sandbox session
          cannot be resumed, and retrying would loop. */}
      {LIVE_AVATAR && !liveDown ? (
        <LiveAvatarPanel
          ref={liveAvatar}
          token={token}
          lang={lang}
          label={t("avatar_label", lang)}
          onState={(s) => {
            setSpeakerState(s === "speaking" ? "speaking" : "idle");
            if (s === "closed" || s === "error") onLivePaused();
          }}
          onReady={onLiveReady}
          onUnavailable={onLiveUnavailable}
        />
      ) : (
        <Avatar state={avatarState} getViseme={getViseme} label={t("avatar_label", lang)} stateLabel={t(`state_${avatarState}` as const, lang)} muteLabel={t("enable_voice", lang)} onUnmute={() => { ensureUnlocked(); if (!voiceOn) toggleVoice(); }} />
      )}
    </section>
  );
  const video = (
    <section className="panel panel-video" aria-label="Lecture video" onClick={() => videoCollapsed && setVideoCollapsed(false)}>
      <VideoPlayer ref={player} token={token} videos={videos} lang={lang} activeId={activeVideo} onActiveChange={setActiveVideo} />
    </section>
  );

  return (
    <div className={`app view-${view}`} lang={lang}>
      <div className={`top ${videoCollapsed ? "video-collapsed" : ""}`} >
        {avatar}
        {video}
      </div>
      <section className="panel panel-chat" aria-label="Chat">
        {topbar}
        <Chat lang={lang} messages={messages} live={live} streaming={streaming} speakingSentence={speakingSentence} interim={interim} listening={listening} sttMode={sttMode} onSend={send} onMic={mic} onStop={stopMic} onJump={jump} />
      </section>
    </div>
  );
}
