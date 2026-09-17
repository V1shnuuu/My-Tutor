import assert from "node:assert/strict";
import { test } from "node:test";

import { messagesToMarkdown } from "./export.ts";
import type { StoredMessage } from "./store.ts";

const cite = (over: Partial<StoredMessage["citations"][0]> = {}) => ({
  ref: "C1", video_id: "v1", title: "Lecture 1", youtube_id: "abc123", url: null,
  t: 754, t_end: 780, snippet: "A peak is a local maximum.", score: 0.9, ...over,
});

test("a question and a cited answer become a heading and a linked timestamp", () => {
  const messages: StoredMessage[] = [
    { role: "user", content: "What is a peak?", lang: "en", citations: [], ts: 1 },
    { role: "assistant", content: "A peak is a local max [C1]", lang: "en", citations: [cite()], ts: 2 },
  ];
  const md = messagesToMarkdown(messages, "My Notes");
  assert.match(md, /^# My Notes/);
  assert.match(md, /## Q: What is a peak\?/);
  assert.match(md, /A peak is a local max \[C1\]/);
  assert.match(md, /Lecture 1.*12:34/);
  assert.match(md, /youtu\.be\/abc123\?t=754/);
  assert.match(md, /A peak is a local maximum\./);
});

test("an uncited answer produces no citation lines", () => {
  const messages: StoredMessage[] = [
    { role: "assistant", content: "General remark", lang: "en", citations: [], ts: 1 },
  ];
  const md = messagesToMarkdown(messages, "Notes");
  assert.ok(!md.includes("**"));
});

test("a local file video links by its url, not a youtube guess", () => {
  const messages: StoredMessage[] = [
    { role: "assistant", content: "Answer", lang: "en", citations: [cite({ youtube_id: null, url: "/media/lec01.mp4" })], ts: 1 },
  ];
  const md = messagesToMarkdown(messages, "Notes");
  assert.match(md, /\/media\/lec01\.mp4/);
  assert.ok(!md.includes("youtu.be"));
});

test("empty input produces just the title, not a crash", () => {
  const md = messagesToMarkdown([], "Notes");
  assert.equal(md.trim(), "# Notes");
});
