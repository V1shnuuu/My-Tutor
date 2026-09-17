/** Admin-authored course content: courses, weeks, lessons, connected YouTube playlist, and
 * the student-facing published tree. Mirrors core/app/content.py's shapes exactly. */
import { API, check, headers } from "./api";

export type IngestStatus = "pending" | "ingesting" | "ready" | "failed";

export interface CourseLesson {
  id: string;
  title: string;
  position: number;
  video_id: string | null;              // synthetic row id — what assignVideo() takes
  video_youtube_id: string | null;      // the real id — what the player actually needs
  video_title: string | null;
  video_thumbnail: string | null;
  video_duration_s: number | null;
  video_ingest_status: IngestStatus | null;
  video_corpus_id: string | null;
}
export interface CourseWeek {
  id: string;
  title: string;
  position: number;
  lessons: CourseLesson[];
}
export interface CoursePlaylist {
  id: string;
  youtube_playlist_id: string;
  title: string;
  channel_title: string;
  thumbnail: string;
  synced_at: number | null;
}
export interface Course {
  id: string;
  title: string;
  description: string;
  status: "draft" | "published";
  created_at: number;
  updated_at: number;
}
export interface CourseTree extends Course {
  weeks: CourseWeek[];
  playlist: CoursePlaylist | null;
}
export interface UnassignedVideo {
  id: string;
  youtube_video_id: string;
  title: string;
  thumbnail: string;
  position: number;
  ingest_status: IngestStatus;
}
export interface AdminCourseTree extends CourseTree {
  unassigned_videos: UnassignedVideo[];
}

async function req<T>(token: string, method: string, path: string, body?: unknown): Promise<T> {
  const r = await check(await fetch(`${API}${path}`, {
    method, headers: headers(token), body: body === undefined ? undefined : JSON.stringify(body),
  }));
  return r.json();
}

// ---------------------------------------------------------------- gate
export const amICourseAdmin = (token: string | null) =>
  fetch(`${API}/admin/course/is_admin`, { headers: headers(token) }).then((r) => r.json()) as Promise<{ is_admin: boolean }>;

// ---------------------------------------------------------------- courses
export const listCourses = (token: string) => req<{ courses: Course[] }>(token, "GET", "/admin/course/courses").then((r) => r.courses);
export const createCourse = (token: string, title: string, description = "") =>
  req<Course>(token, "POST", "/admin/course/courses", { title, description });
export const getAdminCourse = (token: string, id: string) => req<AdminCourseTree>(token, "GET", `/admin/course/courses/${id}`);
export const updateCourse = (token: string, id: string, title: string, description: string) =>
  req<Course>(token, "PATCH", `/admin/course/courses/${id}`, { title, description });
export const deleteCourse = (token: string, id: string) => req<{ ok: true }>(token, "DELETE", `/admin/course/courses/${id}`);
export const publishCourse = (token: string, id: string) => req<Course>(token, "POST", `/admin/course/courses/${id}/publish`);
export const unpublishCourse = (token: string, id: string) => req<Course>(token, "POST", `/admin/course/courses/${id}/unpublish`);

// ---------------------------------------------------------------- weeks / lessons
export const addWeek = (token: string, courseId: string, title: string) =>
  req<CourseWeek>(token, "POST", `/admin/course/courses/${courseId}/weeks`, { title });
export const renameWeek = (token: string, weekId: string, title: string) =>
  req<{ ok: true }>(token, "PATCH", `/admin/course/weeks/${weekId}`, { title });
export const deleteWeek = (token: string, weekId: string) => req<{ ok: true }>(token, "DELETE", `/admin/course/weeks/${weekId}`);
export const reorderWeeks = (token: string, courseId: string, ids: string[]) =>
  req<{ ok: true }>(token, "POST", `/admin/course/courses/${courseId}/weeks/reorder`, { ids });

export const addLesson = (token: string, weekId: string, title: string) =>
  req<CourseLesson>(token, "POST", `/admin/course/weeks/${weekId}/lessons`, { title });
