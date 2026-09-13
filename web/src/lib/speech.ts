/**
 * Speech output pipeline: streaming tokens → sentence chunks → spoken audio + viseme clock.
 *
 * Two engines behind one queue, chosen per sentence (per script run for mixed text):
 *   - server Piper (/tts, WAV) — used when the core has a voice for the language and the device
 *     has none, and always for Arabic (consistent voice; OS Arabic voices are rare). Played on a
 *     single AudioContext with sample-accurate scheduling, so visemes follow the audio clock.
 *   - browser speechSynthesis — unlimited, zero infra; viseme clock re-anchored on word boundaries.
 * Sentence chunking means the first audio starts as soon as the first sentence has streamed.
 */
import { API, type Lang } from "./api";
import { buildTimeline, timeAtChar, visemeAt, type Timeline, type Viseme } from "./visemes";

export type SpeakerState = "idle" | "speaking";

interface Utt {
  text: string;
  lang: Lang;
  sentenceId: number;
  engine: "server" | "browser";
  ready?: Promise<AudioBuffer | null>;
}

const CPS: Record<Lang, number> = { en: 14, fr: 13, ar: 11 }; // chars per second, rough (browser engine only)

let audioCtx: AudioContext | null = null;
let unlocked = false;

function ctx(): AudioContext {
  if (!audioCtx) audioCtx = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
  return audioCtx;
}

