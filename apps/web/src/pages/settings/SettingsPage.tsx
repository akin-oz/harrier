import { useQuery } from "@tanstack/react-query";

import type { components } from "@harrier/contract";

import { ConfigSection } from "../../features/config";
import { CommandList, HostPanel } from "../../features/host-commands";
import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import { appMode } from "../../shared/config/appMode";
import { BackupsSection } from "./BackupsSection";
import { CredentialsSection } from "./CredentialsSection";
import { ExportSection } from "./ExportSection";
import { ScheduleChoiceSection } from "./ScheduleChoiceSection";
import { TelegramSection } from "./TelegramSection";
import "./SettingsPage.css";

type ProfileDocument = components["schemas"]["ProfileDocumentOut"];

async function fetchProfile(): Promise<readonly ProfileDocument[]> {
  // Spec 050's route, which spec 096 reuses rather than repeating.
  const { data, error } = await api.GET("/ops/profile");
  if (error !== undefined) {
    throw new Error(refusalMessage(error, "could not list the profile documents"));
  }
  return data;
}

/**
 * Settings (spec 096): configuration, profile documents, backups, what only
 * the host can do, and where every command has its place.
 *
 * Everything here belongs to the install rather than to a search track, so
 * the page is the same on every track.
 */
export function SettingsPage() {
  // Hosted only: locally credentials and Telegram live in `.env`, and the
  // routes answer 404 (spec 114, amendment of 2026-10-11).
  const hosted = appMode().kind === "hosted";
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

      {hosted && (
        <>
          <section className="settings-section" aria-labelledby="settings-credentials">
            <h3 id="settings-credentials" className="settings-section__heading">
              Credentials
            </h3>
            <CredentialsSection />
          </section>

          <section className="settings-section" aria-labelledby="settings-schedule">
            <h3 id="settings-schedule" className="settings-section__heading">
              Schedule
            </h3>
            <ScheduleChoiceSection />
          </section>

          <section className="settings-section" aria-labelledby="settings-telegram">
            <h3 id="settings-telegram" className="settings-section__heading">
              Telegram notifications
            </h3>
            <TelegramSection />
          </section>
        </>
      )}

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
        <BackupsSection />
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
