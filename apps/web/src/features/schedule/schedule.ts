import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";

export type Schedule = components["schemas"]["ScheduleOut"];
export type ScheduledJob = components["schemas"]["ScheduledJobOut"];
export type HostedJob = components["schemas"]["HostedJobOut"];
type Outcome = NonNullable<HostedJob["last_outcome"]>;
type CredentialKind = components["schemas"]["OfferedJobOut"]["needs"][number];

// Shared by the Operations page and the Settings form, so a save in one
// refreshes the other.
export const SCHEDULE_KEY = ["ops", "schedule"] as const;

export async function fetchSchedule(): Promise<Schedule> {
  const { data, error } = await api.GET("/ops/schedule");
  if (error !== undefined) throw new Error(refusalMessage(error, "could not read the schedule"));
  return data;
}

/** The hosted answer: no host command is the owner's to run (spec 120). */
export function isHosted(schedule: Schedule): boolean {
  return schedule.host_command === null;
}

/**
 * The server's schedule definition could not be read: nothing is on offer,
 * so nothing can be chosen, and the fault is the server's, not the owner's
 * stored row (spec 120, Amendment). Locally `offered` is always null, and an
 * error there is the definition's too.
 */
export function definitionUnreadable(schedule: Schedule): boolean {
  return schedule.error !== null && schedule.offered === null;
}

// Each scheduler outcome in words (spec 108's outcome classes). A typed
// record, so an outcome the contract adds fails the type check here.
const OUTCOME_WORDS: Record<Outcome, string> = {
  running: "Running",
  ok: "OK",
  failed: "Failed",
  crashed: "Crashed",
  timed_out: "Timed out",
  not_configured: "Not configured",
  refused: "Refused",
  interrupted: "Interrupted",
  missed: "Missed",
};

export function outcomeWords(outcome: Outcome): string {
  return OUTCOME_WORDS[outcome];
}

const CREDENTIAL_NAMES: Record<CredentialKind, string> = {
  ai: "AI provider",
  apify: "Apify",
  hunter: "Hunter",
  telegram: "Telegram",
};

export function credentialName(kind: CredentialKind): string {
  return CREDENTIAL_NAMES[kind];
}

/**
 * An ISO 8601 UTC time as the owner reads it, in their schedule's zone:
 * "16 Oct, 20:03". The text as given when it is not a time.
 */
export function inZone(iso: string, zone: string): string {
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return iso;
  try {
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: zone,
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).format(moment);
  } catch {
    // A zone this browser cannot format: UTC, said as such.
    return `${iso.replace("T", " ").replace(/:\d\dZ$/, "")} UTC`;
  }
}

/** The browser's own zone, the default for an owner with no schedule. */
export function browserZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** The zones offered, always including `current`, sorted. */
export function zoneChoices(current: string): string[] {
  const zones = new Set(Intl.supportedValuesOf("timeZone"));
  zones.add(current);
  return [...zones].sort((a, b) => a.localeCompare(b));
}
