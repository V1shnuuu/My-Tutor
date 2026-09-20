import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import Chat, { type LiveState } from "./components/Chat";
import Curriculum from "./components/Curriculum";
import type { LiveAvatarHandle } from "./components/LiveAvatarPanel";
import Sessions from "./components/Sessions";
import SignIn from "./components/SignIn";
import VideoPlayer, { type PlayerHandle } from "./components/VideoPlayer";
import {
  ApiError, chat as chatApi, createConversation, getConversation, me,
  type AuthInfo, type Citation, type Lang, type UserProfile, type Video,
} from "./lib/api";
import { getAnonId, getSessionToken, setSessionToken, signOut, type User } from "./lib/auth";
import { amICourseAdmin, courseToVideos, getPublishedCourse } from "./lib/course";
import { downloadMarkdown, messagesToMarkdown } from "./lib/export";

import { dirOf, t } from "./lib/i18n";
import { SentenceSplitter, Speaker, isUnlocked, resumeAudio, unlockAudio } from "./lib/speech";
import { startListening, type ListenSession } from "./lib/stt";
import { EMPTY_TRANSCRIPT, type LiveTranscript } from "./lib/transcript";
import { db, getPref, setPref, type StoredMessage } from "./lib/store";

const speaker = new Speaker();

// Dynamic import, not a static one: students — the overwhelming majority of visitors — should
// never download the admin-authoring bundle. It only loads when someone actually hits /admin.
const AdminDashboard = lazy(() => import("./components/AdminDashboard"));

// LiveAvatarPanel pulls in livekit-client (a full WebRTC SDK, ~14MB unpacked) purely to render
// the HeyGen streaming avatar — already gated on liveAvatarOn (the server's own /me flag), so
// this only changes *when* it downloads: on demand, not in the initial bundle every visitor
// pays for even with the avatar off or unconfigured.
const LiveAvatarPanel = lazy(() => import("./components/LiveAvatarPanel"));

// What an anonymous visitor sends. The backend accepts it as "no user" and answers anyway —
// signing in buys saved history, not access.
const ANON_TOKEN = "anon";

type Theme = "light" | "dark";

