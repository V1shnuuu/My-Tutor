/**
 * Text → viseme timeline, per language. Uses the Oculus 15-viseme set so the same
 * timeline drives the 2D face today and a 3D GLB (ARKit/Oculus morph targets) later.
 *
 * This is grapheme-driven (letter rules per language) with per-language timing tables.
 * Upgrade path documented in docs/ARCHITECTURE.md §6.2: espeak-ng phonemization → IPA.
 * Timeline times are *nominal*; the speech player re-anchors them to real speech using
 * word-boundary events, so drift stays within one sentence.
 */
import type { Lang } from "./api";

export type Viseme = "sil" | "PP" | "FF" | "TH" | "DD" | "kk" | "CH" | "SS" | "nn" | "RR" | "aa" | "E" | "I" | "O" | "U";

export interface VisemeEvent {
  t: number; // ms from utterance start (nominal)
  v: Viseme;
  charIndex: number; // index into the spoken text (for re-anchoring)
  weight: number; // 0..1 jaw/lip amplitude
}

export interface Timeline {
  events: VisemeEvent[];
  duration: number; // nominal ms
  text: string;
}

const C = 70; // consonant ms
const V = 110; // vowel ms
const VL = 170; // long vowel ms
const PAUSE = 220;

// ---------------------------------------------------------------- Arabic (Egyptian)
const AR: Record<string, [Viseme, number, number]> = {
  ب: ["PP", C, 1], م: ["PP", C, 0.9], پ: ["PP", C, 1],
  ف: ["FF", C, 0.9], ڤ: ["FF", C, 0.9],
  ث: ["TH", C, 0.7], ذ: ["TH", C, 0.7], ظ: ["TH", C, 0.8],
  ت: ["DD", C, 0.7], د: ["DD", C, 0.7], ط: ["DD", C, 0.9], ض: ["DD", C, 0.9], ل: ["DD", C, 0.6], ن: ["nn", C, 0.6],
  ك: ["kk", C, 0.7], ق: ["kk", C, 0.5], غ: ["kk", C, 0.6], خ: ["kk", C, 0.6], ج: ["kk", C, 0.7] /* Egyptian /g/ */,
  ش: ["CH", C, 0.8], چ: ["CH", C, 0.8],
  س: ["SS", C, 0.8], ص: ["SS", C, 0.9], ز: ["SS", C, 0.8],
  ر: ["RR", C, 0.7],
  ا: ["aa", VL, 1], أ: ["aa", V, 0.9], إ: ["I", V, 0.7], آ: ["aa", VL, 1], ى: ["aa", V, 0.8], ء: ["aa", C, 0.5],
  ع: ["aa", C + 20, 0.7], ح: ["aa", C + 20, 0.6], ه: ["aa", C, 0.5], ة: ["aa", V - 30, 0.6],
  ي: ["I", V, 0.7], و: ["U", V, 0.9],
  // tashkeel
  "َ": ["aa", V, 0.9], "ُ": ["U", V, 0.9], "ِ": ["I", V, 0.7], "ً": ["aa", V, 0.9], "ٌ": ["U", V, 0.9], "ٍ": ["I", V, 0.7],
};
const AR_VOWEL_LETTERS = new Set(["ا", "أ", "إ", "آ", "ى", "ي", "و", "ة", "َ", "ُ", "ِ", "ً", "ٌ", "ٍ"]);

function arabic(text: string, push: (v: Viseme, d: number, w: number, i: number) => void) {
  const chars = Array.from(text);
  for (let i = 0; i < chars.length; i++) {
    const ch = chars[i];
    if (ch === "ّ") { // shadda: repeat previous consonant
      const prev = chars[i - 1];
      if (prev && AR[prev]) push(AR[prev][0], C, AR[prev][2], i);
      continue;
    }
    if (ch === "ْ" || ch === "ـ") continue; // sukun, tatweel
    const m = AR[ch];
    if (!m) {
      generic(ch, push, i);
      continue;
    }
    push(m[0], m[1], m[2], i);
    // Undiacritised text: assume a short vowel after a consonant unless a vowel follows.
    const next = chars[i + 1];
    if (!AR_VOWEL_LETTERS.has(ch) && ch !== "ء" && next && !AR_VOWEL_LETTERS.has(next) && next !== "ْ" && next !== "ّ" && /[؀-ۿ]/.test(next)) {
      push("E", V - 50, 0.55, i);
    }
  }
}

// ---------------------------------------------------------------- Latin (EN / FR)
type Rule = [RegExp, Viseme[], number[], number[]];

const EN_RULES: Rule[] = [
  [/^(tch|ch|sh|zh|j)/, ["CH"], [C], [0.8]],
  [/^th/, ["TH"], [C], [0.7]],
  [/^(ph|f|v)/, ["FF"], [C], [0.9]],
  [/^(oo|ou|ew|ue)/, ["U"], [VL], [0.9]],
  [/^(ow)/, ["aa", "U"], [V, V - 40], [0.9, 0.8]],
  [/^(ee|ea|ie|ei|ey)/, ["I"], [VL], [0.7]],
  [/^(ai|ay|ey)/, ["E", "I"], [V, V - 50], [0.8, 0.6]],
  [/^(oa|oe|oh)/, ["O"], [VL], [0.9]],
  [/^(au|aw)/, ["O"], [VL], [0.9]],
  [/^(qu)/, ["kk", "U"], [C, C], [0.6, 0.7]],
  [/^(ck|k|c(?=[^eiy])|g|q|x|ng)/, ["kk"], [C], [0.7]],
  [/^(c|s|z)/, ["SS"], [C], [0.8]],
  [/^(b|p|m)/, ["PP"], [C], [1]],
  [/^(t|d|l)/, ["DD"], [C], [0.7]],
  [/^n/, ["nn"], [C], [0.6]],
  [/^r/, ["RR"], [C], [0.7]],
  [/^w/, ["U"], [C], [0.8]],
  [/^y/, ["I"], [C], [0.6]],
  [/^h/, ["aa"], [C - 20], [0.4]],
  [/^a/, ["aa"], [V], [1]],
  [/^e/, ["E"], [V], [0.8]],
  [/^i/, ["I"], [V], [0.7]],
  [/^o/, ["O"], [V], [0.9]],
  [/^u/, ["U"], [V], [0.8]],
];

