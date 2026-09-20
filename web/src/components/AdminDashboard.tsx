import { useCallback, useEffect, useState } from "react";
import { listVideos, type Video } from "../lib/api";
import {
  addLesson, addWeek, aiParseSyllabus, assignVideo, bulkSyllabus, connectPlaylist, createCourse,
  deleteCourse, deleteLesson, deletePlaylist, deleteWeek, getAdminCourse, getAnalytics, listCourses,
  listPlaylistVideos, publishCourse, renameLesson, renameWeek, reorderLessons, reorderWeeks, retryIngest,
  syncPlaylist, unpublishCourse, updateCourse,
  type AdminCourseTree, type Analytics, type Course, type PlaylistVideo,
} from "../lib/course";
import { parseSyllabus, type ParsedWeek } from "../lib/syllabus";

interface Props {
  token: string;
  onExit: () => void;
}

const ING_LABEL: Record<string, string> = {
  pending: "not ingested yet", ingesting: "learning this lecture…", ready: "Tutor ready", failed: "ingestion failed",
};

/**
 * The Admin Dashboard: course/syllabus authoring + YouTube playlist connect + per-lesson video
 * mapping. A separate screen from the Student Dashboard (App.tsx renders this instead of the
 * whole app when the URL path is /admin and the signed-in account passes the server-side admin
 * check) — deliberately not styled to look like it, so nobody mistakes which one they're on.
 */