/** Call inside a user gesture (first tap on Send/Mic). Unlocks WebAudio + speechSynthesis on iOS. */
export function unlockAudio(): boolean {
  try {
    const c = ctx();
    if (c.state === "suspended") void c.resume();
    const src = c.createBufferSource();
    src.buffer = c.createBuffer(1, 1, 22050);
    src.connect(c.destination);
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
export function resumeAudio() {
  if (audioCtx && audioCtx.state === "suspended") void audioCtx.resume();
}

function pickVoice(lang: Lang): SpeechSynthesisVoice | null {
  if (!hasSpeech()) return null;
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

export const voiceAvailable = (lang: Lang) => pickVoice(lang) !== null;

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
  private running = false;
  private gen = 0; // bumped on stop() so stale async work is ignored
  private tl: Timeline | null = null;
  private clock: "perf" | "audio" = "perf";
  private anchorReal = 0;      // performance.now() ms, or AudioContext seconds
  private anchorNominal = 0;   // nominal ms in the timeline
  private rate = 1;            // nominal ms per real ms
  private state: SpeakerState = "idle";
  private enabled = true;
  private nextSentence = 0;
  private serverVoices: Record<Lang, boolean> = { ar: false, en: false, fr: false };
  private token: string | null = null;
  onState: (s: SpeakerState) => void = () => {};
  onSentence: (id: number | null) => void = () => {};

  configure(token: string | null, serverVoices: Partial<Record<Lang, boolean>>) {
    this.token = token;
    this.serverVoices = { ...this.serverVoices, ...serverVoices };
  }
  setEnabled(on: boolean) {
    this.enabled = on;
    if (!on) this.stop();
  }
  isEnabled() {
    return this.enabled;
  }
  /** Which engine a language will use on this device (for UI hints). */
  engineFor(lang: Lang): "server" | "browser" | "none" {
    if (this.serverVoices[lang] && this.token && (lang === "ar" || !pickVoice(lang))) return "server";
    if (pickVoice(lang)) return "browser";
    return this.serverVoices[lang] && this.token ? "server" : "none";
  }

  /** Enqueue one sentence. Returns its id (for karaoke highlight). */
  enqueue(text: string, lang: Lang): number {
    const id = this.nextSentence++;
    const clean = cleanForSpeech(text);
    if (!this.enabled || !clean) return id;
    // Mixed script: one utterance per script run so each gets the right voice.
    const runs = clean.match(/[؀-ۿݐ-ݿ][^A-Za-zÀ-ÿ]*|[^؀-ۿݐ-ݿ]+/g) || [clean];
    for (const run of runs) {
      const r = run.trim();
      if (!r) continue;
      const isAr = /[؀-ۿ]/.test(r);
      const l: Lang = isAr ? "ar" : lang === "ar" ? "en" : lang;
      const engine = this.engineFor(l);
      if (engine === "none") continue;
      const utt: Utt = { text: r, lang: l, sentenceId: id, engine };
      if (engine === "server") utt.ready = this.fetchAudio(utt, this.gen); // prefetch immediately
      this.queue.push(utt);
    }
    void this.run();
    return id;
  }

  stop() {
    this.gen++;
    this.queue = [];
    this.tl = null;
    if (hasSpeech()) speechSynthesis.cancel();
    this.currentSource?.stop();
    this.currentSource = null;
    this.running = false;
    this.setState("idle");
    this.onSentence(null);
  }

  /** Current viseme + amplitude for the avatar (poll from rAF). */
  current_viseme(): { v: Viseme; w: number } {
    if (!this.tl || this.state !== "speaking") return { v: "sil", w: 0 };
    const now = this.clock === "audio" ? ctx().currentTime * 1000 : performance.now();
    const nominal = this.anchorNominal + (now - this.anchorReal) * this.rate;
    const ev = visemeAt(this.tl, nominal);
    return { v: ev.v, w: ev.weight };
  }

  // ---- internals -------------------------------------------------------------
  private currentSource: AudioBufferSourceNode | null = null;

  private setState(s: SpeakerState) {
    if (s !== this.state) {
      this.state = s;
      this.onState(s);
    }
  }

  private async fetchAudio(utt: Utt, gen: number): Promise<AudioBuffer | null> {
    try {
      const r = await fetch(`${API}/tts`, { method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${this.token}` }, body: JSON.stringify({ text: utt.text, lang: utt.lang }) });
      if (!r.ok || gen !== this.gen) return null;
      const buf = await r.arrayBuffer();
      return await ctx().decodeAudioData(buf);
    } catch {
      return null;
    }
  }

  private async run() {
    if (this.running) return;
    this.running = true;
    const gen = this.gen;
    while (this.queue.length && gen === this.gen) {
      const utt = this.queue.shift()!;
      // keep one server sentence prefetching ahead of playback
      const next = this.queue.find((u) => u.engine === "server" && !u.ready);
      if (next) next.ready = this.fetchAudio(next, gen);
      if (utt.engine === "server") {
        const buf = await utt.ready!;
        if (gen !== this.gen) break;
        if (buf) await this.playBuffer(utt, buf, gen);
        else if (pickVoice(utt.lang)) await this.speakBrowser(utt, gen); // server hiccup → OS voice
      } else {
        await this.speakBrowser(utt, gen);
      }
    }
    if (gen === this.gen) {
      this.running = false;
      this.tl = null;
      this.setState("idle");
      this.onSentence(null);
    }
  }

  private playBuffer(utt: Utt, buf: AudioBuffer, gen: number): Promise<void> {
    return new Promise((resolve) => {
      const c = ctx();
      if (c.state === "suspended") void c.resume();
      const src = c.createBufferSource();
      src.buffer = buf;
      src.connect(c.destination);
      const tl = buildTimeline(utt.text, utt.lang);
      const startAt = c.currentTime + 0.03;
      // Exact scaling: the timeline is stretched to the real audio duration, so drift is bounded per sentence.
      this.tl = tl;
      this.clock = "audio";
      this.anchorReal = startAt * 1000;
      this.anchorNominal = 0;
      this.rate = buf.duration > 0 ? tl.duration / (buf.duration * 1000) : 1;
      this.setState("speaking");
      this.onSentence(utt.sentenceId);
      src.onended = () => { if (this.currentSource === src) this.currentSource = null; resolve(); };
      this.currentSource = src;
      src.start(startAt);
      if (gen !== this.gen) resolve();
    });
  }

  private speakBrowser(utt: Utt, gen: number): Promise<void> {
    return new Promise((resolve) => {
      if (!hasSpeech()) return resolve();
      const u = new SpeechSynthesisUtterance(utt.text);
      const voice = pickVoice(utt.lang);
      if (voice) u.voice = voice;
      u.lang = voice?.lang || (utt.lang === "ar" ? "ar-EG" : utt.lang === "fr" ? "fr-FR" : "en-US");
      const tl = buildTimeline(utt.text, utt.lang);
      const estReal = (utt.text.length / CPS[utt.lang]) * 1000;
      let started = false;
      const startClock = () => {
        if (started || gen !== this.gen) return;
        started = true;
        this.tl = tl;
        this.clock = "perf";
        this.anchorReal = performance.now();
        this.anchorNominal = 0;
        this.rate = estReal > 0 ? tl.duration / estReal : 1;
        this.setState("speaking");
        this.onSentence(utt.sentenceId);
      };
      u.onstart = startClock;
      u.onboundary = (e) => {
        if (!this.tl || e.name !== "word" || gen !== this.gen) return;
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
      u.onend = () => resolve();
      u.onerror = () => resolve();
      // Safari sometimes never fires onstart for the first utterance.
      setTimeout(startClock, 250);
      speechSynthesis.speak(u);
    });
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
    let m: RegExpMatchArray | null;
    while ((m = this.buf.match(/^([\s\S]*?[.!?؟。]+["»”)]?|\S[\s\S]*?\n)(\s|$)/)) && m[1].trim().length >= this.minLen) {
      const s = m[1].trim();
      this.buf = this.buf.slice(m[0].length);
      if (s) this.emit(s);
    }
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
  speechSynthesis.getVoices();
  speechSynthesis.addEventListener?.("voiceschanged", () => speechSynthesis.getVoices());
}