const FR_RULES: Rule[] = [
  [/^(tch)/, ["CH"], [C], [0.8]],
  [/^(ch|j|g(?=[eiy]))/, ["CH"], [C], [0.8]],
  [/^(ph|f|v)/, ["FF"], [C], [0.9]],
  [/^(eau|au|o)/, ["O"], [V], [0.9]],
  [/^(ou)/, ["U"], [VL], [0.9]],
  [/^(oi)/, ["U", "aa"], [C, V], [0.8, 1]],
  [/^(eu|œu|œ)/, ["O"], [V], [0.7]],
  [/^(an|am|en|em)(?![aeiouyéèê])/, ["aa"], [VL], [0.9]],
  [/^(on|om)(?![aeiouyéèê])/, ["O"], [VL], [0.9]],
  [/^(in|im|ain|ein|un|um)(?![aeiouyéèê])/, ["E"], [VL], [0.8]],
  [/^(ai|ei|è|ê|é|e)/, ["E"], [V], [0.8]],
  [/^(qu|k|c(?=[^eiy])|g|x)/, ["kk"], [C], [0.7]],
  [/^(ç|c|s|z)/, ["SS"], [C], [0.8]],
  [/^(b|p|m)/, ["PP"], [C], [1]],
  [/^(t|d|l)/, ["DD"], [C], [0.7]],
  [/^(gn|n)/, ["nn"], [C], [0.6]],
  [/^r/, ["RR"], [C], [0.6]],
  [/^(w)/, ["U"], [C], [0.8]],
  [/^(y|i|î|ï)/, ["I"], [V], [0.7]],
  [/^h/, ["sil"], [20], [0]],
  [/^(a|à|â)/, ["aa"], [V], [1]],
  [/^(u|û|ù)/, ["U"], [V], [0.8]],
];

function latin(text: string, rules: Rule[], lang: Lang, push: (v: Viseme, d: number, w: number, i: number) => void) {
  const lower = text.toLowerCase();
  let i = 0;
  while (i < lower.length) {
    const rest = lower.slice(i);
    // French: silent final e / s / t / x / d before a boundary
    if (lang === "fr" && /^(e|es|s|t|x|d|ent)(?=[\s.,;:!?…]|$)/.test(rest) && i > 0 && /[a-zà-ÿ]/.test(lower[i - 1])) {
      const m = rest.match(/^(e|es|s|t|x|d|ent)/)!;
      i += m[0].length;
      continue;
    }
    let matched = false;
    for (const [re, vs, ds, ws] of rules) {
      const m = rest.match(re);
      if (m) {
        vs.forEach((v, k) => push(v, ds[k], ws[k], i));
        i += m[0].length;
        matched = true;
        break;
      }
    }
    if (!matched) {
      generic(lower[i], push, i);
      i += 1;
    }
  }
}

function generic(ch: string, push: (v: Viseme, d: number, w: number, i: number) => void, i: number) {
  if (/[.!?؟…]/.test(ch)) push("sil", PAUSE, 0, i);
  else if (/[,;:،]/.test(ch)) push("sil", PAUSE / 2, 0, i);
  else if (/\s/.test(ch)) push("sil", 45, 0, i);
  else if (/[0-9٠-٩]/.test(ch)) { push("DD", C, 0.6, i); push("E", V, 0.7, i); }
  // other symbols: silent
}

export function buildTimeline(text: string, lang: Lang): Timeline {
  const events: VisemeEvent[] = [];
  let t = 0;
  const push = (v: Viseme, d: number, w: number, i: number) => {
    events.push({ t, v, charIndex: i, weight: w });
    t += d;
  };
  // Mixed-script text: route each run to its own rule set.
  const runs = text.match(/[؀-ۿݐ-ݿ\sً-ْ]+|[^؀-ۿݐ-ݿ]+/g) || [text];
  let offset = 0;
  for (const run of runs) {
    const isArabic = /[؀-ۿ]/.test(run);
    const sub = (v: Viseme, d: number, w: number, i: number) => push(v, d, w, offset + i);
    if (isArabic) arabic(run, sub);
    else latin(run, lang === "fr" ? FR_RULES : EN_RULES, lang === "fr" ? "fr" : "en", sub);
    offset += run.length;
  }
  events.push({ t, v: "sil", charIndex: text.length, weight: 0 });
  return { events, duration: t, text };
}

/** Nominal time at which the timeline reaches a given character index. */
export function timeAtChar(tl: Timeline, charIndex: number): number {
  let lo = 0, hi = tl.events.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (tl.events[mid].charIndex < charIndex) lo = mid + 1;
    else hi = mid;
  }
  return tl.events[lo]?.t ?? 0;
}

/** Viseme active at nominal time t. */
export function visemeAt(tl: Timeline, t: number): VisemeEvent {
  const ev = tl.events;
  let lo = 0, hi = ev.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ev[mid].t <= t) lo = mid;
    else hi = mid - 1;
  }
  return ev[lo] ?? { t: 0, v: "sil", charIndex: 0, weight: 0 };
}
