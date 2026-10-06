import type { components } from "@harrier/contract";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import type { Job, JobStatus } from "../../entities/job";
import { api } from "../../shared/api/client";
import "./JobActions.css";

type RejectionCode = components["schemas"]["RejectionCode"];
type CompanyOutcomeCode = components["schemas"]["CompanyOutcomeCode"];

// The candidate's verbs, named as the CLI names them. The mapping lives in
// `harrier.tracker.actions.STATUS_BY_VERB`, so the browser cannot offer a
// transition the command line lacks (spec 042). `interviewing` is not here:
// an interview is something the company did, so it is recorded as the
// company's outcome through Company replied or Interview invite (spec 080),
// and the domain records the CLI's `interviewing` verb the same way (spec 079).
const VERBS = [
  { verb: "shortlist", label: "Shortlist", from: ["prospect"] },
  { verb: "track", label: "Request CV", from: ["prospect", "shortlisted"] },
  { verb: "applied", label: "Applied", from: ["prospect", "shortlisted", "tailored_cv_requested"] },
  { verb: "reject", label: "Reject", from: [] },
] as const;

// Once an application is out, the next word is the company's, and leaving is
// a withdrawal rather than a rejection. The two sit side by side under names
// that say who acted, so habit cannot file one as the other (spec 080).
const AFTER_APPLYING: readonly JobStatus[] = ["applied", "interviewing"];

// The frequent exit reasons, one click each (spec 056). The text is what the
// row stores, exactly as spec 056 stored it, so new rows stay groupable with
// old ones; the code is what the history records (spec 079). `hybrid` and
// `onsite` share a code and keep their words. A company's verdict is not
// here and cannot be: `RejectionCode` has no company members.
const EXIT_PILLS: readonly { text: string; code: RejectionCode }[] = [
  { text: "hybrid", code: "not_remote" },
  { text: "onsite", code: "not_remote" },
  { text: "closed", code: "vacancy_closed" },
  { text: "missing stack", code: "stack" },
  { text: "location", code: "location" },
  { text: "language", code: "language" },
];

// Every rejection code the contract declares, with how it reads. A `Record`
// over the generated union, so a code added to `harrier.tracker.reasons`
// fails the type check here until someone decides how it reads (spec 080).
const CODE_LABEL: Record<RejectionCode, string> = {
  not_remote: "not remote",
  location: "location",
  stack: "stack",
  role_too_senior: "too senior",
  role_too_junior: "too junior",
  contract_type: "contract type",
  language: "language",
  timezone: "timezone",
  company: "company",
  compensation: "compensation",
  other: "other",
  vacancy_closed: "vacancy closed",
  duplicate: "duplicate",
  application_expired: "application expired",
  ai_evaluation: "ai evaluation",
  auto_reject: "automatic rule",
};

// Behind `other…`: the candidate's codes no pill carries. The system's codes
// are not offered; the candidate does not run the batch evaluator by hand.
const OTHER_CODES: readonly RejectionCode[] = [
  "role_too_senior",
  "role_too_junior",
  "contract_type",
  "timezone",
  "company",
  "compensation",
  "other",
];

// The company's responses, in the order they are offered. Interview first on
// an applied row because it is the one that moves the row forward, and the
// only one that does not close it, so it carries no danger hover (spec 080).
const COMPANY_PILLS: Record<
  CompanyOutcomeCode,
  { text: string; on: readonly JobStatus[]; closes: boolean }
> = {
  interview_invited: { text: "interview", on: ["applied"], closes: false },
  company_rejected: { text: "rejected", on: ["applied", "interviewing"], closes: true },
  assessment_failed: { text: "assessment failed", on: ["applied", "interviewing"], closes: true },
  ghosted: { text: "ghosted", on: ["applied", "interviewing"], closes: true },
  no_response: { text: "no response", on: ["applied"], closes: true },
};

// The two decisions a row can be mid-way through: the candidate leaving it,
// or recording what the company said. One pattern for both (spec 056's).
type Takeover = "exit" | "company";

