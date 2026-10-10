import { useQuery } from "@tanstack/react-query";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";

export type CommandPlaces = components["schemas"]["CommandPlacesOut"];
export type HostFacts = components["schemas"]["HostFactsOut"];
export type HostFact = components["schemas"]["HostPanelRowOut"]["fact"];
export type Schedule = components["schemas"]["ScheduleOut"];

// Neither route declares a refusal with a body, so the generated `error` is
// `never` and only the body can be absent: a 403 for a missing token, or a
// 503 while a host process holds the database.
async function fetchPlaces(): Promise<CommandPlaces> {
  const { data } = await api.GET("/settings/commands");
  if (data === undefined) throw new Error("could not read the command list");
  return data;
}

async function fetchHostFacts(): Promise<HostFacts> {
  const { data } = await api.GET("/settings/host");
  if (data === undefined) throw new Error("could not read what the container can see");
  return data;
}

/** Where every CLI command has its place, from the API (spec 096). */
export function useCommandPlaces() {
  return useQuery({ queryKey: ["settings", "commands"], queryFn: fetchPlaces });
}

/** What the container can read about each host-only command (spec 096). */
export function useHostFacts() {
  return useQuery({ queryKey: ["settings", "host"], queryFn: fetchHostFacts });
}

async function fetchSchedule(): Promise<Schedule> {
  const { data, error } = await api.GET("/ops/schedule");
  if (error !== undefined) throw new Error(refusalMessage(error, "could not read the schedule"));
  return data;
}

/**
 * The schedule as spec 050's route reads it: cadences and last successes.
 * The key the Operations page uses, so both read one answer.
 */
export function useSchedule() {
  return useQuery({ queryKey: ["ops", "schedule"], queryFn: fetchSchedule });
}
