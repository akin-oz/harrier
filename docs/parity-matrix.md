# Inventory and parity matrix

Every capability of `~/job-hunt-local`, marked keep, change, or drop for the harrier rewrite.
Source paths cite the old repo and are the proof of current behavior. The old repo stays
untouched and running until cutover (see `docs/cutover-plan.md`).

Legend:

- **keep**: port the behavior as is. Implementation may be rewritten, behavior must not change.
- **change**: port the capability, deliberately alter the shape. The rationale names the change.
- **drop**: do not port. The rationale says why it is safe.

## 1. Discovery pipeline

| Capability | Source | Verdict | Rationale |
|---|---|---|---|
| Shared normalized job schema | `scripts/job_sources.py` (make_normalized_job) | keep | The ingestion-only invariant depends on one shared shape; carries over as a typed model. |
| Screening gate order: seen-state, hold list, title, remote/EMEA, dedupe, score cutoff 55 | `scripts/job_sources.py` (screen_jobs) | keep | The order is load-bearing (enrichment cost, Apify billing); tests pin it (`tests/test_job_sources.py`). |
| Remote-only and EMEA enforcement, incl. location-only negative hints and the linkedin_search bypass | `scripts/job_sources.py` (remote_region_allowed) | keep | Policy invariant. The hard-won false-positive exclusions ("office", "flex") documented in code comments carry over as tests. |
| EU-permit and EU-entity phrases as positive signals, never filters | `scripts/job_sources.py` (PREFERRED_SIGNAL_WEIGHTS), `CLAUDE.md` "Candidate EU status" | keep | EU-entity contracting context; a requirement, not a heuristic. |
| Scoring model (base 30, title, keywords, skill signals, region, domain bonus, cap 120) | `scripts/job_sources.py` (score_job, DEFAULT_SCORING) | keep | Overridable from candidate config; behavior preserved, weights stay config-driven. |
| Archetype detection | `scripts/job_sources.py` (detect_archetype) + two more copies | change | Keep the capability, collapse the three copies (also in `evaluate_offer.py` and `tailor_resume.py`) into one domain function. |
| Description cache keyed by URL hash | `state/job-descriptions/` (one file per description) | keep | Saves Apify re-billing and re-fetches; port as is, migrate existing cache. |
| JD enrichment fetch for short descriptions on ATS hosts | `scripts/job_sources.py` (enrich_job_description_for_scoring) | keep | Prevents false low-score rejections; pinned by tests. |
| Per-source seen-state, capped at 10,000 keys | `state/job-discovery/*_seen.json` | keep | Cross-run dedupe layer; migrate existing state at cutover. |
| notes column as key=value store (score=, signals=, external_key=, ...) | `scripts/job_sources.py` (extract_note_value) | change | The hidden schema becomes real columns in the new store (ADR-003). external_key dedupe becomes a first-class indexed field. |
| Greenhouse importer | `scripts/import_greenhouse_jobs.py` | keep | Free, priority 1. |
| Ashby importer incl. HTML fallback on API 404 | `scripts/import_ashby_jobs.py` | keep | Fallback is tested behavior (`tests/test_feed_importers.py`). |
| Lever importer incl. EU API base selection | `scripts/import_lever_jobs.py` | keep | Tested behavior. |
| RemoteOK importer | `scripts/import_remoteok_jobs.py` | keep | Free source, already in the priority chain of `run-job-imports.py`. |
| Apify LinkedIn importer: run lifecycle, dataset-file mode, caching all descriptions even for rejected items | `scripts/import_apify_linkedin_jobs.py` | keep | The only paid source; the cache-everything behavior is a deliberate cost saver. |
| Auto-linking LinkedIn job poster into contacts on accept | `scripts/import_apify_linkedin_jobs.py` (link_publisher_contacts) | change | Keep the capability, route it through the staged-contacts path instead of writing contacts directly. It currently crosses the job/contact boundary. |
| Wellfound batch import | `scripts/import_wellfound_export.py` | keep | Manual export ingest, low cost to port. |
| WTTJ batch import | `scripts/import_wttj_export.py` | keep | Same. |
| Orchestrator: priority order, single aggregated Telegram notify, run summary JSON | `scripts/run-job-imports.py` | keep | The one shared execution path; per-source summaries and `incoming/job_imports_run.json` behavior carry over. |
| Legacy orchestrator incl. Workable support | `scripts/orchestrate_job_search.py` | drop | Superseded by `run-job-imports.py`; not called by any scheduler or wrapper. Workable gets a spec stub in the backlog instead of a blind port. |
| feeds.txt one-URL-per-line routing by netloc | `config/feeds.txt`, `scripts/run-job-imports.py` (parse_ats_feeds) | change | The routing keeps; the storage moves. The watchlist is user data, so it lives in the database with the file as an import source and a fallback (spec 023, ADR-009). The one-URL-per-line format stays as the import format. |
| Per-source board files | `config/greenhouse_boards.txt`, `ashby_boards.txt`, `lever_boards.txt` | drop | Superseded by `feeds.txt` for the orchestrator path; only reachable by direct importer invocation with defaults. |
| Vacancy liveness check: probe prospect URLs, classify open/closed/unknown, opt-in auto-reject of closed | `scripts/check_vacancy_status.py` | keep | Missed by the first sweep of this matrix and found by a feature audit on 2026-08-11. Per-source liveness signals (LinkedIn guest endpoint, the Ashby posting API as the canonical open list, Greenhouse 404/410); only `closed` is auto-rejected and only under `--apply`. Same shape as spec 025's board probe, one level down. |
| Company hold list | `config/companies-hold.csv` | change | The gate keeps; the storage moves to the database with the CSV as an import source (spec 023). The reason column is dropped on import: nothing reads it and it is personal operational commentary (ADR-008). |

