import { useEffect, useRef, useState } from "react";
import type { Lang } from "../lib/api";
import { renderGoogleButton, signInWithGoogle, type User } from "../lib/auth";
import { t } from "../lib/i18n";
import { setPref } from "../lib/store";

interface Props {
  lang: Lang;
  clientId: string;
  onLang: (l: Lang) => void;
  onSignedIn: (token: string, user: User) => void;
  /** Sign-in buys saved history, not access, so there is always a way past this screen. */
  onSkip: () => void;
}

export default function SignIn({ lang, clientId, onLang, onSignedIn, onSkip }: Props) {
  const slot = useRef<HTMLDivElement>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        await renderGoogleButton(slot.current!, clientId, async (credential) => {
          if (!alive) return;
          setBusy(true); setErr(null);
          try {
            const { token, user } = await signInWithGoogle(credential);
            if (alive) onSignedIn(token, user);
          } catch (e) {
            if (alive) setErr((e as Error).message || "signin_failed");
          } finally {
            if (alive) setBusy(false);
          }
        });
      } catch {
        // Google's script is blocked (offline, an ad blocker, a privacy browser). Saying so
        // is better than an empty space where a button should be.
        if (alive) setErr("gsi_blocked");
      }
    })();
    return () => { alive = false; };
  }, [clientId, onSignedIn]);

  return (
    <div className="login" dir={lang === "ar" ? "rtl" : "ltr"} lang={lang}>
      <div className="card">
        <h1>{t("signin_title", lang)}</h1>
        <p>{t("signin_help", lang)}</p>
        <div className="gsi-slot" ref={slot} aria-busy={busy} />
        {err === "gsi_blocked" && <div className="err" role="alert">{t("signin_blocked", lang)}</div>}
        {err && err !== "gsi_blocked" && <div className="err" role="alert">{t("signin_failed", lang)}</div>}
        <button className="link-btn" onClick={onSkip}>{t("signin_skip", lang)}</button>
        <div className="lang-row" role="group" aria-label="Language">
          {(["ar", "en", "fr"] as Lang[]).map((l) => (
            <button type="button" key={l} aria-pressed={lang === l} onClick={() => { onLang(l); setPref("lang", l); }}>
              {l === "ar" ? "عربي" : l === "en" ? "English" : "Français"}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
