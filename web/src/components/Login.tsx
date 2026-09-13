import { useState } from "react";
import { ApiError, redeem, type Lang } from "../lib/api";
import { t } from "../lib/i18n";
import { deviceId, setPref } from "../lib/store";

interface Props { lang: Lang; onLang: (l: Lang) => void; onToken: (token: string) => void }

export default function Login({ lang, onLang, onToken }: Props) {
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      onToken(await redeem(code, deviceId()));
    } catch (ex) {
      if (ex instanceof ApiError) setErr(ex.code === "code_already_used" ? t("login_used", lang) : ex.code === "invalid_code" ? t("login_invalid", lang) : t("login_offline", lang));
      else setErr(t("login_offline", lang));
    } finally { setBusy(false); }
  };

  return (
    <div className="login" dir={lang === "ar" ? "rtl" : "ltr"} lang={lang}>
      <form className="card" onSubmit={submit}>
        <h1>{t("login_title", lang)}</h1>
        <p>{t("login_help", lang)}</p>
        <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="XXXX-XXXX" autoComplete="one-time-code" inputMode="text" aria-label="Enrollment code" autoFocus />
        <button className="btn" type="submit" disabled={busy || code.replace(/[^a-z0-9]/gi, "").length < 8}>{t("login_btn", lang)}</button>
        {err && <div className="err" role="alert">{err}</div>}
        <div className="lang-row" role="group" aria-label="Language">
          {(["ar", "en", "fr"] as Lang[]).map((l) => (
            <button type="button" key={l} aria-pressed={lang === l} onClick={() => { onLang(l); setPref("lang", l); }}>
              {l === "ar" ? "عربي" : l === "en" ? "English" : "Français"}
            </button>
          ))}
        </div>
      </form>
    </div>
  );
}
