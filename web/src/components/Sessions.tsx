import { useEffect, useRef, useState } from "react";
import { deleteConversation, listConversations, type ConversationSummary, type Lang } from "../lib/api";
import { t } from "../lib/i18n";

interface Props {
  token: string;
  lang: Lang;
  /** Bumped by the parent after a turn is saved, so the list picks up new/renamed chats. */
  refreshKey: number;
  activeId: string | null;
  onResume: (id: string) => void;
}

function when(ts: number, lang: Lang): string {
  const d = new Date(ts * 1000);
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days === 0) return d.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" });
  if (days === 1) return t("yesterday", lang);
  if (days < 7) return `${days} ${t("days_ago", lang)}`;
  return d.toLocaleDateString(lang, { day: "numeric", month: "short" });
}

/** The past-conversations drawer, opened by the small tab on the trailing edge. */
export default function Sessions({ token, lang, refreshKey, activeId, onResume }: Props) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(false);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setLoading(true); setErr(false);
    listConversations(token)
      .then((c) => alive && setItems(c))
      .catch(() => alive && setErr(true))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [open, token, refreshKey]);

  // Escape closes, and focus moves into the panel when it opens — it is a dialog, so it has
  // to be operable without a mouse.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", onKey);
    panel.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  const remove = async (id: string) => {
    const before = items;
    setItems((cur) => cur.filter((c) => c.id !== id));   // optimistic: the row goes at once
    try {
      await deleteConversation(token, id);
    } catch {
      setItems(before);                                  // ...and comes back if the server refused
      setErr(true);
    }
  };

  return (
    <>
      <button
        className="sessions-tab"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls="sessions-panel"
        title={t("past_chats", lang)}
      >
        <span aria-hidden="true">🕘</span>
        <span className="sr-only">{t("past_chats", lang)}</span>
      </button>

      {open && <div className="sessions-scrim" onClick={() => setOpen(false)} />}

      <div
        id="sessions-panel"
        className={`sessions-panel ${open ? "open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-label={t("past_chats", lang)}
        aria-hidden={!open}
        tabIndex={-1}
        ref={panel}
      >
        <header>
          <h2>{t("past_chats", lang)}</h2>
          <button className="icon-btn" onClick={() => setOpen(false)} aria-label={t("close", lang)}>✕</button>
        </header>

        {loading && <p className="muted">{t("loading", lang)}</p>}
        {err && !loading && <p className="err">{t("offline", lang)}</p>}
        {!loading && !err && items.length === 0 && <p className="muted">{t("no_past_chats", lang)}</p>}

        <ul>
          {items.map((c) => (
            <li key={c.id} className={c.id === activeId ? "active" : ""}>
              <button className="session-row" onClick={() => { onResume(c.id); setOpen(false); }}>
                <span className="session-title" dir="auto">{c.title || t("untitled_chat", lang)}</span>
                <span className="session-meta">{when(c.updated_at, lang)} · {c.message_count}</span>
              </button>
              <button
                className="icon-btn danger"
                onClick={() => remove(c.id)}
                aria-label={`${t("delete", lang)}: ${c.title || t("untitled_chat", lang)}`}
              >
                🗑
              </button>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}
