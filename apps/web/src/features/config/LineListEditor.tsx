import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import { KindFrame } from "./KindFrame";
import { useKindEditor } from "./useKindEditor";
import type { ConfigEntry } from "./useKindEditor";

function toText(value: unknown): string {
  return Array.isArray(value) ? value.filter((line) => typeof line === "string").join("\n") : "";
}

function toLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

async function routeFeeds(urls: readonly string[]) {
  const { data, error } = await api.POST("/settings/feeds/routing", { body: { urls: [...urls] } });
  if (error !== undefined) throw new Error(refusalMessage(error, "could not check the watchlist"));
  if (data === undefined) throw new Error("refused: the local API token was not accepted");
  return data.unrouted;
}

/**
 * The board watchlist or the LinkedIn searches: one URL per line.
 *
 * For the watchlist, a line no importer handles is named in spec 041's
 * words, which come from the server's own router, and it does not block the
 * save: discovery skips such a line and says so, and the operator may be
 * keeping it on purpose.
 */
export function LineListEditor({
  entry,
  title,
  description,
}: {
  entry: ConfigEntry;
  title: string;
  description: string;
}) {
  const fieldId = useId();
  const editor = useKindEditor(entry, toText, toLines);
  const routed = entry.kind === "feeds";
  // Checked when the field is left, not on every keystroke.
  const [checked, setChecked] = useState<readonly string[]>(() => toLines(toText(entry.value)));
  const routing = useQuery({
    queryKey: ["config", "feeds", "routing", checked],
    queryFn: () => routeFeeds(checked),
    enabled: routed && checked.length > 0,
  });

  return (
    <KindFrame
      entry={editor.current}
      title={title}
      description={description}
      notice={editor.notice}
      save={editor.save}
      reset={editor.reset}
    >
      <label className="config-field" htmlFor={fieldId}>
        <span className="config-field__label">
          <span className="config-visually-hidden">{title}: </span>One URL per line
        </span>
        <textarea
          id={fieldId}
          className="config-field__lines"
          rows={6}
          spellCheck={false}
          value={editor.draft}
          onChange={(event) => {
            editor.setDraft(event.target.value);
          }}
          onBlur={() => {
            setChecked(toLines(editor.draft));
          }}
        />
      </label>
      {routed && routing.data !== undefined && routing.data.length > 0 && (
        <div role="status" className="config-unrouted">
          <p className="config-unrouted__lead">
            Discovery skips these lines. They can still be saved.
          </p>
          <ul className="config-unrouted__list">
            {routing.data.map((item) => (
              <li key={item.url}>{item.message}</li>
            ))}
          </ul>
        </div>
      )}
    </KindFrame>
  );
}
