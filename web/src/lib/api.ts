/** Core API client + SSE stream parser. */
export const API = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") || "http://127.0.0.1:8000";

export type Lang = "ar" | "en" | "fr";

export interface Citation {
  ref: string;
  video_id: string;
  title: string;
  youtube_id: string | null;
  url: string | null;
  t: number;
  t_end: number;
  snippet: string;
  score: number;
}

export interface Video {
  id: string;
  title: string;
  source: "youtube" | "file";
  youtube_id: string | null;
  url: string | null;
  duration: number | null;
  lang: string | null;
  week: number | null;
}

export type ChatEvent =
  | { type: "meta"; lang: Lang; arabizi: boolean; budget: { used: number; cap: number } }
  | { type: "status"; stage: "retrieving" | "queued" | "generating" | "cached" | "floor" | "refusal"; text?: string; eta?: number; depth?: number; provider?: string }
  | { type: "citations"; items: Citation[] }
  | { type: "token"; text: string }
  | { type: "replace"; text: string }
  | { type: "done"; answer: string; source: "llm" | "cache" | "floor" | "refusal"; provider?: string; ms: number }
  | { type: "error"; code: string };

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

function headers(token?: string | null): HeadersInit {
  const h: Record<string, string> = { "content-type": "application/json" };
  if (token) h.authorization = `Bearer ${token}`;
  return h;
}

async function check(r: Response): Promise<Response> {
  if (r.ok) return r;
  let code = `http_${r.status}`;
  try {
    const j = await r.json();
    code = j.detail || code;
  } catch {
    /* ignore */
  }
  throw new ApiError(r.status, code);
}

export async function redeem(code: string, deviceId: string): Promise<string> {
  const r = await check(await fetch(`${API}/auth/redeem`, { method: "POST", headers: headers(), body: JSON.stringify({ code, device_id: deviceId }) }));
  return (await r.json()).token;
}

export async function me(token: string) {
  const r = await check(await fetch(`${API}/me`, { headers: headers(token) }));
  return r.json() as Promise<{ id: string; budget: { used: number; cap: number }; stt: boolean; tts?: Partial<Record<Lang, boolean>>; avatar?: boolean }>;
}

export async function listVideos(token: string): Promise<Video[]> {
  const r = await check(await fetch(`${API}/videos`, { headers: headers(token) }));
  return (await r.json()).videos;
}

export async function transcript(token: string, videoId: string): Promise<{ start: number; end: number; text: string }[]> {
  const r = await fetch(`${API}/videos/${videoId}/transcript`, { headers: headers(token) });
  if (!r.ok) return [];
  return r.json();
}

export async function stt(token: string, blob: Blob, langHint?: Lang): Promise<{ text: string; language: string | null }> {
  const fd = new FormData();
  fd.append("file", blob, "audio.webm");
  if (langHint) fd.append("lang_hint", langHint);
  const r = await check(await fetch(`${API}/stt`, { method: "POST", headers: { authorization: `Bearer ${token}` }, body: fd }));
  return r.json();
}

/** POST /chat and yield parsed SSE events. */
export async function* chat(token: string, message: string, history: { role: string; content: string }[], prevLang: Lang | null, spoken = false, forceLang: Lang | null = null, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
  const r = await check(
    await fetch(`${API}/chat`, { method: "POST", headers: headers(token), body: JSON.stringify({ message, history, prev_lang: prevLang, spoken, force_lang: forceLang }), signal }),
  );
  const reader = r.body!.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("data:")) {
          try {
            yield JSON.parse(line.slice(5)) as ChatEvent;
          } catch {
            /* skip malformed frame */
          }
        }
      }
    }
  }
}
