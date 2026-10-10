import { useQuery } from "@tanstack/react-query";

import type { components } from "@harrier/contract";

import { ConfigSection } from "../../features/config";
import { CommandList, HostPanel } from "../../features/host-commands";
import type { EventSourceFactory } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import { BackupsSection } from "./BackupsSection";
import { ExportSection } from "./ExportSection";
import "./SettingsPage.css";

type ProfileDocument = components["schemas"]["ProfileDocumentOut"];

async function fetchProfile(): Promise<readonly ProfileDocument[]> {
  // Spec 050's route, which spec 096 reuses rather than repeating.
  const { data, error } = await api.GET("/ops/profile");
  if (error !== undefined) {
    throw new Error(refusalMessage(error, "could not list the profile documents"));
  }
  if (data === undefined) throw new Error("refused: the local API token was not accepted");
  return data;
}

/**
 * Settings (spec 096): configuration, profile documents, backups, what only
 * the host can do, and where every command has its place.
 *
 * Everything here belongs to the install rather than to a search track, so
 * the page is the same on every track.
 */
export function SettingsPage({
  createEventSource = (url: string) => new EventSource(url),
}: {
  createEventSource?: EventSourceFactory;
} = {}) {
  const profile = useQuery({ queryKey: ["settings", "profile"], queryFn: fetchProfile });

  return (
    <div className="settings-page">
      <h2 className="settings-page__heading">Settings</h2>
      <p className="settings-page__lead">
        These settings belong to this install, not to a search track. Commands that only the host
        can run are shown with the line to copy; nothing here runs them.
      </p>

      <section className="settings-section" aria-labelledby="settings-config">
        <h3 id="settings-config" className="settings-section__heading">
          Configuration
        </h3>
        <ConfigSection />
      </section>

      <section className="settings-section" aria-labelledby="settings-profile">
        <h3 id="settings-profile" className="settings-section__heading">
          Profile documents
        </h3>
        <p className="settings-muted">
          Listed by name. Their contents are edited on disk: export them on the host, as the panel
          below shows.
        </p>
        {profile.isPending && <p className="settings-muted">Listing the documents…</p>}
        {profile.isError && (
          <p role="alert" className="settings-error">
            {profile.error.message}
          </p>
        )}
        {profile.isSuccess &&
          (profile.data.length === 0 ? (
            <p className="settings-empty">No profile documents are stored.</p>
          ) : (
            <table className="settings-table">
              <thead>
                <tr>
                  <th scope="col">Kind</th>
                  <th scope="col">Name</th>
                  <th scope="col">Format</th>
                  <th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {profile.data.map((document) => (
                  <tr key={`${document.kind}/${document.name}`}>
                    <td>{document.kind}</td>
                    <td>
                      <code>{document.name}</code>
                    </td>
                    <td className="settings-table__muted">{document.format}</td>
                    <td className="settings-table__muted">{document.updated_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
      </section>

      <section className="settings-section" aria-labelledby="settings-backups">
        <h3 id="settings-backups" className="settings-section__heading">
          Backups
        </h3>
        <BackupsSection createEventSource={createEventSource} />
        <p className="settings-muted">
          Restoring an archive stays in the terminal: it replaces the live database.
        </p>
      </section>

      <section className="settings-section" aria-labelledby="settings-export">
        <h3 id="settings-export" className="settings-section__heading">
          Export
        </h3>
        <ExportSection />
      </section>

      <section className="settings-section" aria-labelledby="settings-host">
        <h3 id="settings-host" className="settings-section__heading">
          What only the host can do
        </h3>
        <p className="settings-muted">
          What the container can see, beside the command that would change it. Copy a command and
          run it in a terminal on the host.
        </p>
        <HostPanel profileCount={profile.data?.length ?? null} />
      </section>

      <section className="settings-section" aria-labelledby="settings-commands">
        <h3 id="settings-commands" className="settings-section__heading">
          Commands without a button
        </h3>
        <CommandList />
      </section>
    </div>
  );
}
