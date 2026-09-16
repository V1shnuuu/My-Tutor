/**
 * Live transcript reconciliation.
 *
 * Deliberately free of DOM and Vite globals so it can be reasoned about — and tested — on its
 * own, away from microphones and network. The only job here is turning a speech recogniser's
 * result list into the two halves the UI renders.
 */

/** A live transcript in two halves. `committed` is what the recogniser has finalised and will
 *  not revise; `interim` is the words still in flight, which it may replace on the next event.
 *  Keeping them apart is what lets the UI render settled and unsettled text differently — and
 *  what stops the two being concatenated into duplicates. `text` is simply the two joined. */
export interface LiveTranscript {
  text: string;
  committed: string;
  interim: string;
}

export const EMPTY_TRANSCRIPT: LiveTranscript = { text: "", committed: "", interim: "" };

/** The shape `reconcile` needs from a SpeechRecognitionResultList: a list of results, each
 *  flagged final or not and carrying at least one alternative. Structural rather than the DOM
 *  type so a plain array works in a test — and so this module needs no DOM lib at all. */
export interface AlternativeLike { readonly transcript: string }
export interface ResultLike {
  readonly length: number;
  readonly [index: number]: {
    readonly isFinal: boolean;
    readonly [index: number]: AlternativeLike;
  };
}

/**
 * Rebuild the transcript from the whole result list, every event.
 *
 * The obvious alternative — appending each new final result to a running string — breaks the
 * moment an engine re-delivers a result index it has already finalised (Chrome does this after
 * a pause in continuous mode), turning "my name" into "my name my name". Recomputing from
 * index 0 is idempotent: replaying the same event twice cannot duplicate a word.
 *
 * Whitespace is collapsed because engines pad chunk boundaries inconsistently, but nothing
 * else about the words is touched — no punctuation is invented, no casing changed. What the
 * student said is what they see.
 */
export function reconcile(results: ResultLike): LiveTranscript {
  let committed = "";
  let interim = "";
  for (let i = 0; i < results.length; i++) {
    const r = results[i];
    if (!r) continue;
    const chunk = r[0]?.transcript ?? "";
    if (r.isFinal) committed += chunk;
    else interim += chunk;
  }
  committed = committed.replace(/\s+/g, " ").trim();
  interim = interim.replace(/\s+/g, " ").trim();
  const text = committed && interim ? `${committed} ${interim}` : committed || interim;
  return { text, committed, interim };
}
