/** Local-first persistence: auth token + device id in localStorage, chat history in IndexedDB.
 * The server never stores conversation text. */
import Dexie, { type Table } from "dexie";
import type { Citation, Lang } from "./api";

export interface StoredMessage {
  id?: number;
  role: "user" | "assistant";
  content: string;
  lang: Lang;
  citations: Citation[];
  source?: "llm" | "cache" | "floor" | "refusal";
  ts: number;
}

class TutorDB extends Dexie {
  messages!: Table<StoredMessage, number>;
  constructor() {
    super("adaptive-tutor");
    this.version(1).stores({ messages: "++id, ts" });
  }
}
export const db = new TutorDB();

function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function safeSet(key: string, v: string) {
  try {
    localStorage.setItem(key, v);
  } catch {
    /* private mode etc. */
  }
}

export function deviceId(): string {
  let id = safeGet("tutor.device");
  if (!id) {
    id = (crypto.randomUUID?.() || Math.random().toString(36).slice(2) + Date.now().toString(36)).replace(/-/g, "").slice(0, 32);
    safeSet("tutor.device", id);
  }
  return id;
}
export const getToken = () => safeGet("tutor.token");
export const setToken = (t: string | null) => (t ? safeSet("tutor.token", t) : localStorage.removeItem("tutor.token"));
export const getPref = (k: string) => safeGet(`tutor.pref.${k}`);
export const setPref = (k: string, v: string) => safeSet(`tutor.pref.${k}`, v);
