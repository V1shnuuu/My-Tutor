import { useState } from "react";
import { searchLectures, type Citation, type Lang, type Video } from "../lib/api";
import { t } from "../lib/i18n";

interface Props {
  videos: Video[];
  activeId: string | null;
  lang: Lang;
  onPick: (id: string) => void;
  onJump: (c: Citation) => void;
  /** True only for a signed-in account the server has verified against ADMIN_EMAILS — false
   * (or not passed) for every student, so the button below simply doesn't exist for them
   * rather than existing-but-disabled. */
  isCourseAdmin?: boolean;
}

const mins = (s: number | null) => (s ? `${Math.round(s / 60)}′` : "");

/**
 * The course contents down the side of the page: every lecture in the index, in order,
 * with the one being watched marked. It is also the honest answer to "what can I ask
 * about?" — the tutor only knows what is on this list, so the list is worth showing.
 */
export default function Curriculum({ videos, activeId, lang, onPick, onJump, isCourseAdmin }: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Citation[] | null>(null);
  const [searching, setSearching] = useState(false);

  const runSearch = (q: string) => {
    setQuery(q);
    if (!q.trim()) { setResults(null); return; }
    setSearching(true);
    searchLectures(q).then(setResults).finally(() => setSearching(false));
  };

  // Weeks when the videos declare them, otherwise one flat list. Ingest leaves `week` null
  // unless videos.yaml sets it, and inventing numbers would misrepresent the course.
  const weeks = [...new Set(videos.map((v) => v.week).filter((w): w is number => w != null))].sort((a, b) => a - b);
  const loose = videos.filter((v) => v.week == null);

  // An admin-authored lesson with no video assigned yet: still shown (the syllabus is real,
  // the recording just isn't linked), just not clickable into an empty player.
  const row = (v: Video, n: number) => (
    <li key={v.id}>
      <button
        className={`lesson ${v.id === activeId ? "active" : ""} ${v.unassigned ? "unassigned" : ""}`}
        onClick={() => !v.unassigned && onPick(v.id)}
        aria-current={v.id === activeId ? "true" : undefined}
        aria-disabled={v.unassigned || undefined}
      >
        <span className="n">{n}</span>
        <span className="title" dir="auto">{v.title}</span>
        {v.unassigned ? <span className="dur">{t("video_unassigned", lang)}</span> : v.duration ? <span className="dur">{mins(v.duration)}</span> : null}
      </button>
    </li>
  );

  return (
    <nav className="curriculum" aria-label={t("curriculum", lang)}>
      <div className="curriculum-heading">
        <h2>{t("curriculum", lang)}</h2>
        {isCourseAdmin && (
          <button className="admin-entry" onClick={() => { window.location.href = "/admin"; }}>⚙ Admin</button>
        )}
      </div>
      <div className="curriculum-search">
        <input
          type="search"
          value={query}
          onChange={(e) => runSearch(e.target.value)}
          placeholder={t("search_lectures", lang)}
          aria-label={t("search_lectures", lang)}
        />
      </div>
      {results !== null ? (
        <div className="curriculum-results">
          {searching && <p className="empty">{t("loading", lang)}</p>}
          {!searching && results.length === 0 && <p className="empty">{t("search_no_results", lang)}</p>}
          <ul>
            {results.map((r, i) => (
              <li key={`${r.video_id}-${i}`}>
                <button className="search-result" onClick={() => onJump(r)}>
                  <span className="title" dir="auto">{r.title}</span>
                  <span className="snippet" dir="auto">{r.snippet}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : videos.length === 0 ? (
        <p className="empty">{t("curriculum_empty", lang)}</p>
      ) : (
        <>
          {weeks.map((w) => (
            <section key={w}>
              <h3>{t("week", lang)} {w}</h3>
              <ol>{videos.filter((v) => v.week === w).map((v, i) => row(v, i + 1))}</ol>
            </section>
          ))}
          {loose.length > 0 && <ol>{loose.map((v, i) => row(v, i + 1))}</ol>}
        </>
      )}
    </nav>
  );
}
