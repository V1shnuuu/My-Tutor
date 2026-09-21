import type { Lang } from "../lib/api";
import type { Course } from "../lib/course";
import { t } from "../lib/i18n";

interface Props {
  courses: Course[];
  lang: Lang;
  onPick: (id: string) => void;
}

/** Shown instead of the main app when more than one course is published at once — a student
 * picks which one they're in before the syllabus/chat/video panels load. With zero or exactly
 * one published course, App.tsx never renders this at all (see its bootstrap effect). */
export default function CoursePicker({ courses, lang, onPick }: Props) {
  return (
    <div className="admin-shell course-picker">
      <h1>{t("choose_course", lang)}</h1>
      <ul className="course-picker-list">
        {courses.map((c) => (
          <li key={c.id}>
            <button className="course-picker-item" onClick={() => onPick(c.id)}>
              <span className="title" dir="auto">{c.title}</span>
              {c.description && <span className="desc" dir="auto">{c.description}</span>}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