## 2. Tracker

| Capability | Source | Verdict | Rationale |
|---|---|---|---|
| Single source of truth for application state | `tracker/jobs.csv` | change | The invariant keeps: one store, one write path. The store becomes SQLite with CSV export (ADR-003). |
| 20-column tracker schema | `scripts/job_sources.py` (TRACKER_FIELDS), duplicated in `scripts/jobs.py` and `gui/constants.py` | change | Schema keeps its fields, gains real columns for the notes key=value data, and gets exactly one definition. |
| Status lifecycle prospect, shortlisted, tailored_cv_requested, applied, interviewing, rejected | `scripts/jobs.py:43` | keep | `interviewing` exists in code but not in OPERATIONS.md; the new docs document it. Transitions stay spec-gated. |
| Orthogonal outreach status axis | `scripts/outreach_lib.py` | keep | Second state machine on the same row; preserved as is. |
| Tracker CLI verbs: shortlist, tailor, applied, interviewing, reject, track, add, next, review, reevaluate, answer, evaluate | `scripts/jobs.py` | keep | The daily driver; the new CLI keeps verb parity. |
| PDF-existence gate on `tailor` (row unchanged on failure) | `scripts/jobs.py` (run_tailor_resume) | keep | Artifact-correctness invariant. |
| `applied` seeds the outreach block and follow-up date | `scripts/jobs.py:394` | keep | Encodes the workflow; pinned by tests. |
| Selector semantics: numeric row, unique substring, abort on ambiguity | `scripts/jobs.py` (resolve_selector) | keep | Prevents wrong-row mutations; worth a test of its own. |
| Row repair / column backfill | `scripts/jobs.py` (repair_tracker_row) | drop | A CSV-drift artifact; a real schema with migrations makes it unnecessary. |
| Manual add with dedupe and scoring | `scripts/jobs.py` (add) | keep | Feeds the bookmarklet capture path too. |
| Tracker backups (`*.bak-*` files) | `tracker/` | change | Ad hoc backup files become automated pre-migration snapshots plus the CSV export history. |

## 3. Artifact generation

