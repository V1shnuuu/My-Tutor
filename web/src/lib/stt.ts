/**
 * Voice input, tiered (docs §5.4):
 *   1. Record (Opus) + client-side end-of-speech detection → core /stt (Groq Whisper, best Egyptian accuracy)
 *   2. Browser Web Speech API (unlimited, streams interim text) when the server budget is spent
 *   3. Neither available → caller shows "type your question"
 */
import { ApiError, stt as sttApi, type Lang } from "./api";
import { reconcile, type LiveTranscript } from "./transcript";

export type { LiveTranscript } from "./transcript";

export interface ListenOpts {
  token: string;
  langHint: Lang;
  serverAvailable: boolean;
  onInterim: (t: LiveTranscript) => void;
  onFinal: (text: string, lang: Lang | null) => void;
  onError: (code: "stt_unavailable" | "stt_server_off" | "mic_denied" | "stt_failed") => void;
  onLevel?: (rms: number) => void;
  onMode?: (mode: "server" | "browser") => void;
  /** Is the tutor talking right now? While it is, the mic stays open but the bar to count
   *  as speech is raised, so the tutor's own voice leaking past echo cancellation cannot
   *  interrupt it — only the student can. */
  tutorSpeaking?: () => boolean;
  /** Fired once, the moment the student is judged to be speaking. The caller uses this to
   *  cut the tutor off mid-sentence, which is what makes an interruption feel instant. */
  onSpeechStart?: () => void;
}
export interface ListenSession {
  stop: () => void; // stop and transcribe what was captured
  cancel: () => void;
}

interface SRInstance {
  lang: string; interimResults: boolean; continuous: boolean; maxAlternatives: number;
  onresult: ((e: SpeechRecognitionEvent) => void) | null; onerror: ((e: SpeechRecognitionErrorEvent) => void) | null; onend: (() => void) | null;
  start(): void; stop(): void; abort(): void;
}
type SR = new () => SRInstance;
const w = window as unknown as { SpeechRecognition?: SR; webkitSpeechRecognition?: SR };
const WebSpeech: SR | undefined = w.SpeechRecognition || w.webkitSpeechRecognition;

export const browserSttAvailable = () => !!WebSpeech;
const LOCALE: Record<Lang, string> = { ar: "ar-EG", en: "en-US", fr: "fr-FR" };

/** Verbose STT tracing. Off unless VITE_DEBUG_STT=1, so a student's words never land in a
 *  production console — the interim transcript is the sentence they are speaking out loud. */
const DEBUG = import.meta.env.VITE_DEBUG_STT === "1";
const trace = (...args: unknown[]) => { if (DEBUG) console.log("[stt]", ...args); };


export async function startListening(o: ListenOpts): Promise<ListenSession> {
  if (o.serverAvailable && !!navigator.mediaDevices && typeof MediaRecorder !== "undefined") {
    try {
      return await recordAndUpload(o);
    } catch (e) {
      // Firefox raises PermissionDeniedError instead of the standard NotAllowedError.
      if ((e as Error).name === "NotAllowedError" || (e as Error).name === "PermissionDeniedError") {
        o.onError("mic_denied");
        return noop;
      }
      // Anything else (no microphone hardware, an unsupported recording format, an
      // insecure-context SecurityError) falls through to browser recognition below —
      // the mic is unusable either way, so there is nothing more to try on this lane.
      console.error("startListening: record-and-upload lane failed —", (e as Error).name, (e as Error).message, e);
    }
  } else {
    console.error("startListening: record-and-upload lane skipped —", {
      serverAvailable: o.serverAvailable,
      hasMediaDevices: !!navigator.mediaDevices,
      hasMediaRecorder: typeof MediaRecorder !== "undefined",
      isSecureContext: window.isSecureContext,
    });
  }
  if (WebSpeech) return browserRecognize(o);
  // Nothing left. Brave and other privacy browsers ship no Web Speech API at all, so when
  // the server lane is off too there is no voice input — and the fixable half is the
  // server, not a browser setting the student will never find.
  console.error("startListening: no lane left — WebSpeech unavailable, serverAvailable =", o.serverAvailable);
  o.onError(o.serverAvailable ? "stt_unavailable" : "stt_server_off");
  return noop;
}
const noop: ListenSession = { stop: () => {}, cancel: () => {} };

