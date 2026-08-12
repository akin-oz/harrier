import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../../shared/api/client";
import "./AddJob.css";

// `harrier add` in the browser. The route calls the same function the CLI
// verb does, so the scoring and the duplicate check are not repeated here
// (spec 042).
export function AddJob() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [company, setCompany] = useState("");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [location, setLocation] = useState("");
  const [outcome, setOutcome] = useState<string | null>(null);

  const add = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/tracker", {
        body: { company, title, url, location, source: "manual", description: "" },
      });
      if (error !== undefined) throw new Error(JSON.stringify(error));
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
        void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      }
    },
    onError: (error: Error) => {
      setOutcome(error.message);
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
        Add a job by hand
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