| Capability | Source | Verdict | Rationale |
|---|---|---|---|
| Tailored resume: md, html, pdf, metadata sidecar | `scripts/tailor_resume.py` | keep | Core capability. |
| Verified-content-only rule: AI selects and orders bullet IDs, never invents | `scripts/tailor_resume.py` (require_truth, validate_content_plan) | keep | The honesty invariant; carries over with its validators. |
| Bullet pool embedded in script source | `scripts/tailor_resume.py` (BULLET_POOL) | change | Content moves out of code into the encrypted data layer (ADR-002); code keeps only the mechanism. |
| PDF validation: non-empty, no replacement chars, no unresolved placeholders, page count 1 | `scripts/tailor_resume.py` (validate_rendered_pdf) | keep | The gate behind "success only if the PDF exists". |
| Internal-label scrubbing from recruiter-facing output | `scripts/tailor_resume.py` (normalize_visible_role_title, build_internal_metadata) | keep | Stated rule in OPERATIONS.md; internal metadata stays in sidecar files only. |
| Deterministic fallback plan when LLM unavailable | `scripts/tailor_resume.py` | keep | Keeps the daily driver working offline. |
| Cover letters: 3 paragraphs, banned phrases, PDF-required, PDF/HTML contain only the full letter | `scripts/openai_cover_letters.py`, `scripts/generate_cover_letter.py` | keep | All pinned by `tests/test_openai_cover_letters.py`. |
| Application answers, deterministic path | `scripts/draft_application_answers.py`, `scripts/application_answers_lib.py` | keep | No-LLM fallback with style rules. |
| Application answers, AI path | `scripts/openai_answers.py` | keep | Consumes the application profile guidance. |
| Application profile load/validate + profile_check gate | `scripts/application_profile.py`, `scripts/profile_check.py` | keep | The maintenance gate in OPERATIONS.md; becomes a unit-tested validator. |
| Resume facts module (experience years, period labels) | `scripts/resume_facts.py` | keep | Clean domain module already; ports nearly as is. |
| Offer evaluation: 6-block report, machine verdict contract | `scripts/evaluate_offer.py` | change | Keep the capability and verdict contract; route its private OpenAI path through the shared provider seam (`scripts/llm_client.py` is bypassed today). |
| Batch prospect evaluation with auto-reject at confidence threshold | `scripts/evaluate_prospects.py` | change | Keep, but auto-reject becomes an explicit opt-in flag with an audit trail; it is the only automated status mutation and it has a `.bak-auto-reject` scar. |
| STAR story bank append | `scripts/evaluate_offer.py` writing `interview-prep/story-bank.md` | change | Keep story capture, add dedupe and a bounded store; the append-only file grows without limit. |
| Pipeline inbox from markdown checklist | `scripts/process_pipeline.py`, `data/pipeline.md` | drop | Lightly used; the capture server and manual add cover the same flow. |
| Provider seam: codex-cli, claude-cli, openai-api, anthropic-api, auto fallback chain | `scripts/llm_client.py` | keep | Env-selected, pluggable, with fallback-on-empty; becomes the only LLM entry point (see the two bypasses above). |

## 4. Outreach and contacts

