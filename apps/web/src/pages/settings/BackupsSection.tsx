import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import type { components } from "@harrier/contract";

import { TERMINAL_STATES, useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory, RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";

type Backups = components["schemas"]["BackupsOut"];
type Archive = components["schemas"]["ArchiveOut"];

const BACKUPS_KEY = ["settings", "backups"] as const;

const VERIFICATION: Record<NonNullable<Archive["verification"]>, string> = {
  passed: "Verified",
  failed: "Failed verification",
  running: "Verifying",
};

async function fetchBackups(): Promise<Backups> {
  // The list opens no database, so its one declared refusal is the token's,
  // which has no body: no body is that refusal, said as such.
  const { data } = await api.GET("/settings/backups");
  if (data === undefined) throw new Error("refused: the local API token was not accepted");
  return data;
}

function size(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

/**
 * The archives in the backups directory the container mounts, newest first,
 * with "Take a backup" and a verify per archive (spec 096). Both are runs of
 * the CLI's own verbs; the archive is named, never located. `restore` has no
 * button: it replaces the live database (spec 042).
 */
export function BackupsSection({ createEventSource }: { createEventSource: EventSourceFactory }) {
  const queryClient = useQueryClient();
  const backups = useQuery({ queryKey: BACKUPS_KEY, queryFn: fetchBackups });
  const stream = useRunStream(createEventSource);
  const { run, lastLogLine, failed, active } = stream;

  useEffect(() => {
    if (run !== null && TERMINAL_STATES.has(run.state)) {
      void queryClient.invalidateQueries({ queryKey: BACKUPS_KEY });
    }
  }, [run, queryClient]);

  // Each action clears the other's refusal when it starts, so what is on
  // screen is the current action's answer (review of PR #214).
  const take = useMutation({
    onMutate: () => {
      verify.reset();
    },
    mutationFn: async (): Promise<RunOut> => {
      // Spec 050's route. An empty body takes an archive and deletes none.
      const { data, error } = await api.POST("/ops/backup", { body: {} });
      // A run conflict (409) or a held database (503) in the server's words;
      // only an answer with no body at all is the token refusal.
      if (error !== undefined) throw new Error(refusalMessage(error, "no backup was started"));
      if (data === undefined) throw new Error("refused: the local API token was not accepted");
      return data;
    },
    onSuccess: (started) => {
      stream.begin(started);
      void queryClient.invalidateQueries({ queryKey: BACKUPS_KEY });
    },
  });

  const verify = useMutation({
    onMutate: () => {
      take.reset();
    },
    mutationFn: async (name: string): Promise<RunOut> => {
      const { data, error } = await api.POST("/settings/backups/{name}/verify", {
        params: { path: { name } },
      });
      if (error !== undefined)
        throw new Error(refusalMessage(error, "the archive was not verified"));
      if (data === undefined) throw new Error("refused: the local API token was not accepted");
      return data;
    },
    onSuccess: (started) => {
      stream.begin(started);
      void queryClient.invalidateQueries({ queryKey: BACKUPS_KEY });
    },
  });

  const busy = active || take.isPending || verify.isPending;
  const startError = take.error ?? verify.error;

  return (
    <div className="settings-backups">
      <div className="settings-backups__toolbar">
        <button
          type="button"
          className="settings-button"
          disabled={busy}
          onClick={() => {
            take.mutate();
          }}
        >
          Take a backup
        </button>
        <span className="settings-muted">Writes a verified archive and deletes no older one.</span>
        {run !== null && (
          <span className="settings-backups__run">
            {run.kind === "backup" ? "Backup" : "Verification"}: <strong>{run.state}</strong>
          </span>
        )}
      </div>
      {startError !== null && (
        <p role="alert" className="settings-error">
          {startError.message}
        </p>
      )}
      {run !== null && TERMINAL_STATES.has(run.state) && lastLogLine !== null && (
        <p
          role={failed ? "alert" : "status"}
          className={failed ? "settings-refusal" : "settings-result"}
        >
          {/* A backup that fails verification leaves no archive behind
              (spec 030), and the page says so rather than leaving the
              operator to infer it (spec 050). */}
          {failed && run.kind === "backup" && "The backup failed and no archive was written. "}
          {lastLogLine}
        </p>
      )}
      {backups.isPending && <p className="settings-muted">Listing the backups…</p>}
      {backups.isError && (
        <p role="alert" className="settings-error">
          {backups.error.message}
        </p>
      )}
      {backups.isSuccess && (
        <ArchiveTable
          backups={backups.data}
          busy={busy}
          onVerify={(name) => {
            verify.mutate(name);
          }}
        />
      )}
    </div>
  );
}

function ArchiveTable({
  backups,
  busy,
  onVerify,
}: {
  backups: Backups;
  busy: boolean;
  onVerify: (name: string) => void;
}) {
  if (backups.directory === "absent") {
    return (
      <p className="settings-empty">
        The backups directory is not mounted here, or no backup has created it yet. Taking a backup
        creates it.
      </p>
    );
  }
  if (backups.archives.length === 0) {
    return <p className="settings-empty">The backups directory holds no archives yet.</p>;
  }
  return (
    <table className="settings-table">
      <thead>
        <tr>
          <th scope="col">Archive</th>
          <th scope="col">Size</th>
          <th scope="col">Written</th>
          <th scope="col">Verification</th>
          <th scope="col">
            <span className="settings-visually-hidden">Verify</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {backups.archives.map((archive) => (
          <tr key={archive.name}>
            <td>
              <code>{archive.name}</code>
            </td>
            <td className="settings-table__number">{size(archive.size_bytes)}</td>
            <td className="settings-table__muted">
              {archive.modified_at.slice(0, 16).replace("T", " ")}
            </td>
            <td>
              {archive.verification === null ? (
                <span className="settings-table__muted">Not verified here</span>
              ) : (
                <span
                  className={`settings-verification settings-verification--${archive.verification}`}
                >
                  {VERIFICATION[archive.verification]}
                </span>
              )}
            </td>
            <td>
              <button
                type="button"
                className="settings-button"
                disabled={busy}
                aria-label={`Verify ${archive.name}`}
                onClick={() => {
                  onVerify(archive.name);
                }}
              >
                Verify
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
