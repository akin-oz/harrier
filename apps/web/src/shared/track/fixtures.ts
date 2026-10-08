import type { Track } from "./track";

// Tracks as `GET /tracks` answers them, for tests only. The labels are the
// API's (`harrier.tracks.KIND_RULES`, pinned by
// services/api/tests/test_tracks_cli.py and test_api_tracks.py); the app
// itself never holds them. Slugs and labels are synthetic (ADR-008).
export const INDUSTRY_TRACK: Track = {
  id: 1,
  slug: "job",
  kind: "industry",
  label: "Job search",
  archived: false,
  is_default: true,
  status_labels: {
    prospect: "prospect",
    shortlisted: "shortlisted",
    tailored_cv_requested: "CV requested",
    applied: "applied",
    interviewing: "interviewing",
    rejected: "rejected",
  },
};

export const ACADEMIC_TRACK: Track = {
  id: 2,
  slug: "second-search",
  kind: "academic",
  label: "Second search",
  archived: false,
  is_default: false,
  status_labels: {
    prospect: "found",
    shortlisted: "shortlisted",
    tailored_cv_requested: "preparing documents",
    applied: "submitted",
    interviewing: "interviewing",
    rejected: "closed",
  },
};