// The one verb that is what to do next from this status. The rest stay
// reachable behind a disclosure, which is what keeps the actions column
// narrow enough that the table does not scroll sideways. Every verb is still
// on the row; none is removed.
function forwardVerb(status: string): { verb: string; label: string } | null {
  // A rejected row's way back is Reopen, which is the existing `shortlist`
  // verb under another name rather than a sixth verb. Shortlisted, not
  // prospect, because the batch evaluator reads only prospects and would
  // reject the row again on its next refresh (spec 072).
  if (status === "rejected") return { verb: "shortlist", label: "Reopen" };
  return VERBS.find((entry) => (entry.from as readonly string[]).includes(status)) ?? null;
}

// `onApply` is passed in rather than the page rendering its own button
// beside this one: two separate controls stacked into two rows and made the
// table row twice as tall as it needed to be. What to do next for this job
// belongs on one line.
type Props = { job: Job; onApply?: (job: Job) => void };

export function JobActions({ job, onApply }: Props) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [otherCode, setOtherCode] = useState<RejectionCode>("other");
  const [takeover, setTakeover] = useState<Takeover | null>(null);
  const [otherOpen, setOtherOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  // Which opener gets focus back once a takeover is dismissed. The opener is
  // unmounted while the takeover shows, so it can only be focused after the
  // render that brings it back.
  const [returnFocus, setReturnFocus] = useState<Takeover | null>(null);
  const reasonInputRef = useRef<HTMLInputElement | null>(null);
  const firstPillRef = useRef<HTMLButtonElement | null>(null);
  const exitRef = useRef<HTMLButtonElement | null>(null);
  const companyRef = useRef<HTMLButtonElement | null>(null);

  // Opening a takeover unmounts the button that had focus, so without this a
  // keyboard user lands on nothing. Focus goes to the first pill, or to the
  // reason input once `other…` asked for it (review finding on PR #65).
  useEffect(() => {
    if (takeover === null) return;
    if (otherOpen) {
      reasonInputRef.current?.focus();
    } else {
      firstPillRef.current?.focus();
    }
  }, [takeover, otherOpen]);

  useEffect(() => {
    if (returnFocus === null) return;
    (returnFocus === "exit" ? exitRef : companyRef).current?.focus();
    setReturnFocus(null);
  }, [returnFocus]);

  function reset(): void {
    setTakeover(null);
    setOtherOpen(false);
    setReason("");
    setOtherCode("other");
  }

  // Escape and Cancel both dismiss without sending, and hand focus back to
  // the control that opened the takeover (spec 080).
  function dismiss(): void {
    setReturnFocus(takeover);
    reset();
  }

  function onTakeoverKey(event: KeyboardEvent<HTMLElement>): void {
    if (event.key !== "Escape") return;
    event.preventDefault();
    dismiss();
  }

  function settled(): void {
    setFailure(null);
    reset();
    void queryClient.invalidateQueries({ queryKey: ["jobs"] });
  }

  const change = useMutation({
    mutationFn: async ({
      verb,
      why,
      code,
    }: {
      verb: string;
      why?: string;
      code?: RejectionCode;
    }) => {
      // A code travels only with a rejection; every other verb sends what it
      // always sent.
      const body =
        code === undefined
          ? { verb, reason: why ?? null }
          : { verb, reason: why ?? null, reason_code: code };
      const { data, error } = await api.POST("/tracker/{selector}/status", {
        params: { path: { selector: String(job.id) } },
        body,
      });
      // A refusal is a normal outcome here, not an exception to swallow: the
      // tracker declines transitions and the operator needs the reason it
      // gave, in the words it gave them.
      if (error !== undefined) {
        throw new Error(refusalMessage(error));
      }
      if (data === undefined) {
        throw new Error("the local API token was not accepted");
      }
      return data;
    },
    onSuccess: settled,
    onError: (error: Error) => {
      setFailure(error.message);
    },
  });

  // What the company did, recorded through its own route so it can never
  // arrive as the candidate's rejection (specs 079, 080).
  const outcome = useMutation({
    mutationFn: async (code: CompanyOutcomeCode) => {
      const { data, error } = await api.POST("/tracker/{selector}/outcome", {
        params: { path: { selector: String(job.id) } },
        body: { code, note: null },
      });
      if (error !== undefined) throw new Error(refusalMessage(error));
      if (data === undefined) throw new Error("the local API token was not accepted");
      return data;
    },
    onSuccess: settled,
    onError: (error: Error) => {
      // A stale page can offer this on a row that has since changed. The
      // refusal is shown in the domain's words and the row is fetched again,
      // so what it offers next matches what it is. Closing the takeover
      // unmounts the pill that had focus, so focus goes back to the control
      // that opened it, as it does on Escape and Cancel (review finding on
      // PR #122). Outcomes sent from More open no takeover and need none.
      setFailure(error.message);
      setReturnFocus(takeover);
      reset();
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const rescore = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/tracker/{selector}/rescore", {
        params: { path: { selector: String(job.id) } },
      });
      if (error !== undefined) throw new Error(refusalMessage(error));
      if (data === undefined) throw new Error("the local API token was not accepted");
      return data;
    },
    onSuccess: (result) => {
      setFailure(null);
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      if (result.previous !== String(result.current)) {
        setFailure(`rescored ${result.previous || "-"} to ${String(result.current)}`);
      }
    },
    onError: (error: Error) => {
      setFailure(error.message);
    },
  });

  const busy = change.isPending || outcome.isPending || rescore.isPending;
  const afterApplying = AFTER_APPLYING.includes(job.status);
  const exitLabel = afterApplying ? "Withdraw" : "Reject";
  const primary = afterApplying ? null : forwardVerb(job.status);
  // A rejected row can still move forward (Reopen and the verbs under More),
  // but it cannot be rejected again or applied to until it is reopened
  // (spec 072).
  const closed = job.status === "rejected";
  // Everything the row can do that is not the primary verb and not Reject,
  // which has its own control because it asks for a reason first.
  const secondary = VERBS.filter(
    (entry) => entry.verb !== "reject" && entry.verb !== primary?.verb,
  );
  const companyPills = (Object.keys(COMPANY_PILLS) as CompanyOutcomeCode[]).filter((code) =>
    COMPANY_PILLS[code].on.includes(job.status),
  );
  // Text is required only for `other`, which says nothing without it. A
  // chosen code with no text stores the code's own label, so the row still
  // reads as a reason.
  const otherReady = otherCode !== "other" || reason.trim() !== "";

  return (
    <div className="job-actions">
      {/* While a takeover is open, the row is mid-decision and the other verbs
          are not what to do next. They used to stay live, so a click landed a
          status change on a row the operator was in the middle of rejecting
          (review finding on PR #41). */}
      {takeover === null && (
        <div className="job-actions__row">
          {afterApplying ? (
            <button
              ref={companyRef}
              type="button"
              className="job-actions__primary"
              disabled={busy}
              onClick={() => {
                setTakeover("company");
              }}
            >
              Company replied
            </button>
          ) : (
            primary !== null && (
              <button
                type="button"
                className="job-actions__primary"
                disabled={busy}
                onClick={() => {
                  change.mutate({ verb: primary.verb });
                }}
              >
                {primary.label}
              </button>
            )
          )}
          {/* The exit keeps its own control rather than sitting behind the
              disclosure. It is the one verb that closes a row and the one
              that asks for a reason, so it is weighted differently from the
              rest. */}
          <button
            ref={exitRef}
            type="button"
            className="job-actions__reject"
            disabled={busy || closed}
            onClick={() => {
              setTakeover("exit");
            }}
          >
            {exitLabel}
          </button>
          {onApply !== undefined && (
            <button
              type="button"
              disabled={closed}
              aria-label={`Apply to ${job.company}, ${job.title}`}
              onClick={() => {
                onApply(job);
              }}
            >
              Apply
            </button>
          )}
          <button
            type="button"
            aria-expanded={moreOpen}
            aria-label={`More actions for ${job.company}, ${job.title}`}
            onClick={() => {
              setMoreOpen(!moreOpen);
            }}
          >
            More
          </button>
        </div>
      )}

      {takeover === null && moreOpen && (
        <div
          className="job-actions__more"
          role="group"
          aria-label={`More actions for ${job.company}, ${job.title}`}
        >
          {secondary.map((entry) => (
            <button
              key={entry.verb}
              type="button"
              disabled={busy}
              onClick={() => {
                change.mutate({ verb: entry.verb });
              }}
            >
              {entry.label}
            </button>
          ))}
          {/* A recruiter can write about a job nobody applied to, including
              one that was rejected, and the row then goes straight to
              interviewing (spec 072). Recorded as what it is, the company's
              invitation (specs 079, 080). */}
          {!afterApplying && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                outcome.mutate("interview_invited");
              }}
            >
              Interview invite
            </button>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              rescore.mutate();
            }}
          >
            Rescore
          </button>
        </div>
      )}

      {/* The frequent reasons are one click: the pill is the confirmation,
          and Cancel covers a mis-press before it. `other…` reaches the
          free-text input, which stops being the default path (spec 056). */}
      {takeover === "exit" && !otherOpen && (
        <div
          className="job-actions__pills"
          role="group"
          aria-label={exitLabel}
          onKeyDown={onTakeoverKey}
        >
          <span className="job-actions__pills-label">{exitLabel}:</span>
          {EXIT_PILLS.map((pill, index) => (
            <button
              key={pill.text}
              ref={index === 0 ? firstPillRef : undefined}
              type="button"
              className="job-actions__pill job-actions__pill--closes"
              disabled={busy}
              onClick={() => {
                change.mutate({ verb: "reject", why: pill.text, code: pill.code });
              }}
            >
              {pill.text}
            </button>
          ))}
          <button
            type="button"
            className="job-actions__pill-other"
            disabled={busy}
            onClick={() => {
              setOtherOpen(true);
            }}
          >
            other…
          </button>
          <button type="button" className="job-actions__cancel" onClick={dismiss}>
            Cancel
          </button>
        </div>
      )}

      {takeover === "exit" && otherOpen && (
        // Still the exit takeover, so still named by its word (spec 080).
        <span
          className="job-actions__reason"
          role="group"
          aria-label={exitLabel}
          onKeyDown={onTakeoverKey}
        >
          <select
            aria-label="Reason code"
            value={otherCode}
            onChange={(event) => {
              setOtherCode(event.target.value as RejectionCode);
            }}
          >
            {OTHER_CODES.map((code) => (
              <option key={code} value={code}>
                {CODE_LABEL[code]}
              </option>
            ))}
          </select>
          <input
            ref={reasonInputRef}
            aria-label="Rejection reason"
            value={reason}
            placeholder="why?"
            onChange={(event) => {
              setReason(event.target.value);
            }}
          />
          <button
            type="button"
            disabled={busy || !otherReady}
            onClick={() => {
              change.mutate({
                verb: "reject",
                why: reason.trim() || CODE_LABEL[otherCode],
                code: otherCode,
              });
            }}
          >
            Confirm
          </button>
          <button type="button" className="job-actions__cancel" onClick={dismiss}>
            Cancel
          </button>
        </span>
      )}

      {/* The company's response, in the same pattern and the same anatomy as
          the exit: no new vocabulary, no modal. Only the label says whose
          word it is (spec 080). */}
      {takeover === "company" && (
        <div
          className="job-actions__pills"
          role="group"
          aria-label="Company response"
          onKeyDown={onTakeoverKey}
        >
          <span className="job-actions__pills-label">Company:</span>
          {companyPills.map((code, index) => (
            <button
              key={code}
              ref={index === 0 ? firstPillRef : undefined}
              type="button"
              className={
                COMPANY_PILLS[code].closes
                  ? "job-actions__pill job-actions__pill--closes"
                  : "job-actions__pill"
              }
              disabled={busy}
              onClick={() => {
                outcome.mutate(code);
              }}
            >
              {COMPANY_PILLS[code].text}
            </button>
          ))}
          <button type="button" className="job-actions__cancel" onClick={dismiss}>
            Cancel
          </button>
        </div>
      )}

      {failure !== null && (
        <p className="job-actions__failure" role="status">
          {failure}
        </p>
      )}
    </div>
  );
}

// The API answers a refusal with a `detail` string that is the message the
// domain wrote. Showing it verbatim is the point: the CLI prints the same
// words, and a UI that paraphrased them would be a second implementation of
// the explanation.
function refusalMessage(error: unknown): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return "the tracker refused that change";
}
