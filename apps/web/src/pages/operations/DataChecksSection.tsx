import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import { TOKEN_REFUSED, refusalMessage } from "./RunOutput";

type DataCheck = components["schemas"]["DataCheckOut"];
type Linked = components["schemas"]["LinkContactsOut"];

const KEY = ["ops", "check"] as const;

async function fetchCheck(): Promise<DataCheck> {
  const { data, error } = await api.GET("/ops/check");
  if (error !== undefined) throw new Error(refusalMessage(error, "the data check was refused"));
  if (data === undefined) throw new Error(TOKEN_REFUSED);
  return data;
}

/**
 * Data checks (`harrier check`, specs 036 and 041): the invariant breaches
 * and the contact links that resolve nowhere, each in the domain's words.
 * Linking contact ids is a second, separate action with its own
 * confirmation, because it writes into stored contacts.
 */
export function DataChecksSection() {
  const queryClient = useQueryClient();
  const check = useQuery({ queryKey: KEY, queryFn: fetchCheck });
  const [confirming, setConfirming] = useState(false);

  const link = useMutation({
    mutationFn: async (): Promise<Linked> => {
      const { data, error } = await api.POST("/ops/check/link-contacts", {
        body: { confirm: true },
      });
      if (error !== undefined) throw new Error(refusalMessage(error, "linking was refused"));
      if (data === undefined) throw new Error(TOKEN_REFUSED);
      return data;
    },
    onSuccess: () => {
      setConfirming(false);
      void queryClient.invalidateQueries({ queryKey: KEY });
    },
  });

  const unresolved = check.data?.unresolved_links.length ?? 0;

  return (
    <section className="ops-section" aria-labelledby="ops-check">
      <h3 id="ops-check" className="ops-section__heading">
        Data checks
      </h3>
      {check.isPending && <p className="ops-muted">Checking the tracker…</p>}
      {check.isError && (
        <p role="alert" className="ops-error">
          {check.error.message}
        </p>
      )}
      {check.isSuccess && <CheckResults data={check.data} />}
      {check.isSuccess && unresolved > 0 && !confirming && (
        <div className="ops-actions">
          <button
            type="button"
            className="ops-button--caution"
            onClick={() => {
              setConfirming(true);
            }}
          >
            Link contact ids…
          </button>
        </div>
      )}
      {confirming && (
        <div className="ops-confirm" role="group" aria-label="Confirm linking">
          <p>
            {unresolved} contact {unresolved === 1 ? "link resolves" : "links resolve"} nowhere now.
            Linking writes a job id into each link that matches a tracked job and drops none; it
            reports how many it actually wrote.
          </p>
          <div className="ops-actions">
            <button
              type="button"
              className="ops-button--caution"
              disabled={link.isPending}
              onClick={() => {
                link.mutate();
              }}
            >
              Link them
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirming(false);
              }}
            >
              Leave them
            </button>
          </div>
        </div>
      )}
      {link.error !== null && (
        <p role="alert" className="ops-error">
          {link.error.message}
        </p>
      )}
      {link.isSuccess && (
        <p role="status" className="ops-outcome">
          Linked {link.data.linked}; {link.data.unmatched} left unmatched.
        </p>
      )}
    </section>
  );
}

function CheckResults({ data }: { data: DataCheck }) {
  if (data.breaches.length === 0 && data.unresolved_links.length === 0) {
    return <p className="ops-note">Nothing to report.</p>;
  }
  return (
    <>
      {data.breaches.length > 0 && (
        <ul className="ops-list" aria-label="Rows that break an invariant">
          {data.breaches.map((item, index) => (
            <li key={`${item.job_id}-${String(index)}`}>
              job {item.job_id}: {item.breach}
            </li>
          ))}
        </ul>
      )}
      {data.unresolved_links.length > 0 && (
        <ul className="ops-list" aria-label="Contact links that resolve nowhere">
          {data.unresolved_links.map((item, index) => (
            <li key={`${item.contact}-${String(index)}`}>
              contact {item.contact}: {item.breach}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
