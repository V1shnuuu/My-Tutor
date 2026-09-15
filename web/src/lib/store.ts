/** Local-first persistence: preferences in localStorage, chat history in IndexedDB.
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

export const getPref = (k: string) => safeGet(`tutor.pref.${k}`);
export const setPref = (k: string, v: string) => safeSet(`tutor.pref.${k}`, v);
