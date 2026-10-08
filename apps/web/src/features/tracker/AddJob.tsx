import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../../shared/api/client";
import { TRACKS_KEY, trackKey, trackQuery, useSelectedSlug } from "../../shared/track";
import type { Track } from "../../shared/track";
import { refusalMessage } from "./JobActions";
import "./AddJob.css";

// `harrier add` in the browser. The route calls the same function the CLI
// verb does, so the scoring and the duplicate check are not repeated here
// (spec 042). It adds to the selected track, and off the default track it
// asks for the deadline the queue is ordered by (spec 094). The default
// track's form is unchanged: nothing there reads a deadline (spec 094, Open
// decisions, item 2).
export function AddJob({ track }: { track: Track }) {
  const queryClient = useQueryClient();
  const slug = useSelectedSlug();
  const withDeadline = !track.is_default;
  const [deadline, setDeadline] = useState("");
  const [open, setOpen] = useState(false);
  const [company, setCompany] = useState("");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [location, setLocation] = useState("");
  const [outcome, setOutcome] = useState<string | null>(null);

  const add = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/tracker", {
        params: { query: trackQuery(slug) },
        body: {
          company,
          title,
          url,
          location,
          source: "manual",
          description: "",
          ...(withDeadline && deadline !== "" ? { deadline } : {}),
        },
      });
      // A refusal reads in the domain's words: "track ... is archived" when
      // another tab archived it, rather than the raw response body.
      if (error !== undefined) throw new Error(refusalMessage(error));
      if (data === undefined) throw new Error("the local API token was not accepted");
      return data;
    },
    onSuccess: (result) => {
      // "already tracked" and "not enough to add" are answers, not errors:
      // the operator asked for something the tracker declined, and the
      // message is the domain's own words rather than a paraphrase.
      setOutcome(result.message);
      if (result.status === "added") {
        setCompany("");
        setTitle("");
        setUrl("");
        setLocation("");
        setDeadline("");
        void queryClient.invalidateQueries({ queryKey: trackKey(slug, "jobs") });
      }
    },
    onError: (error: Error) => {
      setOutcome(error.message);
      void queryClient.invalidateQueries({ queryKey: TRACKS_KEY });
    },
  });

  // The trigger stays put and the form opens over the page beneath it. Letting
  // the form itself sit in the toolbar row stretched that row to the height of
  // four fields and pushed the table half a screen down.
  return (
    <div className="add-job-slot">
      <button
        type="button"
        className="add-job__open"
        aria-expanded={open}
        onClick={() => {
          setOpen(!open);
        }}
      >
        {track.is_default ? "Add a job by hand" : "Add a position by hand"}
      </button>
      {open && (
        <form
          className="add-job"
          onSubmit={(event) => {
            event.preventDefault();
            add.mutate();
          }}
        >
          <label>
            Company
            <input
              value={company}
              onChange={(event) => {
                setCompany(event.target.value);
              }}
              required
            />
          </label>
          <label>
            Title
            <input
              value={title}
              onChange={(event) => {
                setTitle(event.target.value);
              }}
              required
            />
          </label>
          <label>
            Location
            <input
              value={location}
              onChange={(event) => {
                setLocation(event.target.value);
              }}
            />
          </label>
          <label>
            URL
            <input
              value={url}
              onChange={(event) => {
                setUrl(event.target.value);
              }}
              type="url"
            />
          </label>
          {withDeadline && (
            <label>
              Deadline
              <input
                type="date"
                value={deadline}
                onChange={(event) => {
                  setDeadline(event.target.value);
                }}
              />
            </label>
          )}
          <div className="add-job__buttons">
            <button type="submit" disabled={add.isPending || company === "" || title === ""}>
              Add
            </button>
            <button
              type="button"
              onClick={() => {
                setOpen(false);
              }}
            >
              Close
            </button>
          </div>
          {outcome !== null && (
            <p className="add-job__outcome" role="status">
              {outcome}
            </p>
          )}
        </form>
      )}
    </div>
  );
}
