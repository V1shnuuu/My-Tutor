import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Avatar, { type AvatarState } from "./components/Avatar";
import Chat, { type LiveState } from "./components/Chat";
import Login from "./components/Login";
import VideoPlayer, { type PlayerHandle } from "./components/VideoPlayer";
import { ApiError, chat as chatApi, listVideos, me, type Citation, type Lang, type Video } from "./lib/api";
import { dirOf, t } from "./lib/i18n";
import { SentenceSplitter, Speaker, hasSpeech, isUnlocked, unlockAudio, voiceAvailable } from "./lib/speech";
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
  const player = useRef<PlayerHandle>(null);
  const session = useRef<ListenSession | null>(null);
  const sentences = useRef<Map<number, string>>(new Map());

  // ---- bootstrap after login
  useEffect(() => {
    if (!token) return;
    let alive = true;
    (async () => {
      try {
        const m = await me(token);
        if (!alive) return;
        setBudget(m.budget); setSttServer(m.stt);
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
  useEffect(() => {
    speaker.onState = setSpeakerState;
    speaker.onSentence = (id) => setSpeakingSentence(id == null ? null : sentences.current.get(id) ?? null);
    speaker.setEnabled(voiceOn);
  }, [voiceOn]);
  useEffect(() => {
    const onVis = () => { if (document.hidden) speaker.stop(); };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);
  useEffect(() => { document.documentElement.dir = dirOf(lang); document.documentElement.lang = lang; }, [lang]);

  const ensureUnlocked = useCallback(() => {
    if (!isUnlocked()) setUnlocked(unlockAudio());
    else setUnlocked(true);
  }, []);

  const getViseme = useCallback(() => speaker.current_viseme(), []);

  // ---- send a message and stream the answer
  const send = useCallback(async (text: string) => {
    if (!token || streaming) return;
    ensureUnlocked();
    speaker.stop();
    sentences.current.clear();
    const userMsg: StoredMessage = { role: "user", content: text, lang, citations: [], ts: Date.now() };
    userMsg.id = await db.messages.add(userMsg);
    const asst: StoredMessage = { role: "assistant", content: "", lang, citations: [], ts: Date.now() };
    setMessages((m) => [...m, userMsg, asst]);
    setStreaming(true); setLive({ stage: "retrieving" });
    const history = messages.slice(-6).map((m) => ({ role: m.role, content: m.content }));
    const splitter = new SentenceSplitter((s) => {
      const id = speaker.enqueue(s, asst.lang);
      sentences.current.set(id, s);
    });
    let content = "";
    const update = (patch: Partial<StoredMessage>) => setMessages((m) => { const c = [...m]; c[c.length - 1] = { ...c[c.length - 1], ...patch }; return c; });
    try {
      for await (const ev of chatApi(token, text, history, lang)) {
        if (ev.type === "meta") {
          asst.lang = ev.lang; setLang(ev.lang); setPref("lang", ev.lang); setBudget(ev.budget); update({ lang: ev.lang });
        } else if (ev.type === "status") {
          setLive({ stage: ev.stage, text: ev.text, eta: ev.eta });
        } else if (ev.type === "citations") {
          asst.citations = ev.items; update({ citations: ev.items });
        } else if (ev.type === "token") {
          content += ev.text; update({ content }); splitter.push(ev.text);
        } else if (ev.type === "replace") {
          speaker.stop(); content = ev.text; update({ content });
          for (const s of ev.text.split(/\n+/)) if (s.trim()) sentences.current.set(speaker.enqueue(s, asst.lang), s.trim());
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
    speaker.stop();
    setInterim(""); setListening(true);
    session.current = await startListening({
      token, langHint: lang, serverAvailable: sttServer,
      onInterim: setInterim,
      onMode: setSttMode,
      onFinal: (text, l) => {
        setListening(false); setInterim(""); session.current = null;
        if (l) setLang(l);
        if (text.trim()) void send(text.trim());
      },
      onError: (code) => {
        setListening(false); setInterim(""); session.current = null;
        setToast(code === "stt_unavailable" ? t("stt_unavailable", lang) : code === "mic_denied" ? "🎙 ✗" : t("stt_unavailable", lang));
      },
    });
  }, [token, lang, sttServer, send, ensureUnlocked]);
  const stopMic = useCallback(() => session.current?.stop(), []);

  const jump = useCallback((c: Citation) => {
    speaker.stop();
    setVideoCollapsed(false);
    player.current?.jump(c.video_id, c.t);
    document.querySelector(".panel-video")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, []);

  const newChat = async () => { speaker.stop(); await db.messages.clear(); setMessages([{ role: "assistant", content: t("welcome", lang), lang, citations: [], ts: Date.now() }]); };
  const toggleVoice = () => { ensureUnlocked(); const v = !voiceOn; setVoiceOn(v); setPref("voice", v ? "on" : "off"); };

  useEffect(() => { if (toast) { const id = setTimeout(() => setToast(null), 5000); return () => clearTimeout(id); } }, [toast]);

  const avatarState: AvatarState = useMemo(() => {
    if (listening) return "listening";
    if (speakerState === "speaking") return "speaking";
    if (streaming) return "thinking";
    if (!voiceOn || (!unlocked && !isUnlocked()) || !hasSpeech()) return "muted";
    return "idle";
  }, [listening, speakerState, streaming, voiceOn, unlocked]);

  if (!token) return <Login lang={lang} onLang={setLang} onToken={(tk) => { setToken(tk); setTok(tk); }} />;

  const arVoice = voiceAvailable("ar");
  const topbar = (
    <div className="topbar">
      <span className="brand">📓 {t("appName", lang)}</span>
      {budget && <span className="pill">{t("budget", lang)}: {budget.used}/{budget.cap}</span>}
      <span className={`pill ${voiceOn ? "ok" : ""}`}><button onClick={toggleVoice}>{voiceOn ? t("voice_on", lang) : t("voice_off", lang)}</button></span>
      {lang === "ar" && voiceOn && !arVoice && <span className="pill warn" title="No Arabic voice installed on this device">ar voice ✗</span>}
      <span className="pill"><button onClick={newChat}>{t("new_chat", lang)}</button></span>
      <span className="pill">
        {(["ar", "en", "fr"] as Lang[]).map((l) => <button key={l} onClick={() => { setLang(l); setPref("lang", l); }} style={{ fontWeight: l === lang ? 700 : 400 }}>{l.toUpperCase()}</button>)}
      </span>
      {toast && <span className="pill danger" role="status">{toast}</span>}
    </div>
  );

  const avatar = (
    <section className="panel panel-avatar" aria-label={t("avatar_label", lang)}>
      <Avatar state={avatarState} getViseme={getViseme} label={t("avatar_label", lang)} stateLabel={t(`state_${avatarState}` as const, lang)} muteLabel={t("enable_voice", lang)} onUnmute={() => { ensureUnlocked(); if (!voiceOn) toggleVoice(); }} />
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
    </div>
  );
}
