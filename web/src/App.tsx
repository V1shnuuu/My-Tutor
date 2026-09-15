import { useCallback, useEffect, useRef, useState } from "react";
import Chat, { type LiveState } from "./components/Chat";
import Curriculum from "./components/Curriculum";
import LiveAvatarPanel, { type LiveAvatarHandle } from "./components/LiveAvatarPanel";
import VideoPlayer, { type PlayerHandle } from "./components/VideoPlayer";
import { ApiError, chat as chatApi, listVideos, me, type Citation, type Lang, type Video } from "./lib/api";

import { dirOf, t } from "./lib/i18n";
import { SentenceSplitter, Speaker, isUnlocked, resumeAudio, unlockAudio } from "./lib/speech";
import { startListening, type ListenSession } from "./lib/stt";
import { db, getPref, setPref, type StoredMessage } from "./lib/store";

const speaker = new Speaker();

// No login/enrollment gate: every visitor is this one fixed anonymous identity. The value
// only has to be a non-empty string — the backend no longer checks it at all (core/app/main.py).
const TOKEN = "anon";

type Theme = "light" | "dark";

export default function App() {
  const token = TOKEN;
  const [theme, setTheme] = useState<Theme>((getPref("theme") as Theme) || "light");
  const [lang, setLang] = useState<Lang>((getPref("lang") as Lang) || (navigator.language.startsWith("ar") ? "ar" : navigator.language.startsWith("fr") ? "fr" : "en"));
  const [videos, setVideos] = useState<Video[]>([]);
  const [activeVideo, setActiveVideo] = useState<string | null>(null);
  const [messages, setMessages] = useState<StoredMessage[]>([]);
  const [live, setLive] = useState<LiveState>({ stage: "idle" });
  const [streaming, setStreaming] = useState(false);
  const [speakerState, setSpeakerState] = useState<"idle" | "speaking">("idle");
  const [speakingSentence, setSpeakingSentence] = useState<string | null>(null);
  const [voiceOn, setVoiceOn] = useState(getPref("voice") !== "off");
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [sttMode, setSttMode] = useState<"server" | "browser" | null>(null);
  const [sttServer, setSttServer] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [videoCollapsed, setVideoCollapsed] = useState(false);
  // Whether to stream HeyGen instead of the local face. The server decides — it holds the
  // key — so there is nothing to configure in the web app. Until /me answers, no.
  const [liveAvatarOn, setLiveAvatarOn] = useState(false);
  // LiveAvatar is an enhancement, never a dependency: a sandbox session stops itself after
  // ~60s and a failed one never starts, so speech re-checks this before every sentence and
  // falls back to Piper/the OS voice. `liveUp` is a ref because the streaming callbacks below
  // are created once per send and must see the current value, not the one they closed over.
  const liveUp = useRef(false);
  // Sentences that arrived while the avatar session was mid-reconnect (sandbox's ~60s cap,
  // or a language switch tearing the session down). Queued here instead of falling straight
  // to the local voice, so a routine reconnect gap doesn't look like "the avatar went silent" —
  // flushed to the avatar the moment it's ready again, or to the local voice if it takes too long.
  const pendingAvatar = useRef<{ text: string; lang: Lang }[]>([]);
  const pendingAvatarTimer = useRef<number | undefined>(undefined);
  // Hands-free: the mic re-arms itself after the tutor finishes speaking, and the energy
  // VAD in lib/stt.ts closes it when the student stops. Off by default — arming a
  // microphone is the user's decision, and the first tap is also the gesture the browser
  // needs before it will grant mic access or unlock audio.
  const [handsFree, setHandsFree] = useState(getPref("handsfree") === "on");
  // The VAD polls this every frame from inside a callback created once per session, so it
  // has to read live state rather than whatever was captured when listening began.
  const speakerStateRef = useRef<"idle" | "speaking">("idle");
  const arming = useRef(false);
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
        setSttServer(m.stt); setLiveAvatarOn(!!m.avatar);
        speaker.configure(token, m.tts || {});
        const vs = await listVideos(token);
        if (!alive) return;
        setVideos(vs); setActiveVideo((a) => a || vs[0]?.id || null);
        const hist = await db.messages.orderBy("ts").toArray();
        if (!alive) return;
        setMessages(hist.length ? hist : [{ role: "assistant", content: t("welcome", lang), lang, citations: [], ts: Date.now() }]);
      } catch (e) {
        console.error("bootstrap: /me or /videos failed —", e instanceof ApiError ? { status: e.status, code: e.code } : e);
        setToast(t("offline", lang));
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
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  const toggleTheme = () => { const next = theme === "dark" ? "light" : "dark"; setTheme(next); setPref("theme", next); };

  const ensureUnlocked = useCallback(() => {
    if (!isUnlocked()) unlockAudio();
  }, []);

  // Which engine says this sentence out loud.
  //
  // The avatar takes every language. Sandbox drops the configured voice_id, but
  // liveavatar.py still sends persona.language, so HeyGen speaks the answer's language in
  // its own voice rather than an English one. Piper stays as the fallback for when no
  // session can be held at all.
  const avatarSpeaks = (_l: Lang) => liveAvatarOn && liveUp.current;
  // Interrupting an inactive engine is a no-op, so stop both rather than guessing which
  // one is mid-sentence when a session drops.
  const stopSpeech = useCallback(() => {
    liveAvatar.current?.interrupt(); speaker.stop();
    if (pendingAvatarTimer.current) { clearTimeout(pendingAvatarTimer.current); pendingAvatarTimer.current = undefined; }
    pendingAvatar.current = [];
  }, []);
  // Sentences queued while the avatar was mid-reconnect get spoken now, in order.
  const flushPendingToAvatar = () => {
    if (pendingAvatarTimer.current) { clearTimeout(pendingAvatarTimer.current); pendingAvatarTimer.current = undefined; }
    const queued = pendingAvatar.current;
    pendingAvatar.current = [];
    for (const { text } of queued) liveAvatar.current?.speakText(text);
  };
  const onLiveReady = useCallback(() => {
    liveUp.current = true;
    flushPendingToAvatar();
  }, []);
  // Speech must not be queued at a session that is mid-reconnect; it would be dropped
  // silently. The panel reconnects on its own, so this only pauses the hand-off — any
  // sentence that arrives in the meantime waits in pendingAvatar rather than jumping to the
  // local voice, unless the reconnect drags on long enough that waiting stops helping.
  const onLivePaused = useCallback(() => {
    liveUp.current = false;
    if (pendingAvatar.current.length && !pendingAvatarTimer.current) {
      pendingAvatarTimer.current = window.setTimeout(() => {
        pendingAvatarTimer.current = undefined;
        const queued = pendingAvatar.current;
        pendingAvatar.current = [];
        for (const { text, lang: l } of queued) sentences.current.set(speaker.enqueue(text, l), text);
      }, 12000);
    }
  }, []);
  const onLiveUnavailable = useCallback(() => {
    liveUp.current = false;
    // The lane itself is down (not a transient reconnect) — nothing queued will ever be
    // flushed to it, so hand it to the local voice right away instead of waiting out the timer.
    if (pendingAvatarTimer.current) { clearTimeout(pendingAvatarTimer.current); pendingAvatarTimer.current = undefined; }
    const queued = pendingAvatar.current;
    pendingAvatar.current = [];
    for (const { text, lang: l } of queued) sentences.current.set(speaker.enqueue(text, l), text);
  }, []);

  // Routes one sentence to whichever engine should speak it: the avatar when it's up, the
  // local voice when the avatar is off entirely, or a short hold in pendingAvatar when it's
  // only mid-reconnect — see onLivePaused/onLiveReady for how that queue drains.
  const speakSentence = (text: string, l: Lang) => {
    if (avatarSpeaks(l)) { liveAvatar.current?.speakText(text); return; }
    if (liveAvatarOn) { pendingAvatar.current.push({ text, lang: l }); return; }
    sentences.current.set(speaker.enqueue(text, l), text);
  };

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
    const splitter = new SentenceSplitter((s) => speakSentence(s, asst.lang));
    let content = "";
    const update = (patch: Partial<StoredMessage>) => setMessages((m) => { const c = [...m]; c[c.length - 1] = { ...c[c.length - 1], ...patch }; return c; });
    try {
      // Spoken answers get a much shorter register; a muted session keeps the reading length.
      for await (const ev of chatApi(token, text, history, lang, voiceOn)) {
        if (ev.type === "meta") {
          asst.lang = ev.lang; setLang(ev.lang); setPref("lang", ev.lang); update({ lang: ev.lang });
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
            speakSentence(s.trim(), asst.lang);
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
        if (code === "mic_denied" || code === "stt_unavailable" || code === "stt_server_off") {
          setHandsFree(false);
          setPref("handsfree", "off");
        }
        setToast(code === "mic_denied" ? "🎙 ✗" : t(code === "stt_server_off" ? "stt_server_off" : "stt_unavailable", lang));
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
    player.current?.jump(c.video_id, c.t);
    document.querySelector(".panel-video")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, []);

  const newChat = async () => { stopSpeech(); await db.messages.clear(); setMessages([{ role: "assistant", content: t("welcome", lang), lang, citations: [], ts: Date.now() }]); };
  const toggleVoice = () => { ensureUnlocked(); const v = !voiceOn; setVoiceOn(v); setPref("voice", v ? "on" : "off"); };

  useEffect(() => { if (toast) { const id = setTimeout(() => setToast(null), 5000); return () => clearTimeout(id); } }, [toast]);

  const engine = speaker.engineFor(lang);
  const topbar = (
    <div className="topbar">
      <span className="brand">📓 {t("appName", lang)}</span>
      <span className={`pill ${voiceOn ? "ok" : ""}`}><button onClick={toggleVoice}>{voiceOn ? t("voice_on", lang) : t("voice_off", lang)}</button></span>
      {voiceOn && engine === "none" && <span className="pill warn" title="No voice for this language on this device or server">{lang} voice ✗</span>}
      {voiceOn && engine === "server" && <span className="pill" title="Server voice (Piper)">🗣 piper</span>}
      <span className={`pill ${handsFree ? "ok" : ""}`}>
        <button onClick={toggleHandsFree} aria-pressed={handsFree}>
          {handsFree ? t("handsfree_on", lang) : t("handsfree_off", lang)}
        </button>
      </span>
      <span className="pill"><button onClick={newChat}>{t("new_chat", lang)}</button></span>
      <span className="pill">
        {(["ar", "en", "fr"] as Lang[]).map((l) => <button key={l} onClick={() => { setLang(l); setPref("lang", l); }} style={{ fontWeight: l === lang ? 700 : 400 }}>{l.toUpperCase()}</button>)}
      </span>
      <span className="pill">
        <button onClick={toggleTheme} aria-pressed={theme === "dark"} title={theme === "dark" ? "Switch to light" : "Switch to dark"}>
          {theme === "dark" ? "🌙" : "☀️"}
        </button>
      </span>
      {toast && <span className="pill danger" role="status">{toast}</span>}
    </div>
  );

  const avatar = (
    <section className="panel panel-avatar" aria-label={t("avatar_label", lang)}>
      {liveAvatarOn ? (
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
        <div className="avatar-wrap avatar-empty">
          <span className="placeholder-icon" aria-hidden="true">🧑‍🏫</span>
          <span>live avatar unavailable</span>
        </div>
      )}
    </section>
  );
  const video = (
    <section className="panel panel-video" aria-label="Lecture video" onClick={() => videoCollapsed && setVideoCollapsed(false)}>
      <VideoPlayer ref={player} token={token} videos={videos} lang={lang} activeId={activeVideo} onActiveChange={setActiveVideo} />
    </section>
  );

  return (
    <div className="app" lang={lang}>
      <div className={`top ${videoCollapsed ? "video-collapsed" : ""}`} >
        {avatar}
        {video}
      </div>
      <section className="panel panel-chat" aria-label="Chat">
        {topbar}
        <Chat lang={lang} messages={messages} live={live} streaming={streaming} speakingSentence={speakingSentence} interim={interim} listening={listening} sttMode={sttMode} onSend={send} onMic={mic} onStop={stopMic} onJump={jump} />
      </section>
      <section className="panel panel-curriculum" aria-label={t("curriculum", lang)}>
        <Curriculum videos={videos} activeId={activeVideo} lang={lang} onPick={setActiveVideo} />
      </section>
    </div>
  );
}
