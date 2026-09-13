/**
 * Speech output pipeline: streaming tokens → sentence chunks → spoken audio + viseme clock.
 *
 * Engine today: the browser's speechSynthesis (unlimited, free, zero infra, needs a user
 * gesture on iOS — see unlockAudio). Sentence chunking means the first audio starts as
 * soon as the first sentence has streamed, before generation finishes. The viseme timeline
 * is nominal and gets re-anchored on word-boundary events where the browser emits them.
 *
 * Upgrade path (docs §5.4): in-browser Piper via sherpa-onnx WASM behind the same interface.
 */
import type { Lang } from "./api";
import { buildTimeline, timeAtChar, visemeAt, type Timeline, type Viseme } from "./visemes";

export type SpeakerState = "idle" | "speaking";

interface Utt {
  text: string;
  lang: Lang;
  sentenceId: number;
}

const CPS: Record<Lang, number> = { en: 14, fr: 13, ar: 11 }; // chars per second, rough

let audioCtx: AudioContext | null = null;
let unlocked = false;

/** Call inside a user gesture (first tap on Send/Mic). Unlocks WebAudio + speechSynthesis on iOS. */
export function unlockAudio(): boolean {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
    if (audioCtx.state === "suspended") void audioCtx.resume();
    const buf = audioCtx.createBuffer(1, 1, 22050);
    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    src.start(0);
    if ("speechSynthesis" in window) {
      const u = new SpeechSynthesisUtterance("");
      u.volume = 0;
      speechSynthesis.speak(u);
    }
    unlocked = true;
  } catch {
    unlocked = false;
  }
  return unlocked;
}
export const isUnlocked = () => unlocked;
export const hasSpeech = () => typeof window !== "undefined" && "speechSynthesis" in window;

function pickVoice(lang: Lang): SpeechSynthesisVoice | null {
  const voices = speechSynthesis.getVoices();
  if (!voices.length) return null;
  const want = lang === "ar" ? ["ar-EG", "ar-SA", "ar"] : lang === "fr" ? ["fr-FR", "fr-CA", "fr"] : ["en-US", "en-GB", "en"];
  const score = (v: SpeechSynthesisVoice) => {
    const l = v.lang.replace("_", "-");
    let s = -1;
    want.forEach((w, i) => {
      if (l === w || (w.length === 2 && l.startsWith(w + "-"))) s = Math.max(s, 10 - i);
    });
    if (s < 0) return -1;
    if (/natural|online|neural|premium|enhanced/i.test(v.name)) s += 3;
    if (/google/i.test(v.name)) s += 2;
    if (v.localService) s += 0.5;
    return s;
  };
  let best: SpeechSynthesisVoice | null = null, bs = -1;
  for (const v of voices) {
    const s = score(v);
    if (s > bs) { best = v; bs = s; }
  }
  return best;
}

export function voiceAvailable(lang: Lang): boolean {
  if (!hasSpeech()) return false;
  return pickVoice(lang) !== null;
}

