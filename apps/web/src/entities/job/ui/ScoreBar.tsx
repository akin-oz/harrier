import "./ScoreBar.css";

// A bare number answers "what is this score" but not "which of these is
// best", which is the question the column exists for. The bar makes rows
// comparable at a glance and the tick marks the cutoff a posting had to
// clear to be in the tracker at all.
//
// Both constants are presentation, not contract fields: the API reports a
// score, not the scale or the cutoff. If it ever reports them, read them
// from there (scoring lives in config/candidate.json, so a user who edits
// their weights can move the real cutoff away from this number).
const MAX_SCORE = 120;
const CUTOFF = 55;

export function ScoreBar({ score }: { score: number | null }) {
  if (score === null) {
    return (
      <span className="score-bar score-bar--unknown" aria-label="no score">
        —
      </span>
    );
  }
  const clamped = Math.max(0, Math.min(MAX_SCORE, score));
  const passes = score >= CUTOFF;
  return (
    <span
      className={`score-bar${passes ? " score-bar--pass" : ""}`}
      role="img"
      aria-label={`score ${String(score)} of ${String(MAX_SCORE)}, cutoff ${String(CUTOFF)}`}
    >
      <span className="score-bar__track">
        <span
          className="score-bar__cutoff"
          style={{ left: `${String((CUTOFF / MAX_SCORE) * 100)}%` }}
        />
        <span
          className="score-bar__fill"
          style={{ width: `${String((clamped / MAX_SCORE) * 100)}%` }}
        />
      </span>
      <span className="score-bar__value">{score}</span>
    </span>
  );
}