export default function App() {
  // The session token when signed in, the anonymous stand-in otherwise. Everything below
  // passes this straight to the API layer, so most of the app never has to know which it is.
  const [sessionToken, setSession] = useState<string | null>(getSessionToken());
  const [user, setUser] = useState<UserProfile | null>(null);
  const [authInfo, setAuthInfo] = useState<AuthInfo | null>(null);
  // Sign-in is a screen you land on, not a wall: dismissed once, it stays dismissed.
  const [skippedSignIn, setSkippedSignIn] = useState(getPref("signin.skipped") === "1");
  // No router in this app (one extra screen didn't justify the dependency) — /admin is
  // recognised by its literal path, fixed for the page's lifetime.
  const [isAdminRoute] = useState(() => window.location.pathname === "/admin");
  const [isCourseAdmin, setIsCourseAdmin] = useState<boolean | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [sessionsKey, setSessionsKey] = useState(0);
  const token = sessionToken || ANON_TOKEN;
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
  // The live transcript, split into settled and in-flight halves so the composer can render
  // them differently. Replaced wholesale on every recogniser event — never appended to, which
  // is what keeps a replayed result from duplicating words.
  const [transcript, setTranscript] = useState<LiveTranscript>(EMPTY_TRANSCRIPT);
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

  // ---- bootstrap
  //
  // Every load starts a new chat, signed in or not: the welcome line, an empty transcript and
  // no conversation id. Past chats are reached deliberately, through the Sessions drawer —
  // which is also why nothing here reads the previous session's IndexedDB history any more.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const m = await me(token);
        if (!alive) return;
        setSttServer(m.stt); setLiveAvatarOn(!!m.avatar);
        setAuthInfo(m.auth); setUser(m.user);
        // A token the server no longer accepts (expired, or JWT_SECRET rotated) comes back
        // as user: null. Drop it rather than letting every later call 401 silently.
        if (sessionToken && !m.user) { setSessionToken(null); setSession(null); }
        speaker.configure(token, m.tts || {});
        // A published, admin-authored course takes over the syllabus/player the existing
        // components already render — reshaped into the same flat Video[] shape (see
        // course.ts's courseToVideos), so Curriculum.tsx and VideoPlayer.tsx needed no
        // changes to support it. No published course yet: an empty lessons list, not the raw
        // ingested corpus — that corpus can hold anything (sample/demo content, an old course
        // being replaced), and dumping it on students unscoped is exactly the leak this course
        // system exists to prevent (see published_video_ids in content.py).
        const course = await getPublishedCourse().catch(() => null);
        if (!alive) return;
        const finalVideos = course ? courseToVideos(course) : [];
        setVideos(finalVideos);
        // Prefer the first lesson that actually has a video over blindly picking index 0 —
        // an admin-authored course can start with unassigned lessons.
        setActiveVideo((a) => a || finalVideos.find((v) => !v.unassigned)?.id || finalVideos[0]?.id || null);
        setMessages([{ role: "assistant", content: t("welcome", lang), lang, citations: [], ts: Date.now() }]);
      } catch (e) {
        console.error("bootstrap: /me or /videos failed —", e instanceof ApiError ? { status: e.status, code: e.code } : e);
        setToast(t("offline", lang));
      }
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // ---- is this signed-in account actually an admin? Checked whenever signed in (not just on
  // /admin) since the Lessons panel's "⚙ Admin" entry button needs the answer too — purely for
  // UX either way (show the button / the dashboard vs. "not authorized"), every admin request
  // is still independently checked server-side, so this being skipped, stale, or spoofed
  // changes nothing about what the account can actually do.
  useEffect(() => {
    if (!sessionToken) { setIsCourseAdmin(null); return; }
    let alive = true;
    amICourseAdmin(sessionToken).then((r) => { if (alive) setIsCourseAdmin(r.is_admin); }).catch(() => { if (alive) setIsCourseAdmin(false); });
    return () => { alive = false; };
  }, [sessionToken]);

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
    // A signed-in chat gets its conversation the moment it has something to say — created
    // here rather than on load, so opening the app and never asking anything leaves no
    // empty row in the history list.
    let convId = conversationId;
    if (sessionToken && !convId) {
      try {
        convId = (await createConversation(sessionToken)).id;
        setConversationId(convId);
      } catch (e) {
        // Saving is a bonus, not a precondition: answer anyway, just without history.
        console.error("could not start a saved conversation —", e);
      }
    }
    const userMsg: StoredMessage = { role: "user", content: text, lang, citations: [], ts: Date.now() };
    userMsg.id = await db.messages.add(userMsg);
    const asst: StoredMessage = { role: "assistant", content: "", lang, citations: [], ts: Date.now() };
    setMessages((m) => [...m, userMsg, asst]);
    setStreaming(true); setLive({ stage: "retrieving" });
    // Signed in, the server reads this conversation's own history from the database and
    // ignores what we send; this local slice is what an anonymous session runs on.
    const history = messages.slice(-6).map((m) => ({ role: m.role, content: m.content }));
    const splitter = new SentenceSplitter((s) => speakSentence(s, asst.lang));
    let content = "";
    const update = (patch: Partial<StoredMessage>) => setMessages((m) => { const c = [...m]; c[c.length - 1] = { ...c[c.length - 1], ...patch }; return c; });
    try {
      // Spoken answers get a much shorter register; a muted session keeps the reading length.
      for await (const ev of chatApi(token, text, history, lang, voiceOn, undefined, convId, sessionToken ? undefined : getAnonId(), activeVideo)) {
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
      // The server just wrote this turn (and, on the first one, the conversation's title),
      // so the drawer's list is now stale.
      if (convId) setSessionsKey((k) => k + 1);
    }
  }, [token, sessionToken, conversationId, streaming, lang, messages, ensureUnlocked]);

  // ---- voice input
  const mic = useCallback(async () => {
    if (!token) return;
    ensureUnlocked();
    stopSpeech();
    setTranscript(EMPTY_TRANSCRIPT); setListening(true);
    session.current = await startListening({
      token, langHint: lang, serverAvailable: sttServer,
      // Preview only. This is the one callback that must never reach the question pipeline:
      // an interim result is a guess the recogniser is still free to revise.
      onInterim: setTranscript,
      tutorSpeaking: () => speakerStateRef.current === "speaking",
      // Barge-in: the student started talking over the answer, so drop it mid-sentence.
      onSpeechStart: () => { if (speakerStateRef.current === "speaking") stopSpeech(); },
      // A mode switch is the STT layer reporting that the server lane is done for this
      // session; drop serverAvailable too or every later attempt retries the same failure.
      onMode: (m) => { setSttMode(m); if (m === "browser") setSttServer(false); },
      // The only path into the pipeline, and only ever with finalised text.
      onFinal: (text, l) => {
        setListening(false); setTranscript(EMPTY_TRANSCRIPT); session.current = null;
        if (l) setLang(l);
        if (text.trim()) void send(text.trim());   // an empty utterance asks the model nothing
      },
      onError: (code) => {
        setListening(false); setTranscript(EMPTY_TRANSCRIPT); session.current = null;
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

  const newChat = useCallback(async () => {
    stopSpeech();
    await db.messages.clear();
    // Dropping the id is what makes it a new conversation: the next message creates a fresh
    // one server-side. The old one stays saved and reachable from the drawer.
    setConversationId(null);
    setMessages([{ role: "assistant", content: t("welcome", lang), lang, citations: [], ts: Date.now() }]);
  }, [lang, stopSpeech]);

  const exportNotes = useCallback(() => {
    const real = messages.filter((m) => m.role === "user" || m.citations.length > 0 || m.content.trim());
    if (real.length === 0) return;
    const md = messagesToMarkdown(real, t("appName", lang));
    downloadMarkdown(`study-notes-${new Date().toISOString().slice(0, 10)}.md`, md);
  }, [messages, lang]);

  /** Load a past conversation and continue it. */
  const resumeConversation = useCallback(async (id: string) => {
    if (!sessionToken || streaming) return;
    stopSpeech();
    try {
      const conv = await getConversation(sessionToken, id);
      await db.messages.clear();
      const restored: StoredMessage[] = conv.messages.map((m) => ({
        role: m.role,
        content: m.content,
        lang: (m.lang || lang) as Lang,
        citations: m.citations || [],
        source: m.source || undefined,
        ts: m.ts * 1000,
      }));
      await db.messages.bulkAdd(restored);
      setMessages(restored);
      setConversationId(id);
    } catch (e) {
      console.error("could not open that conversation —", e);
      setToast(t("offline", lang));
    }
  }, [sessionToken, streaming, lang, stopSpeech]);

  const onSignedIn = useCallback((tok: string, u: User) => {
    setSession(tok);
    setUser(u);
    setConversationId(null);
  }, []);

  const doSignOut = useCallback(async () => {
    stopSpeech();
    signOut();
    setSession(null);
    setUser(null);
    setConversationId(null);
    // The local transcript is the signed-out student's, not the next person's.
    await db.messages.clear();
    setMessages([{ role: "assistant", content: t("welcome", lang), lang, citations: [], ts: Date.now() }]);
    setSkippedSignIn(false);
    setPref("signin.skipped", "0");
  }, [lang, stopSpeech]);

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
      <span className="pill"><button onClick={exportNotes} title={t("export_notes", lang)}>⬇ {t("export_notes", lang)}</button></span>
      <span className="pill">
        {(["ar", "en", "fr"] as Lang[]).map((l) => <button key={l} onClick={() => { setLang(l); setPref("lang", l); }} style={{ fontWeight: l === lang ? 700 : 400 }}>{l.toUpperCase()}</button>)}
      </span>
      <span className="pill">
        <button onClick={toggleTheme} aria-pressed={theme === "dark"} title={theme === "dark" ? "Switch to light" : "Switch to dark"}>
          {theme === "dark" ? "🌙" : "☀️"}
        </button>
      </span>
      {user ? (
        <span className="pill account" title={user.email}>
          {user.picture
            ? <img className="avatar-chip" src={user.picture} alt="" referrerPolicy="no-referrer" />
            : <span className="avatar-chip placeholder" aria-hidden="true">{(user.name || user.email)[0]?.toUpperCase()}</span>}
          <button onClick={doSignOut}>{t("sign_out", lang)}</button>
        </span>
      ) : authInfo?.enabled ? (
        <span className="pill"><button onClick={() => { setSkippedSignIn(false); setPref("signin.skipped", "0"); }}>{t("signin_title", lang)}</button></span>
      ) : null}
      {toast && <span className="pill danger" role="status">{toast}</span>}
    </div>
  );

  const avatar = (
    <section className="panel panel-avatar" aria-label={t("avatar_label", lang)}>
      {liveAvatarOn ? (
        <Suspense fallback={null}>
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
        </Suspense>
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

  // The Admin Dashboard is a separate screen, not a separate app: /admin still needs a
  // Google-signed-in session (reusing the same sign-in flow below), just gated on being an
  // admin too — checked server-side on every admin request regardless of what this render
  // shows, so this branch is about UX, not the actual security boundary.
  if (isAdminRoute) {
    if (authInfo?.enabled && !sessionToken) {
      return <SignIn lang={lang} clientId={authInfo.google_client_id} onLang={setLang} onSignedIn={onSignedIn} onSkip={() => { window.location.href = "/"; }} />;
    }
    if (authInfo && !authInfo.enabled) {
      // No GOOGLE_CLIENT_ID on the server — there is no sign-in flow to offer, so say that
      // plainly instead of sitting on the loading state forever with no way forward.
      return (
        <div className="admin-shell">
          <p>{t("admin_signin_not_configured", lang)}</p>
          <a className="btn" href="/">{t("back_to_tutor", lang)}</a>
        </div>
      );
    }
    if (!sessionToken || isCourseAdmin === null) {
      return <div className="admin-shell"><p>{t("loading", lang)}</p></div>;
    }
    if (!isCourseAdmin) {
      return (
        <div className="admin-shell">
          <p>{t("admin_not_authorized", lang)}</p>
          <a className="btn" href="/">{t("back_to_tutor", lang)}</a>
        </div>
      );
    }
    return (
      <Suspense fallback={<div className="admin-shell"><p>{t("loading", lang)}</p></div>}>
        <AdminDashboard token={sessionToken} onExit={() => { window.location.href = "/"; }} />
      </Suspense>
    );
  }

  // Sign-in is offered once per browser and only when the server has a client id configured.
  // It is never a wall: "continue without an account" and every keyless install land here too.
  if (authInfo?.enabled && !sessionToken && !skippedSignIn) {
    return (
      <SignIn
        lang={lang}
        clientId={authInfo.google_client_id}
        onLang={setLang}
        onSignedIn={onSignedIn}
        onSkip={() => { setSkippedSignIn(true); setPref("signin.skipped", "1"); }}
      />
    );
  }

  return (
    <div className="app" lang={lang}>
      <div className={`top ${videoCollapsed ? "video-collapsed" : ""}`} >
        {avatar}
        {video}
      </div>
      <section className="panel panel-chat" aria-label="Chat">
        {topbar}
        <Chat lang={lang} messages={messages} live={live} streaming={streaming} speakingSentence={speakingSentence} transcript={transcript} listening={listening} sttMode={sttMode} onSend={send} onMic={mic} onStop={stopMic} onJump={jump} />
      </section>
      <section className="panel panel-curriculum" aria-label={t("curriculum", lang)}>
        <Curriculum videos={videos} activeId={activeVideo} lang={lang} onPick={setActiveVideo} onJump={jump} isCourseAdmin={!!isCourseAdmin} />
      </section>
      {sessionToken && (
        <Sessions
          token={sessionToken}
          lang={lang}
          refreshKey={sessionsKey}
          activeId={conversationId}
          onResume={resumeConversation}
        />
      )}
    </div>
  );
}
