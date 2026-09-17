/** Study notes export: the current chat turned into Markdown with every citation's
 * timestamp — the point being a student can review cited lecture moments offline, without
 * re-asking the Tutor. Pure and synchronous: no network call, works for anonymous students
 * exactly the same as signed-in ones, since it only ever reads what's already on screen. */
import type { StoredMessage } from "./store";

const fmtTime = (s: number): string => {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};

export function messagesToMarkdown(messages: StoredMessage[], title: string): string {
  const lines = [`# ${title}`, ""];
  for (const m of messages) {
    if (m.role === "user") {
      lines.push(`## Q: ${m.content}`, "");
      continue;
    }
    lines.push(m.content, "");
    for (const c of m.citations) {
      lines.push(`- **${c.title}** @ ${fmtTime(c.t)}${c.url ? ` — ${c.url}` : c.youtube_id ? ` — https://youtu.be/${c.youtube_id}?t=${Math.floor(c.t)}` : ""}`);
      if (c.snippet) lines.push(`  > ${c.snippet.trim()}`);
    }
    if (m.citations.length) lines.push("");
  }
  return lines.join("\n");
}

export function downloadMarkdown(filename: string, text: string): void {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
