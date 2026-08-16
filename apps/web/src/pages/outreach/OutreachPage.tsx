import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import "./OutreachPage.css";

type OutreachRow = components["schemas"]["OutreachRowOut"];
type Candidate = components["schemas"]["CandidateOut"];
type Contact = components["schemas"]["ContactOut"];

function refused(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

/**
 * One place that turns a client result into a value or a message.
 *
 * The refusal responses here declare a status and no body, so the generated
 * `error` is `never` while `data` becomes optional. Both still happen at
 * runtime: FastAPI sends `{"detail": ...}` with a 403 or 404. Taking a
 * widened result rather than repeating two half-checks per call is what
 * keeps that difference from being handled three slightly different ways.
 */
function unwrap<T>(result: { data?: T; error?: unknown }, fallback: string): T {
  if (result.error !== undefined) throw new Error(refused(result.error, fallback));
  if (result.data === undefined) throw new Error(fallback);
  return result.data;
}

async function fetchDue(): Promise<readonly OutreachRow[]> {
  return unwrap(await api.GET("/outreach/due", {}), "could not load the due queue");
}

async function fetchContacts(): Promise<readonly Contact[]> {
  return unwrap(await api.GET("/outreach/contacts", {}), "could not load contacts");
}

async function fetchCandidates(selector: string): Promise<readonly Candidate[]> {
  return unwrap(
    await api.GET("/outreach/{selector}/candidates", { params: { path: { selector } } }),
    "could not load candidates",
  );
}

export function OutreachPage() {
  const queryClient = useQueryClient();
  const [selector, setSelector] = useState("");
  // What the query actually keys on. Typing "1234" into the box used to
  // issue four requests, one per keystroke, and each of them read the
  // tracker and the staged artifact on the server (review finding on PR #51).
  const [settled, setSettled] = useState("");
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    const timer = globalThis.setTimeout(() => {
      setSettled(selector);
    }, 300);
    return () => {
      globalThis.clearTimeout(timer);
    };
  }, [selector]);

  const due = useQuery({ queryKey: ["outreach", "due"], queryFn: fetchDue });
  const contacts = useQuery({ queryKey: ["outreach", "contacts"], queryFn: fetchContacts });
  const candidates = useQuery({
    queryKey: ["outreach", "candidates", settled],
    queryFn: () => fetchCandidates(settled),
    enabled: settled !== "",
  });

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ["outreach"] });
  };

  const mark = useMutation({
    mutationFn: async ({ id, what }: { id: number; what: "sent" | "replied" }) => {
      const path = what === "sent" ? "/outreach/{selector}/sent" : "/outreach/{selector}/replied";
      const { error } = await api.POST(path, {
        params: { path: { selector: String(id) } },
        body: { date: null },
      });
      if (error !== undefined) throw new Error(refused(error, `could not mark ${what}`));
    },
    onSuccess: refreshAll,
    onError: (error: Error) => {
      setFailure(error.message);
    },
  });

  const snooze = useMutation({
    mutationFn: async ({ id, until }: { id: number; until: string }) => {
      const { error } = await api.POST("/outreach/{selector}/snooze", {
        params: { path: { selector: String(id) } },
        body: { until },
      });
      if (error !== undefined) throw new Error(refused(error, "could not snooze"));
    },
    onSuccess: refreshAll,
    onError: (error: Error) => {
      setFailure(error.message);
    },
  });

  const decide = useMutation({
    mutationFn: async ({ url, approve }: { url: string; approve: boolean }) => {
      const path = approve
        ? "/outreach/{selector}/candidates/approve"
        : "/outreach/{selector}/candidates/reject";
      // `settled`, not `selector`: the rows on screen belong to the settled
      // value, and during the debounce window the two differ. Sending the raw
      // value applied a decision about one job to another (review finding on
      // PR #51).
      const { error } = await api.POST(path, {
        params: { path: { selector: settled } },
        body: { linkedin_url: url },
      });
      if (error !== undefined) throw new Error(refused(error, "could not record that decision"));
    },
    onSuccess: refreshAll,
    onError: (error: Error) => {
      setFailure(error.message);
    },
  });

  const findContacts = useMutation({
    mutationFn: async () => {
      const { error } = await api.POST("/outreach/{selector}/find-contacts", {
        params: { path: { selector: settled } },
        body: { best_only: false, max_items: null },
      });
      if (error !== undefined) throw new Error(refused(error, "could not start contact discovery"));
    },
    onSuccess: refreshAll,
    onError: (error: Error) => {
      setFailure(error.message);
    },
  });

  return (
    <section className="outreach-page">
      <h2 className="outreach-page__heading">Outreach</h2>
      <p className="outreach-page__note">
        Nothing here sends anything. Drafts are written for you to send yourself, and “Mark sent”
        records that you already did.
      </p>

      {failure !== null && (
        <p role="alert" className="outreach-page__error">
          {failure}
        </p>
      )}

      {/* Due */}
      <h3 className="outreach-page__subheading">Due</h3>
      {due.isPending && <p className="outreach-page__muted">Loading the due queue…</p>}
      {due.isError && (
        <p role="alert" className="outreach-page__error">
          {due.error.message}
        </p>
      )}
      {due.isSuccess &&
        (due.data.length === 0 ? (
          <p className="outreach-page__muted">Nothing is due.</p>
        ) : (
          <table className="outreach-table">
            <thead>
              <tr>
                <th>Company</th>
                <th>Role</th>
                <th>Next action</th>
                <th>Best contact</th>
                <th>Record</th>
              </tr>
            </thead>
            <tbody>
              {due.data.map((row) => (
                <tr key={row.id}>
                  <td>{row.company}</td>
                  <td>{row.title}</td>
                  <td>{row.next_outreach_action}</td>
                  <td>{row.best_contact_name || "none"}</td>
                  <td className="outreach-table__actions">
                    <button
                      type="button"
                      onClick={() => {
                        setFailure(null);
                        mark.mutate({ id: row.id, what: "sent" });
                      }}
                    >
                      Mark sent
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setFailure(null);
                        mark.mutate({ id: row.id, what: "replied" });
                      }}
                    >
                      Mark replied
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setFailure(null);
                        const until = globalThis.prompt("Snooze until (YYYY-MM-DD)");
                        if (until !== null && until !== "") {
                          snooze.mutate({ id: row.id, until });
                        }
                      }}
                    >
                      Snooze
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}

      {/* Candidates */}
      <h3 className="outreach-page__subheading">Candidates</h3>
      <div className="outreach-page__row">
        <label className="outreach-page__field">
          <span>Job id</span>
          <input
            value={selector}
            onChange={(event) => {
              setSelector(event.target.value);
            }}
            placeholder="e.g. 12"
          />
        </label>
        <button
          type="button"
          disabled={settled === "" || settled !== selector || findContacts.isPending}
          onClick={() => {
            setFailure(null);
            findContacts.mutate();
          }}
        >
          Find contacts
        </button>
        {/* Marked, because it is not free: this reaches a paid service. */}
        <span className="outreach-page__paid">calls a paid service (Hunter, Apify)</span>
      </div>

      {/* An unknown job id answers 404, and this used to render nothing at
          all: the operator typed an id, the page went quiet, and the reason
          never reached them. Spec 048 requires the refusal to be visible. */}
      {settled === selector && settled !== "" && candidates.isError && (
        <p role="alert" className="outreach-page__error">
          {candidates.error.message}
        </p>
      )}

      {/* Nothing actionable is shown while the typed value and the settled
          one disagree, so a decision cannot land on the row of a job the
          operator has already navigated away from. */}
      {settled === selector && settled !== "" && candidates.isSuccess && (
        <>
          <p className="outreach-page__muted">
            Staged for your decision. Approving is what creates a contact; nothing else does.
          </p>
          {candidates.data.length === 0 ? (
            <p className="outreach-page__muted">No candidates found for this job.</p>
          ) : (
            <table className="outreach-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Title</th>
                  <th>Relevance</th>
                  <th>Fit</th>
                  <th>Status</th>
                  <th>Decide</th>
                </tr>
              </thead>
              <tbody>
                {candidates.data.map((candidate) => (
                  <tr key={candidate.linkedin_url}>
                    <td>{candidate.person_name}</td>
                    <td>{candidate.person_title}</td>
                    <td>{candidate.relevance}</td>
                    <td>{candidate.fit_score}</td>
                    <td>{candidate.review_status || "pending"}</td>
                    <td className="outreach-table__actions">
                      <button
                        type="button"
                        onClick={() => {
                          setFailure(null);
                          decide.mutate({ url: candidate.linkedin_url, approve: true });
                        }}
                      >
                        Approve
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setFailure(null);
                          decide.mutate({ url: candidate.linkedin_url, approve: false });
                        }}
                      >
                        Reject
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

      {/* Contacts */}
      <h3 className="outreach-page__subheading">Contacts</h3>
      {contacts.isPending && <p className="outreach-page__muted">Loading contacts…</p>}
      {contacts.isError && (
        <p role="alert" className="outreach-page__error">
          {contacts.error.message}
        </p>
      )}
      {contacts.isSuccess &&
        (contacts.data.length === 0 ? (
          <p className="outreach-page__muted">No contacts approved yet.</p>
        ) : (
          <table className="outreach-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Title</th>
                <th>Company</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {contacts.data.map((contact) => (
                <tr key={contact.id}>
                  <td>{contact.person_name}</td>
                  <td>{contact.person_title}</td>
                  <td>{contact.company}</td>
                  <td>{contact.contact_status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </section>
  );
}
