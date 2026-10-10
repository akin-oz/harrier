import { KindFrame } from "./KindFrame";
import { useKindEditor } from "./useKindEditor";
import type { ConfigEntry } from "./useKindEditor";

interface HoldRow {
  // The row's own identity while it is edited, so React keeps each field with
  // its row when a row above is removed. Never saved (review of PR #214).
  id: number;
  company: string;
  until: string;
}

let lastRowId = 0;

function nextRowId(): number {
  lastRowId += 1;
  return lastRowId;
}

function toRows(value: unknown): HoldRow[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item: unknown): HoldRow[] => {
    if (typeof item === "string") return [{ id: nextRowId(), company: item, until: "" }];
    if (typeof item === "object" && item !== null && "company" in item) {
      const company = typeof item.company === "string" ? item.company : "";
      const until =
        "hold_until" in item && typeof item.hold_until === "string" ? item.hold_until : "";
      return [{ id: nextRowId(), company, until }];
    }
    return [];
  });
}

// The store's two shapes: a bare name holds with no end, an object carries
// the last day (spec 052). Blank rows are dropped; the store refuses a
// blank company in an object, so none is sent.
function fromRows(rows: readonly HoldRow[]): unknown {
  return rows
    .filter((row) => row.company.trim() !== "")
    .map((row) =>
      row.until.trim() === ""
        ? row.company.trim()
        : { company: row.company.trim(), hold_until: row.until.trim() },
    );
}

function localToday(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${String(now.getFullYear())}-${month}-${day}`;
}

/**
 * The company holds: one row per company, with an optional last day.
 *
 * Expiry is decided when holds are read, never written (spec 052), so an
 * expired hold is still stored and is shown as expired rather than dropped.
 * A hold applies on its last day and lapses the day after, the rule
 * `harrier.userconfig.store.hold_is_active` states; the date compared is
 * this machine's, which is the machine discovery runs on.
 */
export function HoldsEditor({
  entry,
  today = localToday(),
}: {
  entry: ConfigEntry;
  today?: string;
}) {
  const editor = useKindEditor(entry, toRows, fromRows);
  const rows = editor.draft;

  function update(index: number, change: Partial<HoldRow>): void {
    editor.setDraft(rows.map((row, at) => (at === index ? { ...row, ...change } : row)));
  }

  return (
    <KindFrame
      entry={editor.current}
      title="Company holds"
      description="Companies discovery skips. A hold with a date applies through that day and lapses after it."
      notice={editor.notice}
      save={editor.save}
      reset={editor.reset}
    >
      {rows.length === 0 ? (
        <p className="config-field__help">No company is on hold.</p>
      ) : (
        <table className="config-holds">
          <thead>
            <tr>
              <th scope="col">Company</th>
              <th scope="col">Last day</th>
              <th scope="col">
                <span className="config-visually-hidden">State</span>
              </th>
              <th scope="col">
                <span className="config-visually-hidden">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => {
              const expired = row.until !== "" && row.until < today;
              const label = row.company.trim() || `row ${String(index + 1)}`;
              return (
                <tr key={row.id} className={expired ? "config-holds__expired" : undefined}>
                  <td>
                    <input
                      className="config-field__text"
                      aria-label={`Company, ${label}`}
                      value={row.company}
                      onChange={(event) => {
                        update(index, { company: event.target.value });
                      }}
                    />
                  </td>
                  <td>
                    <input
                      className="config-field__date"
                      type="date"
                      aria-label={`Last day, ${label}`}
                      value={row.until}
                      onChange={(event) => {
                        update(index, { until: event.target.value });
                      }}
                    />
                  </td>
                  <td>
                    {expired ? (
                      <span className="config-hold-state config-hold-state--expired">Expired</span>
                    ) : (
                      <span className="config-hold-state">
                        {row.until === "" ? "No end" : "Active"}
                      </span>
                    )}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="config-button config-button--quiet"
                      aria-label={`Remove ${label}`}
                      onClick={() => {
                        editor.setDraft(rows.filter((_, at) => at !== index));
                      }}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <div>
        <button
          type="button"
          className="config-button"
          onClick={() => {
            editor.setDraft([...rows, { id: nextRowId(), company: "", until: "" }]);
          }}
        >
          Add a company
        </button>
      </div>
    </KindFrame>
  );
}
