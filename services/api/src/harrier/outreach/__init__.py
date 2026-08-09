"""Contacts, staged discovery, and the outreach queue (spec 016).

The staging discipline: discovery writes candidates to a review
artifact; only an explicit approval writes into the contacts store.
"""

from __future__ import annotations

from harrier.outreach.backfill import BackfillSummary, backfill_posters
from harrier.outreach.contacts import (
    best_contact,
    contact_sort_key,
    contacts_for_job,
    delete_contact,
    find_contact,
    infer_relevance,
    parse_linked_jobs,
    serialize_linked_jobs,
    update_contact,
    update_contact_status,
    upsert_contact,
)
from harrier.outreach.discovery import (
    approve_candidate,
    build_best_contact_search_specs,
    build_search_specs,
    find_best_contacts_for_job,
    find_contacts_for_job,
    has_strong_best_contact,
    load_candidates_artifact,
    merge_ranked_contacts,
    normalize_profile_result,
    outreach_slug,
    score_contact_fit,
    update_candidate_review_status,
    write_candidates_artifact,
)
from harrier.outreach.hunter import domain_search, find_email, verify_email
from harrier.outreach.state import (
    business_days_after,
    filter_outreach_rows,
    mark_job_outreach_replied,
    mark_job_outreach_sent,
    outreach_due_rows,
    refresh_outreach_fields,
    set_best_contact_for_job,
    snooze_job_outreach,
    sync_tracker_outreach,
)

__all__ = [
    "BackfillSummary",
    "approve_candidate",
    "backfill_posters",
    "best_contact",
    "build_best_contact_search_specs",
    "build_search_specs",
    "business_days_after",
    "contact_sort_key",
    "contacts_for_job",
    "delete_contact",
    "domain_search",
    "filter_outreach_rows",
    "find_best_contacts_for_job",
    "find_contact",
    "find_contacts_for_job",
    "find_email",
    "has_strong_best_contact",
    "infer_relevance",
    "load_candidates_artifact",
    "mark_job_outreach_replied",
    "mark_job_outreach_sent",
    "merge_ranked_contacts",
    "normalize_profile_result",
    "outreach_due_rows",
    "outreach_slug",
    "parse_linked_jobs",
    "refresh_outreach_fields",
    "score_contact_fit",
    "serialize_linked_jobs",
    "set_best_contact_for_job",
    "snooze_job_outreach",
    "sync_tracker_outreach",
    "update_candidate_review_status",
    "update_contact",
    "update_contact_status",
    "upsert_contact",
    "verify_email",
    "write_candidates_artifact",
]
