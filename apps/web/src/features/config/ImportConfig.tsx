import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import { CONFIG_KEY } from "./useKindEditor";

/**
 * `harrier config import` as one button (spec 096). It overwrites every
 * stored kind that has a file in the checkout, so it asks first and says
 * that, and it reports what it imported in the command's own lines.
 */
export function ImportConfig({ onImported }: { onImported: () => void }) {
  const queryClient = useQueryClient();
  const [asking, setAsking] = useState(false);
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const openerRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (asking) cancelRef.current?.focus();
  }, [asking]);

  const run = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/config/import");
      if (error !== undefined) throw new Error(refusalMessage(error, "nothing was imported"));
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: CONFIG_KEY });
      onImported();
    },
  });

  function cancel(): void {
    setAsking(false);
    openerRef.current?.focus();
  }

  function onKey(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key !== "Escape") return;
    event.preventDefault();
    cancel();
  }

  return (
    <div className="config-import">
      {asking ? (
        <div
          className="config-kind__confirm"
          role="group"
          aria-label="Import the configuration files"
          onKeyDown={onKey}
        >
          <p className="config-kind__question">
            Import the files in the checkout? Each stored kind that has a file is overwritten with
            the file&apos;s value.
          </p>
          <div className="config-kind__actions">
            <button
              type="button"
              className="config-button config-button--danger"
              disabled={run.isPending}
              onClick={() => {
                setAsking(false);
                run.mutate();
              }}
            >
              Overwrite from the files
            </button>
            <button ref={cancelRef} type="button" className="config-button" onClick={cancel}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          ref={openerRef}
          type="button"
          className="config-button"
          disabled={run.isPending}
          onClick={() => {
            run.reset();
            setAsking(true);
          }}
        >
          Import from the files
        </button>
      )}
      {run.error !== null && (
        <p role="alert" className="config-kind__refusal">
          {run.error.message}
        </p>
      )}
      <div role="status" className="config-import__report">
        {run.data !== undefined && (
          <>
            <ul className="config-import__lines">
              {run.data.report.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <p>
              Imported {run.data.imported.length} of {run.data.total} kinds.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