export default function AdminDashboard({ token, onExit }: Props) {
  const [courses, setCourses] = useState<Course[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [tree, setTree] = useState<AdminCourseTree | null>(null);
  const [playlistVideos, setPlaylistVideos] = useState<PlaylistVideo[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [playlistUrl, setPlaylistUrl] = useState("");
  const [bulkText, setBulkText] = useState("");
  const [parsed, setParsed] = useState<ParsedWeek[] | null>(null);
  const [parsing, setParsing] = useState(false);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [corpusVideos, setCorpusVideos] = useState<Video[] | null>(null);

  const refreshCourses = useCallback(async () => {
    setCourses(await listCourses(token));
  }, [token]);

  useEffect(() => { void getAnalytics(token).then(setAnalytics).catch(() => setAnalytics(null)); }, [token]);
  useEffect(() => { void listVideos(token).then(setCorpusVideos).catch(() => setCorpusVideos(null)); }, [token]);

  const refreshTree = useCallback(async (id: string) => {
    const t = await getAdminCourse(token, id);
    setTree(t);
    setPlaylistVideos(t.playlist ? await listPlaylistVideos(token, id) : []);
  }, [token]);

  useEffect(() => { void refreshCourses(); }, [refreshCourses]);
  useEffect(() => { if (activeId) void refreshTree(activeId); else setTree(null); }, [activeId, refreshTree]);

  // Transcription runs for minutes in a background thread server-side; the status badge is
  // the only window into it. Poll while anything is in flight so "learning this lecture…"
  // actually turns into "Tutor ready" (or "failed", with its error) without a manual reload.
  const inFlight = !!tree?.weeks.some((w) => w.lessons.some((l) => l.video_ingest_status === "pending" || l.video_ingest_status === "ingesting"));
  useEffect(() => {
    if (!inFlight || !activeId) return;
    const id = setInterval(() => { void refreshTree(activeId); }, 10_000);
    return () => clearInterval(id);
  }, [inFlight, activeId, refreshTree]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setErr(null);
    try {
      await fn();
      if (activeId) await refreshTree(activeId);
      await refreshCourses();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const create = () => {
    if (!newTitle.trim()) return;
    void run(async () => {
      const c = await createCourse(token, newTitle.trim());
      setNewTitle("");
      setActiveId(c.id);
    });
  };

  const connect = () => {
    if (!activeId || !playlistUrl.trim()) return;
    void run(async () => { await connectPlaylist(token, activeId, playlistUrl.trim()); setPlaylistUrl(""); });
  };

  const doParse = async () => {
    if (!bulkText.trim()) return;
    const local = parseSyllabus(bulkText);
    if (local.length > 0) { setParsed(local); setErr(null); return; }
    // The plain "Week N" + numbered-list format found nothing — let the local LLM (same one
    // the Tutor's chat already uses, no new key) take a pass at unstructured pasted text
    // instead of just reporting "nothing found".
    setParsing(true); setErr(null);
    try {
      setParsed(await aiParseSyllabus(token, bulkText));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setParsed(local);
    } finally {
      setParsing(false);
    }
  };

  const applyBulk = () => {
    if (!activeId || !parsed?.length) return;
    void run(async () => {
      await bulkSyllabus(token, activeId, parsed);
      setBulkText(""); setParsed(null);
    });
  };

  if (!tree && !activeId) {
    return (
      <div className="admin-shell">
        <header className="admin-topbar">
          <h1>Admin Dashboard</h1>
          <button className="btn" onClick={onExit}>← Back to tutor</button>
        </header>
        {err && <div className="admin-err">{err}</div>}
        <section className="admin-card">
          <h2>Courses</h2>
          <ul className="admin-course-list">
            {courses.map((c) => (
              <li key={c.id}>
                <button className="admin-course-row" onClick={() => setActiveId(c.id)}>
                  <span>{c.title}</span>
                  <span className={`admin-status ${c.status}`}>{c.status}</span>
                </button>
                <button className="icon-btn danger" onClick={() => run(() => deleteCourse(token, c.id))} aria-label={`Delete ${c.title}`}>🗑</button>
              </li>
            ))}
            {courses.length === 0 && <li className="admin-empty">No courses yet — create one below.</li>}
          </ul>
          <div className="admin-row">
            <input value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder="Course title, e.g. MIT 6.006 Introduction to Algorithms" onKeyDown={(e) => e.key === "Enter" && create()} />
            <button className="btn primary" disabled={busy || !newTitle.trim()} onClick={create}>+ Create Course</button>
          </div>
        </section>

        {analytics && (
          <section className="admin-card">
            <h2>Last 24 Hours</h2>
            <div className="admin-analytics-grid">
              <div><strong>{analytics.questions_24h}</strong><span>questions answered</span></div>
              <div><strong>{analytics.cache_hit_rate_24h != null ? `${Math.round(analytics.cache_hit_rate_24h * 100)}%` : "—"}</strong><span>cache hit rate</span></div>
              <div><strong>{analytics.refusal_rate_24h != null ? `${Math.round(analytics.refusal_rate_24h * 100)}%` : "—"}</strong><span>off-topic refusals</span></div>
              <div><strong>{analytics.floor_rate_24h != null ? `${Math.round(analytics.floor_rate_24h * 100)}%` : "—"}</strong><span>answered with no LLM (floor)</span></div>
              <div><strong>{analytics.corpus.videos}</strong><span>videos indexed, {analytics.corpus.chunks} chunks</span></div>
              <div><strong>{analytics.queue_depth}</strong><span>requests queued right now</span></div>
            </div>
            {analytics.providers.length > 0 && (
              <table className="admin-provider-table">
                <thead><tr><th>Provider</th><th>Tier</th><th>Used today</th><th>Errors</th></tr></thead>
                <tbody>
                  {analytics.providers.map((p) => (
                    <tr key={p.id}><td>{p.id}</td><td>{p.tier}</td><td>{p.rpd_used}{p.rpd ? ` / ${p.rpd}` : ""}</td><td>{p.errors}</td></tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="muted">Speech-to-text: {analytics.stt.engine}{analytics.stt.cap ? ` (${analytics.stt.used}/${analytics.stt.cap} used today)` : ""}</p>
          </section>
        )}

        {corpusVideos && corpusVideos.length > 0 && (
          <section className="admin-card">
            <h2>Ingested corpus ({corpusVideos.length})</h2>
            <p className="muted">
              Already transcribed and indexed, but not organized into any course's lessons — students don't see
              this list. To turn any of it into real lessons, connect a matching playlist above and assign videos
              to lessons; anything left here stays indexed but unused.
            </p>
            <ul className="admin-corpus-list">
              {corpusVideos.map((v) => (
                <li key={v.id}>
                  <span className="title" dir="auto">{v.title}</span>
                  {v.duration ? <span className="muted">{Math.round(v.duration / 60)}′</span> : null}
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    );
  }

  if (!tree) return <div className="admin-shell"><p>Loading course…</p></div>;

  return (
    <div className="admin-shell">
      <header className="admin-topbar">
        <button className="btn-ghost" onClick={() => setActiveId(null)}>← All courses</button>
        <input
          className="admin-inline-title admin-course-title"
          defaultValue={tree.title}
          onBlur={(e) => {
            const title = e.target.value.trim();
            if (title && title !== tree.title) void run(() => updateCourse(token, tree.id, title, tree.description));
          }}
        />
        <span className={`admin-status ${tree.status}`}>{tree.status}</span>
        <div className="admin-topbar-actions">
          <a className="btn" href="/" target="_blank" rel="noreferrer">Preview Student Dashboard</a>
          {tree.status === "published"
            ? <button className="btn" disabled={busy} onClick={() => run(() => unpublishCourse(token, tree.id))}>Unpublish</button>
            : <button className="btn primary" disabled={busy} onClick={() => run(() => publishCourse(token, tree.id))}>Publish</button>}
          <button className="btn" onClick={onExit}>← Back to tutor</button>
        </div>
      </header>
      {err && <div className="admin-err">{err}</div>}

      <section className="admin-card">
        <h2>Course details</h2>
        <textarea
          className="admin-bulk"
          rows={2}
          defaultValue={tree.description}
          placeholder="Optional course description — shown nowhere to students yet, but kept here for your own reference."
          onBlur={(e) => {
            const description = e.target.value;
            if (description !== tree.description) void run(() => updateCourse(token, tree.id, tree.title, description));
          }}
        />
      </section>

      <section className="admin-card">
        <h2>YouTube Playlist</h2>
        {tree.playlist ? (
          <div className="admin-playlist-info">
            <img src={tree.playlist.thumbnail} alt="" />
            <div>
              <strong>{tree.playlist.title}</strong>
              <div className="muted">{tree.playlist.channel_title} · {playlistVideos.length} video{playlistVideos.length === 1 ? "" : "s"}</div>
            </div>
            <button className="btn" disabled={busy} onClick={() => run(() => syncPlaylist(token, tree.id))}>Sync Playlist</button>
            <button
              className="btn danger"
              disabled={busy}
              onClick={() => {
                if (confirm("Disconnect this playlist? Lessons pointing at its videos go back to \"not assigned yet\" — already-transcribed videos stay usable if you reconnect the same playlist later.")) {
                  void run(() => deletePlaylist(token, tree.id));
                }
              }}
            >
              Disconnect
            </button>
          </div>
        ) : (
          <div className="admin-row">
            <input value={playlistUrl} onChange={(e) => setPlaylistUrl(e.target.value)} placeholder="https://www.youtube.com/playlist?list=..." onKeyDown={(e) => e.key === "Enter" && connect()} />
            <button className="btn primary" disabled={busy || !playlistUrl.trim()} onClick={connect}>Connect Playlist</button>
          </div>
        )}
        {tree.unassigned_videos.length > 0 && (
          <details className="admin-unassigned">
            <summary>Unassigned playlist videos ({tree.unassigned_videos.length})</summary>
            <ul>
              {tree.unassigned_videos.map((v) => (
                <li key={v.id}>○ {v.title} <span className="muted">({ING_LABEL[v.ingest_status]})</span></li>
              ))}
            </ul>
          </details>
        )}
      </section>

      <section className="admin-card">
        <h2>Paste a Syllabus</h2>
        <p className="muted">Paste a full syllabus — clean "Week N" + numbered lessons parses instantly; anything messier is automatically restructured by the local model. Review the result before saving — nothing is written until you click Save.</p>
        <textarea className="admin-bulk" rows={6} value={bulkText} onChange={(e) => { setBulkText(e.target.value); setParsed(null); }} placeholder={"WEEK 1\n1. Algorithmic Thinking, Peak Finding\n2. Models of Computation, Document Distance\n\nWEEK 2\n1. Insertion Sort, Merge Sort"} />
        <div className="admin-row">
          <button className="btn" disabled={!bulkText.trim() || parsing} onClick={() => void doParse()}>{parsing ? "Parsing with AI…" : "Parse"}</button>
          {parsed && <button className="btn primary" disabled={busy || !parsed.length} onClick={applyBulk}>Save {parsed.length} week{parsed.length === 1 ? "" : "s"} to course</button>}
        </div>
        {parsed && (
          <div className="admin-preview">
            {parsed.length === 0 && <p className="muted">Nothing recognisable was found — check the format.</p>}
            {parsed.map((w, i) => (
              <div key={i} className="admin-preview-week">
                <strong>{w.title}</strong>
                <ol>{w.lessons.map((l, j) => <li key={j}>{l}</li>)}</ol>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="admin-card">
        <h2>Syllabus</h2>
        {tree.weeks.map((w, wi) => (
          <div key={w.id} className="admin-week">
            <div className="admin-week-header">
              <input className="admin-inline-title" defaultValue={w.title} onBlur={(e) => e.target.value !== w.title && run(() => renameWeek(token, w.id, e.target.value))} />
              <div className="admin-week-actions">
                <button className="icon-btn" disabled={wi === 0} onClick={() => { const ids = tree.weeks.map((x) => x.id); [ids[wi - 1], ids[wi]] = [ids[wi], ids[wi - 1]]; void run(() => reorderWeeks(token, tree.id, ids)); }}>↑</button>
                <button className="icon-btn" disabled={wi === tree.weeks.length - 1} onClick={() => { const ids = tree.weeks.map((x) => x.id); [ids[wi], ids[wi + 1]] = [ids[wi + 1], ids[wi]]; void run(() => reorderWeeks(token, tree.id, ids)); }}>↓</button>
                <button className="icon-btn danger" onClick={() => run(() => deleteWeek(token, w.id))}>🗑 Delete week</button>
              </div>
            </div>
            {w.lessons.map((l, li) => (
              <div key={l.id} className="admin-lesson">
                <span className="admin-lesson-n">{li + 1}</span>
                <input className="admin-inline-title" defaultValue={l.title} onBlur={(e) => e.target.value !== l.title && run(() => renameLesson(token, l.id, e.target.value))} />
                <select
                  value={l.video_id ?? ""}
                  onChange={(e) => run(() => assignVideo(token, l.id, e.target.value || null))}
                >
                  <option value="">— Video: not assigned yet —</option>
                  {playlistVideos.map((v) => <option key={v.id} value={v.id}>{v.title}</option>)}
                </select>
                {l.video_ingest_status && (
                  <span className={`admin-ingest ${l.video_ingest_status}`} title={l.video_ingest_error ?? undefined}>
                    {ING_LABEL[l.video_ingest_status]}
                  </span>
                )}
                {l.video_ingest_status === "failed" && l.video_id && (
                  <button className="btn-ghost" disabled={busy} title={l.video_ingest_error ?? undefined} onClick={() => run(() => retryIngest(token, l.video_id!))}>↻ Retry</button>
                )}
                <button className="icon-btn" disabled={li === 0} onClick={() => { const ids = w.lessons.map((x) => x.id); [ids[li - 1], ids[li]] = [ids[li], ids[li - 1]]; void run(() => reorderLessons(token, w.id, ids)); }}>↑</button>
                <button className="icon-btn" disabled={li === w.lessons.length - 1} onClick={() => { const ids = w.lessons.map((x) => x.id); [ids[li], ids[li + 1]] = [ids[li + 1], ids[li]]; void run(() => reorderLessons(token, w.id, ids)); }}>↓</button>
                <button className="icon-btn danger" onClick={() => run(() => deleteLesson(token, l.id))}>🗑</button>
              </div>
            ))}
            <button className="btn-ghost" onClick={() => run(() => addLesson(token, w.id, "New lesson"))}>+ Add Lesson</button>
          </div>
        ))}
        <button className="btn" onClick={() => run(() => addWeek(token, tree.id, `Week ${tree.weeks.length + 1}`))}>+ Add Week</button>
      </section>
    </div>
  );
}