export function cleanForSpeech(text: string): string {
  return text
    .replace(/\[C\d+\]/g, " ")
    .replace(/\[\[[^\]]*\]\]/g, " ")
    .replace(/[*_`#>|]/g, " ")
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/[•▶]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export class Speaker {
  private queue: Utt[] = [];
  private current: Utt | null = null;
  private tl: Timeline | null = null;
  private anchorReal = 0;
  private anchorNominal = 0;
  private rate = 1; // nominal ms per real ms
  private state: SpeakerState = "idle";
  private enabled = true;
  private nextSentence = 0;
  onState: (s: SpeakerState) => void = () => {};
  onSentence: (id: number | null) => void = () => {};

  setEnabled(on: boolean) {
    this.enabled = on;
    if (!on) this.stop();
  }
  isEnabled() {
    return this.enabled;
  }

  /** Enqueue one sentence. Returns its id (for karaoke highlight). */
  enqueue(text: string, lang: Lang): number {
    const id = this.nextSentence++;
    const clean = cleanForSpeech(text);
    if (!this.enabled || !hasSpeech() || !clean) return id;
    // Mixed script: one utterance per script run so each gets the right voice.
    const runs = clean.match(/[؀-ۿݐ-ݿ][^A-Za-zÀ-ÿ]*|[^؀-ۿݐ-ݿ]+/g) || [clean];
    for (const run of runs) {
      const r = run.trim();
      if (!r) continue;
      const isAr = /[؀-ۿ]/.test(r);
      this.queue.push({ text: r, lang: isAr ? "ar" : lang === "ar" ? "en" : lang, sentenceId: id });
    }
    this.pump();
    return id;
  }

  stop() {
    this.queue = [];
    this.current = null;
    this.tl = null;
    if (hasSpeech()) speechSynthesis.cancel();
    this.setState("idle");
    this.onSentence(null);
  }

  /** Current viseme + amplitude for the avatar (poll from rAF). */
  current_viseme(): { v: Viseme; w: number } {
    if (!this.tl || this.state !== "speaking") return { v: "sil", w: 0 };
    const nominal = this.anchorNominal + (performance.now() - this.anchorReal) * this.rate;
    const ev = visemeAt(this.tl, nominal);
    return { v: ev.v, w: ev.weight };
  }

  private setState(s: SpeakerState) {
    if (s !== this.state) {
      this.state = s;
      this.onState(s);
    }
  }

  private pump() {
    if (this.current || !this.queue.length) return;
    const utt = this.queue.shift()!;
    this.current = utt;
    const u = new SpeechSynthesisUtterance(utt.text);
    const voice = pickVoice(utt.lang);
    if (voice) u.voice = voice;
    u.lang = voice?.lang || (utt.lang === "ar" ? "ar-EG" : utt.lang === "fr" ? "fr-FR" : "en-US");
    u.rate = 1.0;
    const tl = buildTimeline(utt.text, utt.lang);
    const estReal = (utt.text.length / CPS[utt.lang]) * 1000;
    const startClock = () => {
      this.tl = tl;
      this.anchorReal = performance.now();
      this.anchorNominal = 0;
      this.rate = estReal > 0 ? tl.duration / estReal : 1;
      this.setState("speaking");
      this.onSentence(utt.sentenceId);
    };
    u.onstart = startClock;
    u.onboundary = (e) => {
      if (!this.tl || e.name !== "word") return;
      const now = performance.now();
      const nominalC = timeAtChar(this.tl, e.charIndex);
      const dReal = now - this.anchorReal;
      if (dReal > 80) {
        const measured = (nominalC - this.anchorNominal) / dReal;
        if (measured > 0.3 && measured < 3) this.rate = this.rate * 0.5 + measured * 0.5;
      }
      this.anchorReal = now;
      this.anchorNominal = nominalC;
    };
    const finish = () => {
      if (this.current !== utt) return;
      this.current = null;
      this.tl = null;
      if (this.queue.length) this.pump();
      else {
        this.setState("idle");
        this.onSentence(null);
      }
    };
    u.onend = finish;
    u.onerror = finish;
    // Safari sometimes never fires onstart for the first utterance; start the clock on a timer as a fallback.
    setTimeout(() => {
      if (this.current === utt && this.state !== "speaking") startClock();
    }, 250);
    speechSynthesis.speak(u);
  }
}

/** Splits a token stream into sentence-sized pieces for the Speaker. */
export class SentenceSplitter {
  private buf = "";
  private emit: (sentence: string) => void;
  private minLen: number;
  constructor(emit: (sentence: string) => void, minLen = 14) {
    this.emit = emit;
    this.minLen = minLen;
  }
  push(token: string) {
    this.buf += token;
    // Split on sentence terminators (incl. Arabic ؟ and newline) once we have enough text.
    let m: RegExpMatchArray | null;
    while ((m = this.buf.match(/^([\s\S]*?[.!?؟。]+["»”)]?|\S[\s\S]*?\n)(\s|$)/)) && m[1].trim().length >= this.minLen) {
      const s = m[1].trim();
      this.buf = this.buf.slice(m[0].length);
      if (s) this.emit(s);
    }
    // Long run without punctuation: cut at a comma/clause after ~140 chars
    if (this.buf.length > 140) {
      const cut = this.buf.search(/[,،;:]\s/);
      if (cut > 40) {
        this.emit(this.buf.slice(0, cut + 1).trim());
        this.buf = this.buf.slice(cut + 2);
      }
    }
  }
  flush() {
    const s = this.buf.trim();
    this.buf = "";
    if (s) this.emit(s);
  }
}

if (hasSpeech()) {
  // Warm the voice list (Chrome loads it asynchronously).
  speechSynthesis.getVoices();
  speechSynthesis.addEventListener?.("voiceschanged", () => speechSynthesis.getVoices());
}