| Capability | Source | Verdict | Rationale |
|---|---|---|---|
| Contacts store with linked_jobs, application-context vs person-profile split | `scripts/outreach_lib.py`, `tracker/contacts.csv` | keep | Same store decision as the tracker (ADR-003). |
| Staged candidate discovery, approve before persist | `scripts/find_contacts.py` | keep | "Nothing writes contacts directly" is an invariant; staging path preserved. |
| Contact fit scoring and early stop on strong match | `scripts/find_contacts.py` (score_contact_fit, has_strong_best_contact) | keep | The early stop is an Apify cost saver. |
| Bulk contact search over applied rows | `scripts/find_contacts_bulk.py` | keep | Same path, batch driver. |
| AI outreach drafts with audience inference | `scripts/generate_outreach.py`, `scripts/openai_outreach.py` | keep | Draft-only; nothing auto-sends. |
| Deterministic template outreach with lint-and-repair loop | `scripts/outreach_messages.py`, `scripts/outreach_messages_lib.py`, `config/outreach/*.json` | keep | Config-driven, offline-capable parallel path. |
| Outreach queue actions: list, mark-sent, mark-replied, snooze | `scripts/outreach_queue.py` | keep | Manual send discipline preserved. |
| Hunter.io email lookup | `scripts/hunter_lib.py` | keep | Used from the GUI; 50 free credits/month, key via env. |
| LinkedIn poster backfill via guest endpoint | `scripts/backfill_linkedin_posters_guest.py` | keep | Free, idempotent; fix the stale docstring on port. |
| Older Apify-dataset poster backfill | `scripts/backfill_linkedin_publishers.py` | drop | Superseded by the guest variant. |

## 5. Mail and notifications

| Capability | Source | Verdict | Rationale |
|---|---|---|---|
| Gmail watch: OAuth readonly, rule-cascade classification, tracker matching, dedupe state | `scripts/gmail_watch.py`, `scripts/gmail_watch_lib.py` | keep | Pinned by `tests/test_gmail_watch.py`; classification kinds and ignore lists carry over. |
| One-time OAuth consent flow | `scripts/setup_gmail_oauth.py` | keep | Setup path for the token. |
| Daily digest: new prospects, top prospects, outreach due, ghosted (>21 days), Gmail events | `scripts/send_daily_digest.py` | keep | The 20:30 Telegram summary. |
| Telegram sender | `scripts/send_telegram.py` | keep | Shared by discovery, digest, gmail watch. |
| gmail_handler.log as digest data source | root `gmail_handler.log` | change | The append-only JSONL at repo root becomes a proper event log location; digest keeps reading events. |
| Root-level gmail shims | `gmail_handler.py`, `gmail_poller.py` | drop | Legacy adapters for an external caller that no longer exists. |

## 6. Capture server

| Capability | Source | Verdict | Rationale |
|---|---|---|---|
| Localhost capture endpoint for bookmarklets: GET/POST /add, /ping, status contract 200/400/409/500 | `scripts/job_server.py`, `docs/bookmarklets.md` | change | Capability keeps; it becomes endpoints on the FastAPI service instead of a second ad hoc HTTP server. The GET-with-HTML-response trick for mixed-content stays. |
| Fresh config reload per request | `scripts/job_server.py` (importlib reload) | drop | An artifact of the script architecture; the API service reads config normally. |
| Bookmarklets | `docs/bookmarklets.md` | keep | Ported with updated port/URL. |

## 7. GUI (feature inventory, not architecture)

| Capability | Source | Verdict | Rationale |
|---|---|---|---|
| Dashboard: pipeline counts, alerts, funnel, source effectiveness, quick review, run-discovery button | `gui/page_dashboard.py` | keep | Feature parity target for the React app. |
| Tracker page: table, filters, detail, evaluation, application workspace, contact tools | `gui/page_tracker.py` | keep | Largest surface; becomes multiple FSD features. |
| Apply page: resume, cover letter, answers, interview prep, bulk generation | `gui/page_apply.py` | keep | Artifact workflows with PDF-ready state. |
| Outreach page: queue, staged review, contacts, drafts | `gui/page_outreach.py` | keep | Staged-approval UX preserved. |
| System page: scoring settings editor, logs, schedule status, launchctl controls | `gui/page_system.py`, `page_scoring.py`, `page_logs.py` | keep | Scoring editor writes candidate config; log tails become the run-log stream (ADR-004). |
| Orphaned standalone pages | `gui/page_answers.py`, `page_cover_letter.py`, `page_resume.py`, `page_contacts.py`, `page_drafts.py` | drop | Dead code; superseded by tabs in current pages. |
| Auto-rerun after state-changing actions | `gui/app.py` + OPERATIONS.md "GUI Usage" | change | The need (fresh reads after mutation) is met by query invalidation in the new client, not page reruns. |
| Streamlit itself | `gui/` | drop | Decided: retired in favor of React with strict TypeScript. |

