import { useId } from "react";

import { KindFrame } from "./KindFrame";
import { useKindEditor } from "./useKindEditor";
import type { ConfigEntry } from "./useKindEditor";

const COUNT = "apify_scheduled_count";

// The bound `harrier.discovery.APIFY_MAX_COUNT` applies where the count is
// read (spec 035). The store accepts any object for this kind and clamps on
// read, so the editor states the bound rather than letting a larger number
// look as though it will be used.
export const APIFY_MIN_COUNT = 1;
export const APIFY_MAX_COUNT = 500;

interface DiscoveryDraft {
  count: string;
  // Every other setting, kept as it is. A key beginning with `_` is a note
  // for a reader of the file, not a setting, and `harrier config import`
  // drops it for the same reason.
  others: Record<string, unknown>;
}

function toDraft(value: unknown): DiscoveryDraft {
  const settings =
    typeof value === "object" && value !== null && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  const others: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(settings)) {
    if (key !== COUNT && !key.startsWith("_")) others[key] = item;
  }
  const count = settings[COUNT];
  return { count: typeof count === "number" ? String(count) : "", others };
}

function fromDraft(draft: DiscoveryDraft): unknown {
  const text = draft.count.trim();
  return text === "" ? { ...draft.others } : { ...draft.others, [COUNT]: Number(text) };
}

function problem(count: string): string | null {
  const text = count.trim();
  if (text === "") return null;
  const value = Number(text);
  if (!Number.isInteger(value) || value < APIFY_MIN_COUNT || value > APIFY_MAX_COUNT) {
    return `The count is a whole number from ${String(APIFY_MIN_COUNT)} to ${String(APIFY_MAX_COUNT)}.`;
  }
  return null;
}

/** The discovery settings: the scheduled Apify count, bounded (spec 035). */
export function DiscoveryEditor({ entry }: { entry: ConfigEntry }) {
  const fieldId = useId();
  const helpId = useId();
  const editor = useKindEditor(entry, toDraft, fromDraft);
  const invalid = problem(editor.draft.count);
  const kept = Object.keys(editor.draft.others);

  return (
    <KindFrame
      entry={editor.current}
      title="Discovery settings"
      description="How many results a scheduled run asks each LinkedIn search for. Empty uses the default."
      notice={editor.notice}
      save={editor.save}
      reset={editor.reset}
      blocked={invalid}
    >
      <div className="config-field">
        <label className="config-field__label" htmlFor={fieldId}>
          Scheduled Apify count per search
        </label>
        <input
          id={fieldId}
          className="config-field__number"
          type="number"
          inputMode="numeric"
          min={APIFY_MIN_COUNT}
          max={APIFY_MAX_COUNT}
          step={1}
          value={editor.draft.count}
          aria-invalid={invalid !== null}
          aria-describedby={helpId}
          onChange={(event) => {
            editor.setDraft({ ...editor.draft, count: event.target.value });
          }}
        />
        <span id={helpId} className="config-field__help">
          From {APIFY_MIN_COUNT} to {APIFY_MAX_COUNT}. A larger stored number is cut to{" "}
          {APIFY_MAX_COUNT} when a run reads it.
        </span>
      </div>
      {kept.length > 0 && (
        <p className="config-field__help">Kept as they are: {kept.join(", ")}.</p>
      )}
    </KindFrame>
  );
}
