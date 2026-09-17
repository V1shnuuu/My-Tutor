import { useEffect, useState } from "react";
import { ApiError, getQuiz, type Lang, type QuizQuestion } from "../lib/api";
import { t } from "../lib/i18n";

interface Props {
  videoId: string;
  lang: Lang;
  onJump: (seconds: number) => void;
}

const fmt = (s: number) => {
  s = Math.max(0, Math.floor(s));
  const m = Math.floor(s / 60), sec = s % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
};

/** Practice questions generated from this video's own transcript (core/app/quiz.py) — never
 * the model's general knowledge, always traceable back to a real moment in the lecture. */
export default function QuizPanel({ videoId, lang, onJump }: Props) {
  const [questions, setQuestions] = useState<QuizQuestion[] | null>(null);
  const [picked, setPicked] = useState<Record<number, number>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true); setError(null); setQuestions(null); setPicked({});
    getQuiz(videoId)
      .then((qs) => { if (alive) setQuestions(qs); })
      .catch((e) => {
        if (!alive) return;
        setError(e instanceof ApiError && e.code === "quiz_busy" ? t("quiz_busy", lang) : t("quiz_unavailable", lang));
      })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [videoId, lang]);

  if (loading) return <div className="quiz-panel"><p className="muted">{t("quiz_generating", lang)}</p></div>;
  if (error) return <div className="quiz-panel"><p className="muted">{error}</p></div>;
  if (!questions || questions.length === 0) return <div className="quiz-panel"><p className="muted">{t("quiz_unavailable", lang)}</p></div>;

  return (
    <div className="quiz-panel">
      {questions.map((q, qi) => {
        const chosen = picked[qi];
        return (
          <div key={qi} className="quiz-question">
            <p className="quiz-q-text">{qi + 1}. {q.question}</p>
            <div className="quiz-options">
              {q.options.map((opt, oi) => {
                const isChosen = chosen === oi;
                const isCorrect = oi === q.answer_index;
                const cls = chosen == null ? "" : isCorrect ? "correct" : isChosen ? "wrong" : "";
                return (
                  <button key={oi} className={`quiz-option ${cls}`} disabled={chosen != null}
                    onClick={() => setPicked((p) => ({ ...p, [qi]: oi }))}>
                    {opt}
                  </button>
                );
              })}
            </div>
            {chosen != null && (
              <button className="quiz-jump" onClick={() => onJump(q.t)}>▶ {t("jump", lang)} {fmt(q.t)}</button>
            )}
          </div>
        );
      })}
    </div>
  );
}
