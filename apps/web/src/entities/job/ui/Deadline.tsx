import "./Deadline.css";

const DAY_MS = 86_400_000;

function localIsoDate(now: Date): string {
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${String(now.getFullYear())}-${month}-${day}`;
}

// Whole days between two ISO dates, both read as UTC midnight so a daylight
// saving change cannot make a day 23 hours long.
function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(to) - Date.parse(from)) / DAY_MS);
}

function plural(count: number): string {
  return count === 1 ? "1 day" : `${String(count)} days`;
}

/**
 * When a call closes, said in words. Whether it has passed is the server's
 * answer (`deadline_passed`), never this browser's clock, so the table cannot
 * call a deadline open that the queue the server ordered treats as passed
 * (spec 094). The browser's date only counts the days in between, and where
 * the two clocks disagree the server's word wins.
 */
export function Deadline({
  deadline,
  passed,
  now = new Date(),
}: {
  deadline: string;
  passed: boolean;
  now?: Date;
}) {
  if (deadline === "") {
    return <span className="deadline deadline--none">No deadline</span>;
  }
  const days = daysBetween(localIsoDate(now), deadline);
  let phrase: string;
  if (passed) {
    phrase = days < 0 ? `passed ${plural(-days)} ago` : "passed";
  } else if (days <= 0) {
    phrase = "closes today";
  } else {
    phrase = `closes in ${plural(days)}`;
  }
  const tone = passed ? " deadline--passed" : days <= 7 ? " deadline--soon" : "";
  return (
    <span className={`deadline${tone}`}>
      <time className="deadline__date" dateTime={deadline}>
        {deadline}
      </time>
      <span className="deadline__phrase">{phrase}</span>
    </span>
  );
}
