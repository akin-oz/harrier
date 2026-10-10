import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";

export type ConfigEntry = components["schemas"]["ConfigOut"];

// Configuration belongs to the install, not to a track (spec 096 leaves
// per-track configuration out of scope), so its key carries no track.
export const CONFIG_KEY = ["config"] as const;

// A 403 carries no body the contract describes, so it arrives as no data.
export const REFUSED_TOKEN = "refused: the local API token was not accepted";

async function fetchConfig(): Promise<readonly ConfigEntry[]> {
  const { data, error } = await api.GET("/config");
  if (error !== undefined || data === undefined) {
    throw new Error("could not read the configuration");
  }
  return data;
}

/** Every configuration kind, with where its value comes from (spec 023). */
export function useConfig() {
  return useQuery({ queryKey: CONFIG_KEY, queryFn: fetchConfig });
}

/**
 * One kind's editor state: the operator's draft, saving the whole kind in
 * one request, and resetting it to the file.
 *
 * A refusal leaves the draft exactly as typed, so the operator corrects the
 * input rather than retyping it. A save or a reset replaces the draft with
 * what the server now holds, which is the only value worth editing next.
 */
export function useKindEditor<D>(
  entry: ConfigEntry,
  toDraft: (value: unknown) => D,
  fromDraft: (draft: D) => unknown,
) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<D>(() => toDraft(entry.value));
  const [notice, setNotice] = useState<string | null>(null);
  // What the server answered last, which is what is in effect now.
  const [adopted, setAdopted] = useState<ConfigEntry | null>(null);
  const current = adopted ?? entry;

  function adopt(next: ConfigEntry): void {
    queryClient.setQueryData<readonly ConfigEntry[]>(CONFIG_KEY, (existing) =>
      existing?.map((item) => (item.kind === next.kind ? next : item)),
    );
    setAdopted(next);
    setDraft(toDraft(next.value));
  }

  const save = useMutation({
    mutationFn: async (): Promise<ConfigEntry> => {
      const { data, error } = await api.PUT("/config/{kind}", {
        params: { path: { kind: entry.kind } },
        body: { value: fromDraft(draft) },
      });
      if (error !== undefined) throw new Error(refusalMessage(error, "the value was not saved"));
      if (data === undefined) throw new Error(REFUSED_TOKEN);
      return data;
    },
    onMutate: () => {
      setNotice(null);
    },
    onSuccess: (next) => {
      adopt(next);
      setNotice("Saved to the store.");
    },
  });

  const reset = useMutation({
    mutationFn: async (): Promise<ConfigEntry> => {
      const { data, error } = await api.DELETE("/config/{kind}", {
        params: { path: { kind: entry.kind } },
      });
      if (error !== undefined)
        throw new Error(refusalMessage(error, "the stored value was not removed"));
      if (data === undefined) throw new Error(REFUSED_TOKEN);
      return data;
    },
    onMutate: () => {
      setNotice(null);
    },
    onSuccess: (next) => {
      // The route answers with the value now in effect either way, so
      // whether anything was removed is read from what was in effect before,
      // as `harrier config unset` reports it (spec 023).
      const wasStored = current.source === "store";
      adopt(next);
      setNotice(
        wasStored
          ? "The stored value was removed. The file in the checkout is in use."
          : "Nothing was stored. The file in the checkout was already in use.",
      );
    },
  });

  return { current, draft, setDraft, notice, save, reset };
}

export type KindEditorState = ReturnType<typeof useKindEditor>;
