/**
 * Parses a pasted syllabus into weeks/lessons for the admin to review before saving — never
 * saved directly, since a pasted format can't be trusted to always match what this expects
 * (see the master prompt's own "do not assume every pasted syllabus follows exactly this
 * format" instruction). Deliberately permissive rather than strict: a week header is
 * recognised by a handful of shapes, and inside a week every other non-blank line becomes a
 * lesson whether or not it happens to be numbered.
 */
export interface ParsedWeek {
  title: string;
  lessons: string[];
}

const WEEK_LINE = /^week\s+(\d+)\s*[:.\-–]?\s*(.*)$/i;
const NUMBERED_LESSON = /^\s*\d+[.)]\s*(.+)$/;
const BULLET_LESSON = /^\s*[-*•]\s*(.+)$/;

export function parseSyllabus(text: string): ParsedWeek[] {
  const weeks: ParsedWeek[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;

    const weekMatch = line.match(WEEK_LINE);
    if (weekMatch) {
      const [, num, rest] = weekMatch;
      weeks.push({ title: rest ? `Week ${num}: ${rest}` : `Week ${num}`, lessons: [] });
      continue;
    }

    const lessonText = line.match(NUMBERED_LESSON)?.[1] ?? line.match(BULLET_LESSON)?.[1] ?? line;
    if (weeks.length === 0) {
      // Content before any "Week N" header — start an implicit first week rather than
      // silently dropping it, since the admin still needs to see and can delete it.
      weeks.push({ title: "Week 1", lessons: [] });
    }
    weeks[weeks.length - 1].lessons.push(lessonText.trim());
  }
  return weeks.filter((w) => w.lessons.length > 0);
}
