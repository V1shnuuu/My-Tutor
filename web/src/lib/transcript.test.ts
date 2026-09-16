/**
 * Tests for live transcript reconciliation.
 *
 * No test runner is installed in web/, and adding one is its own decision, so this runs
 * directly on Node's TypeScript support:
 *
 *   node --test src/lib/transcript.test.ts        (from web/)
 *
 * The duplicate cases are the point. Everything else about the live preview is cosmetic; a
 * duplicated word in the committed half is a wrong question reaching the model on the
 * browser-only STT lane, where committed text is what actually gets sent.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { reconcile, type ResultLike } from "./transcript.ts";

/** Build a result list the way a speech engine delivers one. */
function results(...parts: Array<[string, boolean]>): ResultLike {
  const arr = parts.map(([transcript, isFinal]) => ({ isFinal, 0: { transcript } }));
  return arr as unknown as ResultLike;
}

test("an empty result list is an empty transcript", () => {
  assert.deepEqual(reconcile(results()), { text: "", committed: "", interim: "" });
});

test("interim-only speech shows as pending, nothing committed", () => {
  const t = reconcile(results(["Hello", false]));
  assert.equal(t.committed, "");
  assert.equal(t.interim, "Hello");
  assert.equal(t.text, "Hello");
});

test("progressive interim updates replace, never accumulate", () => {
  // What the UI shows as the student keeps talking — each event is the whole list again.
  assert.equal(reconcile(results(["Hello", false])).text, "Hello");
  assert.equal(reconcile(results(["Hello my", false])).text, "Hello my");
  assert.equal(reconcile(results(["Hello my name", false])).text, "Hello my name");
  assert.equal(reconcile(results(["Hello my name is Vishnu", false])).text, "Hello my name is Vishnu");
});

test("finalised text moves into the committed half", () => {
  const t = reconcile(results(["Hello my name is Vishnu", true]));
  assert.equal(t.committed, "Hello my name is Vishnu");
  assert.equal(t.interim, "");
  assert.equal(t.text, "Hello my name is Vishnu");
});

test("committed and interim join with exactly one space", () => {
  const t = reconcile(results(["Hello my name", true], [" is Vishnu", false]));
  assert.equal(t.committed, "Hello my name");
  assert.equal(t.interim, "is Vishnu");
  assert.equal(t.text, "Hello my name is Vishnu");
});

test("re-delivering an already-final result cannot duplicate it", () => {
  // The regression this function exists for: an accumulate-on-final implementation turns the
  // second call into "Hello my name Hello my name".
  const list = results(["Hello my name", true]);
  const first = reconcile(list);
  const second = reconcile(list);
  assert.equal(first.text, "Hello my name");
  assert.deepEqual(first, second, "reconcile must be idempotent");
});

test("a repeated phrase the student actually said is preserved", () => {
  // Idempotence must not become de-duplication: saying a word twice is legitimate.
  const t = reconcile(results(["very very good", true]));
  assert.equal(t.text, "very very good");
});

test("multi-segment continuous speech concatenates in order", () => {
  const t = reconcile(results(
    ["What is ", true],
    ["artificial intelligence", true],
    [" and how", false],
  ));
  assert.equal(t.committed, "What is artificial intelligence");
  assert.equal(t.interim, "and how");
  assert.equal(t.text, "What is artificial intelligence and how");
});

test("ragged whitespace from chunk boundaries collapses", () => {
  const t = reconcile(results(["What   is", true], ["\n\n  blockchain ", false]));
  assert.equal(t.text, "What is blockchain");
});

test("nothing invents punctuation or changes the words", () => {
  const t = reconcile(results(["what is blockchain", true]));
  assert.equal(t.text, "what is blockchain", "no capitalisation, no question mark");
});

test("Arabic and French text survive intact", () => {
  const ar = reconcile(results(["إزاي الخوارزمية دي بتشتغل", true]));
  assert.equal(ar.text, "إزاي الخوارزمية دي بتشتغل");
  const fr = reconcile(results(["qu'est-ce que l'intelligence", true], [" artificielle", false]));
  assert.equal(fr.text, "qu'est-ce que l'intelligence artificielle");
});

test("a malformed or empty alternative does not crash or inject undefined", () => {
  const broken = [
    { isFinal: true, 0: { transcript: "ok" } },
    { isFinal: false },                       // no alternative at all
    undefined,                                 // a hole in the list
  ] as unknown as ResultLike;
  const t = reconcile(broken);
  assert.equal(t.text, "ok");
  assert.ok(!t.text.includes("undefined"));
});
