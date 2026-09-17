import assert from "node:assert/strict";
import { test } from "node:test";

import { parseSyllabus } from "./syllabus.ts";

test("the master prompt's own example format", () => {
  const text = `
WEEK 1
1. Algorithmic Thinking, Peak Finding
2. Models of Computation, Document Distance
3. Asymptotic Complexity, Peak Finding
4. Python Cost Model, Document Distance

WEEK 2
1. Insertion Sort, Merge Sort
2. Heaps and Heap Sort
3. Document Distance, Insertion and Merge Sort
`;
  const parsed = parseSyllabus(text);
  assert.equal(parsed.length, 2);
  assert.equal(parsed[0].title, "Week 1");
  assert.deepEqual(parsed[0].lessons, [
    "Algorithmic Thinking, Peak Finding",
    "Models of Computation, Document Distance",
    "Asymptotic Complexity, Peak Finding",
    "Python Cost Model, Document Distance",
  ]);
  assert.equal(parsed[1].title, "Week 2");
  assert.equal(parsed[1].lessons.length, 3);
});

test("lowercase 'week', mixed casing", () => {
  const parsed = parseSyllabus("week 1\n1. Intro\nWeek 2\n1. More");
  assert.deepEqual(parsed.map((w) => w.title), ["Week 1", "Week 2"]);
});

test("a week header with trailing text keeps it in the title", () => {
  const parsed = parseSyllabus("Week 1: Foundations\n1. Intro");
  assert.equal(parsed[0].title, "Week 1: Foundations");
});

test("week header separators - colon, dash, en-dash", () => {
  for (const sep of [":", "-", "–", "."]) {
    const parsed = parseSyllabus(`Week 3 ${sep} Sorting\n1. Merge sort`);
    assert.equal(parsed[0].title, "Week 3: Sorting", `separator ${JSON.stringify(sep)}`);
  }
});

test("bullet points instead of numbers", () => {
  const parsed = parseSyllabus("Week 1\n- First lesson\n* Second lesson\n• Third lesson");
  assert.deepEqual(parsed[0].lessons, ["First lesson", "Second lesson", "Third lesson"]);
});

test("bare lines with no numbering or bullets still become lessons", () => {
  const parsed = parseSyllabus("Week 1\nJust a plain line\nAnother plain line");
  assert.deepEqual(parsed[0].lessons, ["Just a plain line", "Another plain line"]);
});

test("content before any Week header starts an implicit first week", () => {
  const parsed = parseSyllabus("1. Orphan lesson\nWeek 1\n1. Real lesson");
  assert.equal(parsed.length, 2);
  assert.equal(parsed[0].title, "Week 1");
  assert.deepEqual(parsed[0].lessons, ["Orphan lesson"]);
});

test("blank lines and extra whitespace are ignored", () => {
  const parsed = parseSyllabus("\n\nWeek 1\n\n   1. Lesson one   \n\n\n2. Lesson two\n\n");
  assert.deepEqual(parsed[0].lessons, ["Lesson one", "Lesson two"]);
});

test("a week header with no lessons under it is dropped, not saved empty", () => {
  const parsed = parseSyllabus("Week 1\n1. Lesson\nWeek 2\nWeek 3\n1. Lesson");
  assert.deepEqual(parsed.map((w) => w.title), ["Week 1", "Week 3"]);
});

test("empty input parses to nothing, not a crash", () => {
  assert.deepEqual(parseSyllabus(""), []);
  assert.deepEqual(parseSyllabus("   \n\n  "), []);
});

test("Arabic and French lesson titles pass through untouched", () => {
  const parsed = parseSyllabus("Week 1\n1. التفكير الخوارزمي\n2. Pensée algorithmique");
  assert.deepEqual(parsed[0].lessons, ["التفكير الخوارزمي", "Pensée algorithmique"]);
});
