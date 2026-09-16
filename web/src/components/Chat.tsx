import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { Citation, Lang } from "../lib/api";
import { t } from "../lib/i18n";
import type { StoredMessage } from "../lib/store";
import type { LiveTranscript } from "../lib/transcript";

export interface LiveState {
  stage: "idle" | "retrieving" | "queued" | "generating" | "cached" | "floor" | "refusal";
  text?: string;
  eta?: number;
}

interface Props {
  lang: Lang;
  messages: StoredMessage[];
  live: LiveState;
  streaming: boolean;
  speakingSentence: string | null;
  /** Live speech preview. Never submitted as-is — see App's onFinal for what actually gets sent. */
  transcript: LiveTranscript;
  listening: boolean;
  sttMode: "server" | "browser" | null;
  onSend: (text: string) => void;
  onMic: () => void;
  onStop: () => void;
  onJump: (c: Citation) => void;
}

const fmt = (s: number) => {
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};

/** Render answer text with [C1] tags turned into clickable timestamp chips and the sentence being spoken highlighted. */
function Body({ text, citations, speaking, onJump, lang }: { text: string; citations: Citation[]; speaking: string | null; onJump: (c: Citation) => void; lang: Lang }) {
  const parts = text.split(/(\[C\d+\](?:\[C\d+\])*)/g);
  const byRef = new Map(citations.map((c) => [c.ref, c]));
  let hlDone = false;
  return (
    <>
      {parts.map((p, i) => {
        if (/^\[C\d+\]/.test(p)) {
          const refs = p.match(/C\d+/g) || [];
          return refs.map((r, j) => {
            const c = byRef.get(r);
            if (!c) return null;
            return (
              <button key={`${i}-${j}`} className="cite" onClick={() => onJump(c)} title={`${t("jump", lang)} ${fmt(c.t)} — ${c.title}`}>
                <span aria-hidden="true">▶</span> <span className="t">{fmt(c.t)}</span>
              </button>
            );
          });
        }
        if (speaking && !hlDone) {
          const idx = p.indexOf(speaking);
          if (idx >= 0) {
            hlDone = true;
            return (
              <span key={i}>
                {p.slice(0, idx)}<span className="speaking-now">{speaking}</span>{p.slice(idx + speaking.length)}
              </span>
            );
          }
        }
        return <span key={i}>{p}</span>;
      })}
    </>
  );
}

export default function Chat({ lang, messages, live, streaming, speakingSentence, transcript, listening, sttMode, onSend, onMic, onStop, onJump }: Props) {
  const [draft, setDraft] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const liveRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, live, transcript.text]);

  // A long dictation scrolls inside its own box, so the newest words stay in view.
  useEffect(() => {
    const el = liveRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [transcript.text]);

  const submit = () => {
    const text = draft.trim();
    if (!text || streaming) return;
    setDraft("");
    onSend(text);
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
    if (e.key === "ArrowUp" && !draft) {
      const last = [...messages].reverse().find((m) => m.role === "user");
      if (last) setDraft(last.content);
    }
  };

  const stageLine = () => {
    switch (live.stage) {
      case "retrieving": return <span className="status-line"><span className="dots">{t("retrieving", lang)}</span></span>;
      case "generating": return <span className="status-line"><span className="dots">{t("generating", lang)}</span></span>;
      case "queued": return <span className="status-line"><span className="queue-banner">⏳ {live.text}</span></span>;
      default: return null;
    }
  };

  return (
    <div className="chat">
      <div className="sr-only" aria-live="polite">{live.stage === "generating" && speakingSentence ? speakingSentence : ""}</div>
      <div className="messages" ref={listRef} role="log" aria-label="Conversation">
        {messages.map((m, i) => {
          const isLast = i === messages.length - 1;
          return (
            <div key={m.id ?? i} className={`msg ${m.role}`}>
              <div className="bubble" dir="auto" lang={m.lang}>
                {m.role === "assistant"
                  ? <Body text={m.content} citations={m.citations} speaking={isLast ? speakingSentence : null} onJump={onJump} lang={lang} />
                  : m.content}
                {isLast && streaming && m.role === "assistant" && !m.content && stageLine()}
              </div>
              {m.role === "assistant" && m.source && m.source !== "llm" && (
                <div className="meta"><span className="tag">{t(m.source === "cache" ? "cached" : m.source === "floor" ? "floor" : "refusal", lang)}</span></div>
              )}
              {isLast && streaming && m.role === "assistant" && m.content && live.stage === "queued" && <div className="meta">{stageLine()}</div>}
            </div>
          );
        })}
      </div>
      {listening && (
        <div className="live-transcript" dir="auto" ref={liveRef} data-testid="live-transcript" data-empty={!transcript.text || undefined}>
          <span className="live-dot" aria-hidden="true" />
          <span className="live-text">
            {transcript.text ? (
              <>
                {/* Settled words render solid; the tail the recogniser may still revise is
                    dimmed, so the student can see which part is not final yet. */}
                <span className="committed" data-testid="live-committed">{transcript.committed}</span>
                {transcript.committed && transcript.interim ? " " : ""}
                <span className="pending" data-testid="live-interim">{transcript.interim}</span>
              </>
            ) : (
              <span className="muted">{t("listening", lang)}…</span>
            )}
          </span>
          <span className="live-label">
            {t("listening", lang)}{sttMode === "browser" ? ` · ${t("stt_fallback", lang)}` : ""}
          </span>
        </div>
      )}
      {/* The transcript is announced politely rather than rendered into a live region on every
          keystroke-equivalent, which would make a screen reader read each partial guess. */}
      <div className="sr-only" aria-live="polite">
        {listening && transcript.committed ? transcript.committed : ""}
      </div>
      <div className="composer">
        <button className={`btn ${listening ? "rec" : ""}`} onClick={listening ? onStop : onMic} aria-label={listening ? t("stop", lang) : t("mic", lang)} data-testid="mic-button" data-listening={listening || undefined} disabled={streaming && !listening}>
          {listening ? "■" : "🎙"}
        </button>
        <textarea
          ref={taRef}
          value={listening ? transcript.text : draft}
          onChange={(e) => { if (!listening) setDraft(e.target.value); }}
          onKeyDown={onKey}
          placeholder={listening ? t("listening", lang) : t("placeholder", lang)}
          rows={1}
          maxLength={800}
          aria-label={t("placeholder", lang)}
          dir="auto"
          readOnly={listening}
        />
        <button className="btn primary" onClick={submit} disabled={!draft.trim() || streaming} aria-label={t("send", lang)}>
          <span className="icon-dir" aria-hidden="true">➤</span>
        </button>
      </div>
    </div>
  );
}
