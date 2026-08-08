import type { components } from "@harrier/contract";

export type Job = components["schemas"]["JobOut"];
export type JobStatus = Job["status"];

export const JOB_STATUSES: readonly JobStatus[] = [
  "prospect",
  "shortlisted",
  "tailored_cv_requested",
  "applied",
  "interviewing",
  "rejected",
] as const;
