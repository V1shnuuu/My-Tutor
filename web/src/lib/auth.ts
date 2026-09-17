/**
 * Google Sign-In + session token.
 *
 * The browser gets an ID token from Google Identity Services, hands it to core once, and gets
 * back our own JWT — that JWT, not Google's, is what every later request carries. The client
 * id is public (it ships in this bundle by design) and comes from the server so there is one
 * place to configure it; with none set, `configure()` reports disabled and the app stays in
 * anonymous, browser-local mode.
 */
import { API } from "./api";

const GSI_SRC = "https://accounts.google.com/gsi/client";
const TOKEN_KEY = "tutor.session";

export interface User {
  email: string;
  name: string;
  picture: string;
}

interface GsiCredentialResponse { credential?: string }
interface GsiIdConfig {
  client_id: string;
  callback: (r: GsiCredentialResponse) => void;
  auto_select?: boolean;
  cancel_on_tap_outside?: boolean;
  context?: string;
  ux_mode?: string;
}
interface GsiButtonOptions {
  type?: string; theme?: string; size?: string; text?: string; shape?: string;
  logo_alignment?: string; width?: number;
}
declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize(cfg: GsiIdConfig): void;
          renderButton(el: HTMLElement, opts: GsiButtonOptions): void;
          prompt(): void;
          disableAutoSelect(): void;
        };
      };
    };
  }
}

function safeGet(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
function safeSet(key: string, v: string) {
  try { localStorage.setItem(key, v); } catch { /* private mode */ }
}
function safeRemove(key: string) {
  try { localStorage.removeItem(key); } catch { /* private mode */ }
}

export const getSessionToken = () => safeGet(TOKEN_KEY);
export const setSessionToken = (t: string | null) => (t ? safeSet(TOKEN_KEY, t) : safeRemove(TOKEN_KEY));

const ANON_ID_KEY = "tutor.anon_id";
/** A stable per-browser id for students who never sign in — sent as X-Anon-Id so the server
 * can give each anonymous visitor their own daily/minute budget instead of lumping every
 * signed-out student into one shared bucket (see core/app/main.py's /chat). Minted once and
 * reused; a private/incognito window that clears storage just gets a fresh one next time,
 * which is fine — it's a fairness cap, not an identity anything else depends on. */
export function getAnonId(): string {
  let id = safeGet(ANON_ID_KEY);
  if (!id) {
    id = (crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`);
    safeSet(ANON_ID_KEY, id);
  }
  return id;
}

let gsiReady: Promise<void> | null = null;
/** Loads Google's script once. Rejects rather than hanging when it is blocked (offline, an
 *  ad blocker, a privacy browser), so the caller can show sign-in as unavailable. */
function loadGsi(): Promise<void> {
  if (gsiReady) return gsiReady;
  gsiReady = new Promise((resolve, reject) => {
    if (window.google?.accounts?.id) return resolve();
    const s = document.createElement("script");
    s.src = GSI_SRC;
    s.async = true;
    s.defer = true;
    s.onload = () => (window.google?.accounts?.id ? resolve() : reject(new Error("gsi loaded but absent")));
    s.onerror = () => reject(new Error("gsi blocked"));
    document.head.appendChild(s);
  });
  return gsiReady;
}

/** Exchanges a Google ID token for our session token. */
export async function signInWithGoogle(credential: string): Promise<{ token: string; user: User }> {
  const r = await fetch(`${API}/auth/google`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ credential }),
  });
  if (!r.ok) {
    let code = `http_${r.status}`;
    try { code = (await r.json()).detail || code; } catch { /* non-JSON error body */ }
    throw new Error(code);
  }
  const data = (await r.json()) as { token: string; user: User };
  setSessionToken(data.token);
  return data;
}

/** Renders Google's own button into `el`. Google requires their button for the credential
 *  flow — a custom-styled one cannot mint an ID token. */
export async function renderGoogleButton(
  el: HTMLElement,
  clientId: string,
  onCredential: (credential: string) => void,
): Promise<void> {
  await loadGsi();
  window.google!.accounts.id.initialize({
    client_id: clientId,
    callback: (r) => { if (r.credential) onCredential(r.credential); },
    auto_select: false,
    cancel_on_tap_outside: true,
    context: "signin",
    ux_mode: "popup",
  });
  el.innerHTML = "";
  window.google!.accounts.id.renderButton(el, {
    type: "standard", theme: "outline", size: "large",
    text: "continue_with", shape: "pill", logo_alignment: "left", width: 280,
  });
}

export function signOut() {
  setSessionToken(null);
  try { window.google?.accounts.id.disableAutoSelect(); } catch { /* never loaded */ }
}