export const renameLesson = (token: string, lessonId: string, title: string) =>
  req<{ ok: true }>(token, "PATCH", `/admin/course/lessons/${lessonId}`, { title });
export const deleteLesson = (token: string, lessonId: string) => req<{ ok: true }>(token, "DELETE", `/admin/course/lessons/${lessonId}`);
export const reorderLessons = (token: string, weekId: string, ids: string[]) =>
  req<{ ok: true }>(token, "POST", `/admin/course/weeks/${weekId}/lessons/reorder`, { ids });
export const assignVideo = (token: string, lessonId: string, videoId: string | null) =>
  req<{ ok: true }>(token, "POST", `/admin/course/lessons/${lessonId}/video`, { video_id: videoId });

// ---------------------------------------------------------------- bulk syllabus
export interface SyllabusWeekInput { title: string; lessons: string[] }
export const bulkSyllabus = (token: string, courseId: string, weeks: SyllabusWeekInput[]) =>
  req<{ weeks: (CourseWeek & { lessons: CourseLesson[] })[] }>(token, "POST", `/admin/course/courses/${courseId}/syllabus/bulk`, { weeks });

/** Fallback for pasted text the plain regex parser (syllabus.ts's parseSyllabus) can't make
 * sense of — routes the raw text through the local LLM (same one the Tutor's chat already
 * uses; no new key) to infer week/lesson structure. Same review-before-save shape either way. */
export const aiParseSyllabus = (token: string, text: string) =>
  req<{ weeks: SyllabusWeekInput[] }>(token, "POST", "/admin/course/syllabus/ai_parse", { text }).then((r) => r.weeks);

// ---------------------------------------------------------------- playlist
export const connectPlaylist = (token: string, courseId: string, url: string) =>
  req<{ playlist: CoursePlaylist; sync: { added: number; updated: number; removed: string[]; total: number } }>(
    token, "POST", `/admin/course/courses/${courseId}/playlist`, { url },
  );
export const syncPlaylist = (token: string, courseId: string) =>
  req<{ sync: { added: number; updated: number; removed: string[]; total: number } }>(
    token, "POST", `/admin/course/courses/${courseId}/playlist/sync`,
  );
export interface PlaylistVideo { id: string; youtube_video_id: string; title: string; thumbnail: string; position: number; ingest_status: IngestStatus }
export const listPlaylistVideos = (token: string, courseId: string) =>
  req<{ videos: PlaylistVideo[] }>(token, "GET", `/admin/course/courses/${courseId}/playlist/videos`).then((r) => r.videos);

// ---------------------------------------------------------------- student
export async function getPublishedCourse(): Promise<CourseTree | null> {
  const r = await fetch(`${API}/course`);
  if (!r.ok) return null;  // a failed fetch is "no course to show", not a crash — see courseToVideos
  return r.json();
}

/** Reshapes the published course into the flat Video[] the existing Curriculum/VideoPlayer
 * components already know how to render — the point being that neither of those components
 * needed to change to support a dynamic, admin-authored syllabus. `week` is the week's
 * position (1-based): the existing Curriculum.tsx groups by number, not by a custom title,
 * matching the master prompt's own example output ("Week 1", "Week 2", ...).
 *
 * `id` is the corpus video id (e.g. "yt-<youtubeId>"), not the lesson id: every backend
 * lookup keyed by video — transcripts, captions, citation jumps from the Tutor's answers —
 * is keyed by the corpus id, so using the lesson id here would make captions and
 * citation-jumps silently never resolve. Falls back to the lesson id only while there is no
 * corpus id yet (not assigned, or still ingesting) — fine, since there's nothing to look up
 * either way until ingestion reaches "ready". */
export function courseToVideos(course: CourseTree): import("./api").Video[] {
  return course.weeks.flatMap((w, wi) =>
    w.lessons.map((l) => ({
      id: l.video_corpus_id || l.id,
      title: l.title,
      source: "youtube" as const,
      youtube_id: l.video_youtube_id,
      url: null,
      duration: l.video_duration_s,
      lang: null,
      week: wi + 1,
      unassigned: !l.video_youtube_id,
    })),
  );
}