## 8. Scheduling and operations

| Capability | Source | Verdict | Rationale |
|---|---|---|---|
| Discovery 4x daily, Apify weekday mornings only, digest 20:30, gmail watch every 5 min | `launchd/*.plist`, `scripts/run-all-intake.sh` | keep | Cadence carries over unchanged (ADR-006). |
| Weekday-morning cost gate in the intake wrapper | `scripts/run-all-intake.sh` | keep | The Apify cost policy, moved into the new CLI so the wrapper stays thin. |
| launchd plists | `launchd/` | change | Regenerated for the new repo path. Two of three currently point at `~/Documents/projects/job-hunt-local` while the repo lives at `~/job-hunt-local`; the rewrite fixes this, and templating removes the hardcoded username. |
| Apify count discrepancy (50 in run-all-intake.sh, 150 in CLAUDE.md, 200 in gui/constants.py) | three files | change | Pick one configured value in one place; the spec for discovery settles the number. |
| Docker topology: job_server, gui, cron scheduler | `Dockerfile`, `docker-compose.yml`, `docker/` | change | Keep a container path for the public demo mode; drop the cron container mirror as a supported production path (launchd is the scheduler of record). Compose currently disables Streamlit XSRF and mounts secrets; the demo container gets neither. |
| Log files layout | `logs/` | change | Keeps per-concern logs; adds structured run logs for the GUI stream. |

## 9. Everything else in the sweep

| Path | Verdict | Rationale |
|---|---|---|
| `_bmad/` | drop | BMAD framework install; nothing in the job-hunt code references it. |
| `downloaded_skills/` + `skills.json` | drop | Marketplace skills unrelated to this project. |
| `.claude/skills/` | drop | BMAD skills, same. |
| `interview-prep/` | keep (data) | Real prep content; migrates into the local database (ADR-008), not public. |
| `reports/` | keep (data) | Evaluation reports; regenerable but useful history. Migrates as private data; existence-gating for re-runs keeps. |
| `templates/resume-template.html` + css, cover-letter templates | keep | Render shells for the PDF pipeline. |
| `templates/job-fit-rubric*`, `job-eval-*` | drop | Human reference docs no code reads; scoring lives in code and config. |
| `templates/openclaw-*.md` | drop | OpenClaw is explicitly out of scope. |
| `mcp.json` | drop | Only read as a Hunter API key fallback; env config replaces it. |
| `examples/outreach/` | change | Becomes part of the synthetic demo fixtures. |
| `deep-research-report.md`, `.idea/`, `.DS_Store` files | drop | One-off artifact and editor noise. |
| `README.md` (11 bytes) | change | The new README is a first-class deliverable. |
| Tests (22 files) | change | Behavior pins carry over as ported tests; gaps noted in the QA plan (no coverage today for evaluate_offer, evaluate_prospects auto-reject, send_daily_digest, remoteok importer). |

## Counts

Keep 59, change 22, drop 16, across 97 matrix rows. Every change and drop above traces to a spec
in the backlog before implementation; nothing is dropped by accident at cutover: the parity
checklist (`docs/parity-checklist.md`) is generated from this table by
`harrier parity checklist`, and `test_parity.py::test_stated_counts_match_the_table` fails
if these totals and the table disagree.

These totals read 58/20/15 until spec 022, which undercounted the table by three rows. The
checklist generator was what noticed: it parsed 96 rows out of a document claiming 93. Spec 023
then moved the feeds watchlist and the hold list from keep to change, since their storage moved
into the database even though their behavior did not. A feature audit on 2026-08-11 added the
vacancy liveness check, which the original sweep missed entirely: the checklist would have been
signed off with that capability silently dropped, which is the exact failure this table exists to
prevent.