// Widest-first: modern Chromium/Firefox take webm/opus, Safari (and old Android WebViews)
// take mp4/aac, and plain "audio/ogg" covers a few odd ones neither codec string matches.
// isTypeSupported itself is missing on some very old MediaRecorder polyfills, hence the guard.
const MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/aac", "audio/ogg;codecs=opus", "audio/ogg"];
function pickMime(): string {
  if (typeof MediaRecorder.isTypeSupported !== "function") return "";
  return MIME_CANDIDATES.find((m) => { try { return MediaRecorder.isTypeSupported(m); } catch { return false; } }) || "";
}

async function recordAndUpload(o: ListenOpts): Promise<ListenSession> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  // From here on any failure must release the mic before this function's caller falls
  // back to browser recognition — otherwise the mic stays lit for a lane that never runs.
  let rec: MediaRecorder;
  const mime = pickMime();
  try {
    rec = new MediaRecorder(stream, mime ? { mimeType: mime, audioBitsPerSecond: 32000 } : undefined);
  } catch (e) {
    stream.getTracks().forEach((t) => t.stop());
    throw e;
  }
  o.onMode?.("server");
  const chunks: Blob[] = [];
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);

  // Simple energy VAD: stop 1.1 s after speech ends (Arabic speakers pause longer than English).
  const AC = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AC) {
    stream.getTracks().forEach((t) => t.stop());
    throw new Error("no_audio_context");
  }
  const ctx = new AC();
  const src = ctx.createMediaStreamSource(stream);
  const an = ctx.createAnalyser();
  an.fftSize = 1024;
  src.connect(an);
  const data = new Float32Array(an.fftSize);
  let spokeAt = 0, startedAt = performance.now(), spoke = false, cancelled = false, stopped = false;
  const hang = o.langHint === "ar" ? 1300 : 1100;
  // Ordinary open-mic threshold, and the raised one used while the tutor is talking.
  // Browser echo cancellation removes most of the playback, and this covers the rest —
  // deliberately conservative, since a false barge-in cuts the tutor off mid-word.
  const SPEECH_RMS = 0.02, BARGE_RMS = 0.085;
  const tick = () => {
    if (stopped) return;
    an.getFloatTimeDomainData(data);
    let s = 0;
    for (let i = 0; i < data.length; i++) s += data[i] * data[i];
    const rms = Math.sqrt(s / data.length);
    o.onLevel?.(rms);
    const now = performance.now();
    const speaking = o.tutorSpeaking?.() ?? false;
    if (rms > (speaking ? BARGE_RMS : SPEECH_RMS)) {
      if (!spoke) o.onSpeechStart?.();   // cut the tutor off on the first syllable
      spoke = true;
      spokeAt = now;
    }
    // Waiting through the tutor's answer is not the student being silent, so neither the
    // give-up timer nor the hard cap may run while it is still talking.
    if (speaking) { startedAt = now; requestAnimationFrame(tick); return; }
    if ((spoke && now - spokeAt > hang) || now - startedAt > 30000) { stop(); return; }
    if (!spoke && now - startedAt > 8000) { cancel(); o.onFinal("", null); return; }
    requestAnimationFrame(tick);
  };

  // The server upload only yields text once transcription finishes, which reads as a dead
  // mic for the several seconds it takes. Web Speech (when present) streams interim words
  // as they're spoken, so run it purely as a live caption — its text never reaches onFinal,
  // the server's Whisper pass still produces the answer that actually gets sent.
  //
  // Chrome silently ends a `continuous: true` session after a long pause (a documented
  // quirk, not an error — `onend` fires with no matching `onerror`), which would otherwise
  // freeze the caption for the rest of the utterance while recording carries on underneath.
  // `armLiveCaption` restarts it whenever that happens and we are still actually recording.
  // Each restart is a new recognizer with its own result list starting over from nothing, so
  // `carry` holds what was already committed before the restart — without it, the caption
  // would visibly lose everything said before the pause.
  let liveCaption: SRInstance | null = null;
  let carry = "";
  const armLiveCaption = () => {
    if (!WebSpeech || stopped) return;
    try {
      const lc = new WebSpeech();
      lc.lang = LOCALE[o.langHint];
      lc.interimResults = true;
      lc.continuous = true;
      lc.maxAlternatives = 1;
      let lastCommitted = "";
      lc.onresult = (e: SpeechRecognitionEvent) => {
        const t = reconcile(e.results);
        lastCommitted = t.committed;
        const committed = carry && t.committed ? `${carry} ${t.committed}` : carry || t.committed;
        const text = committed && t.interim ? `${committed} ${t.interim}` : committed || t.interim;
        trace("interim:", t.interim, "| committed:", committed);
        o.onInterim({ text, committed, interim: t.interim });
      };
      // Best-effort only — the server transcript is what actually gets sent — but silent
      // failures here are indistinguishable from "the browser doesn't support this at all",
      // so surface the reason (network unreachable, mic already claimed exclusively by the
      // MediaRecorder stream above, no-speech, etc.) rather than swallowing it blind.
      // `network` in particular is what a privacy browser blocking Google's speech endpoint
      // looks like, and it is the one failure a student can actually act on.
      lc.onerror = (e: SpeechRecognitionErrorEvent) => {
        if (e.error !== "no-speech" && e.error !== "aborted") console.warn("live caption: recognition error —", e.error);
      };
      lc.onend = () => {
        trace("live caption ended");
        liveCaption = null;
        if (stopped) return;
        carry = carry && lastCommitted ? `${carry} ${lastCommitted}` : carry || lastCommitted;
        armLiveCaption();
      };
      lc.start();
      liveCaption = lc;
      trace("live caption (re)started —", LOCALE[o.langHint]);
    } catch (e) { console.warn("live caption: failed to start —", e); liveCaption = null; }
  };
  if (WebSpeech) armLiveCaption();
  else trace("no WebSpeech in this browser — interim text disabled on the server lane");

  const cleanup = () => {
    stopped = true;
    stream.getTracks().forEach((t) => t.stop());
    void ctx.close();
    try { liveCaption?.abort(); } catch { /* */ }
  };
  const stop = () => {
    if (stopped) return;
    cleanup();
    if (rec.state !== "inactive") rec.stop();
  };
  const cancel = () => { cancelled = true; stop(); };

  rec.onstop = async () => {
    if (cancelled) return;
    const blob = new Blob(chunks, { type: mime || "audio/webm" });
    if (blob.size < 2000) { o.onFinal("", null); return; }
    try {
      const r = await sttApi(o.token, blob, o.langHint);
      const lang = (r.language || "").slice(0, 2) as Lang;
      o.onFinal(r.text, ["ar", "en", "fr"].includes(lang) ? lang : null);
    } catch (e) {
      // Whatever went wrong up there — budget spent, rate limited, the local model missing,
      // their outage — server STT is not answering for this student right now. This
      // utterance is lost either way (the audio can't be re-decoded here), so the win is
      // telling the caller to stop routing to the server, which is what makes the next
      // press of the mic work instead of failing the same way.
      if (WebSpeech) o.onMode?.("browser");
      o.onError(e instanceof ApiError && e.code === "stt_budget" ? "stt_unavailable" : "stt_failed");
    }
  };
  rec.start(250);
  requestAnimationFrame(tick);
  return { stop, cancel };
}

function browserRecognize(o: ListenOpts): ListenSession {
  const rec = new WebSpeech!();
  rec.lang = LOCALE[o.langHint];
  rec.interimResults = true;
  rec.continuous = false;
  rec.maxAlternatives = 1;
  o.onMode?.("browser");
  // On this lane the committed half is not just a preview — it is the question that gets
  // sent, so a duplicated word here would reach the model. `reconcile` recomputes rather
  // than accumulates precisely so a replayed result cannot corrupt it.
  let finalText = "";
  rec.onresult = (e: SpeechRecognitionEvent) => {
    const t = reconcile(e.results);
    finalText = t.committed;
    trace("interim:", t.interim, "| committed:", t.committed);
    o.onInterim(t);
  };
  rec.onerror = (e: SpeechRecognitionErrorEvent) => {
    if (e.error === "not-allowed") o.onError("mic_denied");
    else if (e.error !== "aborted" && e.error !== "no-speech") o.onError("stt_failed");
    else o.onFinal("", null);
  };
  rec.onend = () => o.onFinal(finalText.trim(), finalText ? o.langHint : null);
  try {
    rec.start();
  } catch {
    o.onError("stt_failed");
  }
  return { stop: () => rec.stop(), cancel: () => rec.abort() };
}
