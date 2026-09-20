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
  /** True for an admin-authored lesson with no video mapped yet — see Curriculum.tsx. */
  unassigned?: boolean;
}

export type ChatEvent =
  | { type: "meta"; lang: Lang; arabizi: boolean }
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

export function headers(token?: string | null): HeadersInit {
  const h: Record<string, string> = { "content-type": "application/json" };
  if (token) h.authorization = `Bearer ${token}`;
  return h;
}

export async function check(r: Response): Promise<Response> {
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

export interface AuthInfo { google_client_id: string; enabled: boolean }
export interface UserProfile { email: string; name: string; picture: string }

export async function me(token: string) {
  const r = await check(await fetch(`${API}/me`, { headers: headers(token) }));
  return r.json() as Promise<{
    stt: boolean; tts?: Partial<Record<Lang, boolean>>; avatar?: boolean;
    auth: AuthInfo; user: UserProfile | null;
  }>;
}

// ---------------------------------------------------------------- conversations (signed in)
export interface ConversationSummary {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  message_count: number;
}
export interface StoredTurn {
  role: "user" | "assistant";
  content: string;
  lang: Lang | null;
  citations: Citation[];
  source: "llm" | "cache" | "floor" | "refusal" | null;
  ts: number;
}

export async function listConversations(token: string): Promise<ConversationSummary[]> {
  const r = await check(await fetch(`${API}/conversations`, { headers: headers(token) }));
  return (await r.json()).conversations;
}

export async function createConversation(token: string): Promise<ConversationSummary> {
  const r = await check(await fetch(`${API}/conversations`, { method: "POST", headers: headers(token), body: "{}" }));
  return r.json();
}

export async function getConversation(token: string, id: string): Promise<ConversationSummary & { messages: StoredTurn[] }> {
  const r = await check(await fetch(`${API}/conversations/${id}`, { headers: headers(token) }));
  return r.json();
}

export async function deleteConversation(token: string, id: string): Promise<void> {
  await check(await fetch(`${API}/conversations/${id}`, { method: "DELETE", headers: headers(token) }));
}

export async function renameConversation(token: string, id: string, title: string): Promise<ConversationSummary> {
  const r = await check(await fetch(`${API}/conversations/${id}`, {
    method: "PATCH", headers: headers(token), body: JSON.stringify({ title }),
  }));
  return r.json();
}

export async function listVideos(token: string): Promise<Video[]> {
  const r = await check(await fetch(`${API}/videos`, { headers: headers(token) }));
  return (await r.json()).videos;
}

/** `capLang` ("en" | "ar") returns captions translated to that language regardless of what
 * the video is actually spoken in — see VideoPlayer.tsx, which is the only caller that
 * passes it. Omitted, this is the original untranslated transcript. */
export async function transcript(token: string, videoId: string, capLang?: "en" | "ar"): Promise<{ start: number; end: number; text: string }[]> {
  const qs = capLang ? `?lang=${capLang}` : "";
  const r = await fetch(`${API}/videos/${videoId}/transcript${qs}`, { headers: headers(token) });
  if (!r.ok) return [];
  return r.json();
}

export interface QuizQuestion { question: string; options: string[]; answer_index: number; t: number }

/** Grounded in this video's own transcript — see core/app/quiz.py. First call for a video
 * can take a few seconds (a real LLM generation); cached server-side after that. */
export async function getQuiz(videoId: string): Promise<QuizQuestion[]> {
  const r = await check(await fetch(`${API}/videos/${videoId}/quiz`));
  return (await r.json()).questions;
}

/** Cross-lecture search — every video's transcript, not just the one on screen. No LLM, pure
 * retrieval, so it's instant and exact. Same shape as a chat Citation (title/t/snippet/…),
 * so a result can be handed straight to the same onJump a citation click already uses. */
export async function searchLectures(q: string, k = 10): Promise<Citation[]> {
  if (!q.trim()) return [];
  const r = await check(await fetch(`${API}/search?${new URLSearchParams({ q, k: String(k) })}`));
  return (await r.json()).results;
}

export async function stt(token: string, blob: Blob, langHint?: Lang): Promise<{ text: string; language: string | null }> {
  const fd = new FormData();
  fd.append("file", blob, "audio.webm");
  if (langHint) fd.append("lang_hint", langHint);
  const r = await check(await fetch(`${API}/stt`, { method: "POST", headers: { authorization: `Bearer ${token}` }, body: fd }));
  return r.json();
}

/** POST /chat and yield parsed SSE events.
 *
 * With `conversationId` the server owns the history: it appends this turn to that
 * conversation and feeds the model that conversation's own prior turns, ignoring `history`.
 * Without one (anonymous), `history` is what the client remembers locally.
 *
 * `anonId` (from lib/auth.ts's getAnonId) gives each signed-out browser its own daily/minute
 * budget server-side instead of one shared pool for every anonymous student — pass it
 * whenever there's no signed-in session token.
 *
 * `videoId` is the lecture the student has open: it scopes retrieval to that one video, so
 * the answer comes from what's on screen rather than anywhere in the course. */
export async function* chat(token: string, message: string, history: { role: string; content: string }[], prevLang: Lang | null, spoken = false, signal?: AbortSignal, conversationId?: string | null, anonId?: string, videoId?: string | null): AsyncGenerator<ChatEvent> {
  const r = await check(
    await fetch(`${API}/chat`, {
      method: "POST",
      headers: anonId ? { ...headers(token), "X-Anon-Id": anonId } : headers(token),
      body: JSON.stringify({ message, history, prev_lang: prevLang, spoken, conversation_id: conversationId ?? null, video_id: videoId ?? null }),
      signal,
    }),
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
