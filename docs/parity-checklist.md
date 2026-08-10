# Parity checklist

Generated from `docs/parity-matrix.md` by `harrier parity checklist`, and
committed on purpose. The skeleton is derived, but the ticks and waivers are
not: they are a human's review record, reproducible from nothing, and losing
them means redoing every item. That is the same reason
`packages/contract/openapi.json` is committed rather than ignored, and like
that file this one is drift-checked, by
`test_parity.py::test_the_committed_checklist_matches_the_matrix`.

Do not edit the item text. Tick an item when you have verified it, or waive it
with a reason:

    - [x] `slug` capability ... (waived: reason)

Regenerating preserves ticks and waivers for items that still exist and lists
any that the matrix no longer carries. Waiver reasons are committed to a public
repository: keep them about the capability, never about a company or a person.


96 items: 60 keep, 20 change, 16 drop.

## 1. Discovery pipeline

- [ ] `shared-normalized-job-schema` **Shared normalized job schema** (keep: behavior identical, proof named) source: `scripts/job_sources.py` (make_normalized_job)
- [ ] `screening-gate-order-seen-state-hold-list-title-remote-emea-dedupe-score` **Screening gate order: seen-state, hold list, title, remote/EMEA, dedupe, score cutoff 55** (keep: behavior identical, proof named) source: `scripts/job_sources.py` (screen_jobs)
- [ ] `remote-only-and-emea-enforcement-incl-location-only-negative-hints-and-t` **Remote-only and EMEA enforcement, incl. location-only negative hints and the linkedin_search bypass** (keep: behavior identical, proof named) source: `scripts/job_sources.py` (remote_region_allowed)
- [ ] `eu-permit-and-eu-entity-phrases-as-positive-signals-never-filters` **EU-permit and EU-entity phrases as positive signals, never filters** (keep: behavior identical, proof named) source: `scripts/job_sources.py` (PREFERRED_SIGNAL_WEIGHTS), `CLAUDE.md` "Candidate EU status"
- [ ] `scoring-model-base-30-title-keywords-skill-signals-region-domain-bonus-c` **Scoring model (base 30, title, keywords, skill signals, region, domain bonus, cap 120)** (keep: behavior identical, proof named) source: `scripts/job_sources.py` (score_job, DEFAULT_SCORING)
- [ ] `archetype-detection` **Archetype detection** (change: difference verified intentional) source: `scripts/job_sources.py` (detect_archetype) + two more copies
- [ ] `description-cache-keyed-by-url-hash` **Description cache keyed by URL hash** (keep: behavior identical, proof named) source: `state/job-descriptions/` (4,079 files)
- [ ] `jd-enrichment-fetch-for-short-descriptions-on-ats-hosts` **JD enrichment fetch for short descriptions on ATS hosts** (keep: behavior identical, proof named) source: `scripts/job_sources.py` (enrich_job_description_for_scoring)
- [ ] `per-source-seen-state-capped-at-10-000-keys` **Per-source seen-state, capped at 10,000 keys** (keep: behavior identical, proof named) source: `state/job-discovery/*_seen.json`
- [ ] `notes-column-as-key-value-store-score-signals-external-key` **notes column as key=value store (score=, signals=, external_key=, ...)** (change: difference verified intentional) source: `scripts/job_sources.py` (extract_note_value)
- [ ] `greenhouse-importer` **Greenhouse importer** (keep: behavior identical, proof named) source: `scripts/import_greenhouse_jobs.py`
- [ ] `ashby-importer-incl-html-fallback-on-api-404` **Ashby importer incl. HTML fallback on API 404** (keep: behavior identical, proof named) source: `scripts/import_ashby_jobs.py`
- [ ] `lever-importer-incl-eu-api-base-selection` **Lever importer incl. EU API base selection** (keep: behavior identical, proof named) source: `scripts/import_lever_jobs.py`
- [ ] `remoteok-importer` **RemoteOK importer** (keep: behavior identical, proof named) source: `scripts/import_remoteok_jobs.py`
- [ ] `apify-linkedin-importer-run-lifecycle-dataset-file-mode-caching-all-desc` **Apify LinkedIn importer: run lifecycle, dataset-file mode, caching all descriptions even for rejected items** (keep: behavior identical, proof named) source: `scripts/import_apify_linkedin_jobs.py`
- [ ] `auto-linking-linkedin-job-poster-into-contacts-on-accept` **Auto-linking LinkedIn job poster into contacts on accept** (change: difference verified intentional) source: `scripts/import_apify_linkedin_jobs.py` (link_publisher_contacts)
- [ ] `wellfound-batch-import` **Wellfound batch import** (keep: behavior identical, proof named) source: `scripts/import_wellfound_export.py`
- [ ] `wttj-batch-import` **WTTJ batch import** (keep: behavior identical, proof named) source: `scripts/import_wttj_export.py`
- [ ] `orchestrator-priority-order-single-aggregated-telegram-notify-run-summar` **Orchestrator: priority order, single aggregated Telegram notify, run summary JSON** (keep: behavior identical, proof named) source: `scripts/run-job-imports.py`
- [ ] `legacy-orchestrator-incl-workable-support` **Legacy orchestrator incl. Workable support** (drop: confirmed dropped on purpose) source: `scripts/orchestrate_job_search.py`
- [ ] `feeds-txt-one-url-per-line-routing-by-netloc` **feeds.txt one-URL-per-line routing by netloc** (keep: behavior identical, proof named) source: `config/feeds.txt`, `scripts/run-job-imports.py` (parse_ats_feeds)
- [ ] `per-source-board-files` **Per-source board files** (drop: confirmed dropped on purpose) source: `config/greenhouse_boards.txt`, `ashby_boards.txt`, `lever_boards.txt`
- [ ] `company-hold-list` **Company hold list** (keep: behavior identical, proof named) source: `config/companies-hold.csv`

## 2. Tracker

- [ ] `single-source-of-truth-for-application-state` **Single source of truth for application state** (change: difference verified intentional) source: `tracker/jobs.csv`
- [ ] `20-column-tracker-schema` **20-column tracker schema** (change: difference verified intentional) source: `scripts/job_sources.py` (TRACKER_FIELDS), duplicated in `scripts/jobs.py` and `gui/constants.py`
- [ ] `status-lifecycle-prospect-shortlisted-tailored-cv-requested-applied-inte` **Status lifecycle prospect, shortlisted, tailored_cv_requested, applied, interviewing, rejected** (keep: behavior identical, proof named) source: `scripts/jobs.py:43`
- [ ] `orthogonal-outreach-status-axis` **Orthogonal outreach status axis** (keep: behavior identical, proof named) source: `scripts/outreach_lib.py`
- [ ] `tracker-cli-verbs-shortlist-tailor-applied-interviewing-reject-track-add` **Tracker CLI verbs: shortlist, tailor, applied, interviewing, reject, track, add, next, review, reevaluate, answer, evaluate** (keep: behavior identical, proof named) source: `scripts/jobs.py`
- [ ] `pdf-existence-gate-on-tailor-row-unchanged-on-failure` **PDF-existence gate on `tailor` (row unchanged on failure)** (keep: behavior identical, proof named) source: `scripts/jobs.py` (run_tailor_resume)
- [ ] `applied-seeds-the-outreach-block-and-follow-up-date` **`applied` seeds the outreach block and follow-up date** (keep: behavior identical, proof named) source: `scripts/jobs.py:394`
- [ ] `selector-semantics-numeric-row-unique-substring-abort-on-ambiguity` **Selector semantics: numeric row, unique substring, abort on ambiguity** (keep: behavior identical, proof named) source: `scripts/jobs.py` (resolve_selector)
- [ ] `row-repair-column-backfill` **Row repair / column backfill** (drop: confirmed dropped on purpose) source: `scripts/jobs.py` (repair_tracker_row)
- [ ] `manual-add-with-dedupe-and-scoring` **Manual add with dedupe and scoring** (keep: behavior identical, proof named) source: `scripts/jobs.py` (add)
- [ ] `tracker-backups-bak-files` **Tracker backups (`*.bak-*` files)** (change: difference verified intentional) source: `tracker/`

## 3. Artifact generation

- [ ] `tailored-resume-md-html-pdf-metadata-sidecar` **Tailored resume: md, html, pdf, metadata sidecar** (keep: behavior identical, proof named) source: `scripts/tailor_resume.py`
- [ ] `verified-content-only-rule-ai-selects-and-orders-bullet-ids-never-invent` **Verified-content-only rule: AI selects and orders bullet IDs, never invents** (keep: behavior identical, proof named) source: `scripts/tailor_resume.py` (require_truth, validate_content_plan)
- [ ] `bullet-pool-embedded-in-script-source` **Bullet pool embedded in script source** (change: difference verified intentional) source: `scripts/tailor_resume.py` (BULLET_POOL)
- [ ] `pdf-validation-non-empty-no-replacement-chars-no-unresolved-placeholders` **PDF validation: non-empty, no replacement chars, no unresolved placeholders, page count 1** (keep: behavior identical, proof named) source: `scripts/tailor_resume.py` (validate_rendered_pdf)
- [ ] `internal-label-scrubbing-from-recruiter-facing-output` **Internal-label scrubbing from recruiter-facing output** (keep: behavior identical, proof named) source: `scripts/tailor_resume.py` (normalize_visible_role_title, build_internal_metadata)
- [ ] `deterministic-fallback-plan-when-llm-unavailable` **Deterministic fallback plan when LLM unavailable** (keep: behavior identical, proof named) source: `scripts/tailor_resume.py`
- [ ] `cover-letters-3-paragraphs-banned-phrases-pdf-required-pdf-html-contain-` **Cover letters: 3 paragraphs, banned phrases, PDF-required, PDF/HTML contain only the full letter** (keep: behavior identical, proof named) source: `scripts/openai_cover_letters.py`, `scripts/generate_cover_letter.py`
- [ ] `application-answers-deterministic-path` **Application answers, deterministic path** (keep: behavior identical, proof named) source: `scripts/draft_application_answers.py`, `scripts/application_answers_lib.py`
- [ ] `application-answers-ai-path` **Application answers, AI path** (keep: behavior identical, proof named) source: `scripts/openai_answers.py`
- [ ] `application-profile-load-validate-profile-check-gate` **Application profile load/validate + profile_check gate** (keep: behavior identical, proof named) source: `scripts/application_profile.py`, `scripts/profile_check.py`
- [ ] `resume-facts-module-experience-years-period-labels` **Resume facts module (experience years, period labels)** (keep: behavior identical, proof named) source: `scripts/resume_facts.py`
- [ ] `offer-evaluation-6-block-report-machine-verdict-contract` **Offer evaluation: 6-block report, machine verdict contract** (change: difference verified intentional) source: `scripts/evaluate_offer.py`
- [ ] `batch-prospect-evaluation-with-auto-reject-at-confidence-threshold` **Batch prospect evaluation with auto-reject at confidence threshold** (change: difference verified intentional) source: `scripts/evaluate_prospects.py`
- [ ] `star-story-bank-append` **STAR story bank append** (change: difference verified intentional) source: `scripts/evaluate_offer.py` writing `interview-prep/story-bank.md` (641 KB)
- [ ] `pipeline-inbox-from-markdown-checklist` **Pipeline inbox from markdown checklist** (drop: confirmed dropped on purpose) source: `scripts/process_pipeline.py`, `data/pipeline.md`
- [ ] `provider-seam-codex-cli-claude-cli-openai-api-anthropic-api-auto-fallbac` **Provider seam: codex-cli, claude-cli, openai-api, anthropic-api, auto fallback chain** (keep: behavior identical, proof named) source: `scripts/llm_client.py`

## 4. Outreach and contacts

- [ ] `contacts-store-with-linked-jobs-application-context-vs-person-profile-sp` **Contacts store with linked_jobs, application-context vs person-profile split** (keep: behavior identical, proof named) source: `scripts/outreach_lib.py`, `tracker/contacts.csv`
- [ ] `staged-candidate-discovery-approve-before-persist` **Staged candidate discovery, approve before persist** (keep: behavior identical, proof named) source: `scripts/find_contacts.py`
- [ ] `contact-fit-scoring-and-early-stop-on-strong-match` **Contact fit scoring and early stop on strong match** (keep: behavior identical, proof named) source: `scripts/find_contacts.py` (score_contact_fit, has_strong_best_contact)
- [ ] `bulk-contact-search-over-applied-rows` **Bulk contact search over applied rows** (keep: behavior identical, proof named) source: `scripts/find_contacts_bulk.py`
- [ ] `ai-outreach-drafts-with-audience-inference` **AI outreach drafts with audience inference** (keep: behavior identical, proof named) source: `scripts/generate_outreach.py`, `scripts/openai_outreach.py`
- [ ] `deterministic-template-outreach-with-lint-and-repair-loop` **Deterministic template outreach with lint-and-repair loop** (keep: behavior identical, proof named) source: `scripts/outreach_messages.py`, `scripts/outreach_messages_lib.py`, `config/outreach/*.json`
- [ ] `outreach-queue-actions-list-mark-sent-mark-replied-snooze` **Outreach queue actions: list, mark-sent, mark-replied, snooze** (keep: behavior identical, proof named) source: `scripts/outreach_queue.py`
- [ ] `hunter-io-email-lookup` **Hunter.io email lookup** (keep: behavior identical, proof named) source: `scripts/hunter_lib.py`
- [ ] `linkedin-poster-backfill-via-guest-endpoint` **LinkedIn poster backfill via guest endpoint** (keep: behavior identical, proof named) source: `scripts/backfill_linkedin_posters_guest.py`
- [ ] `older-apify-dataset-poster-backfill` **Older Apify-dataset poster backfill** (drop: confirmed dropped on purpose) source: `scripts/backfill_linkedin_publishers.py`

## 5. Mail and notifications

- [ ] `gmail-watch-oauth-readonly-rule-cascade-classification-tracker-matching-` **Gmail watch: OAuth readonly, rule-cascade classification, tracker matching, dedupe state** (keep: behavior identical, proof named) source: `scripts/gmail_watch.py`, `scripts/gmail_watch_lib.py`
- [ ] `one-time-oauth-consent-flow` **One-time OAuth consent flow** (keep: behavior identical, proof named) source: `scripts/setup_gmail_oauth.py`
- [ ] `daily-digest-new-prospects-top-prospects-outreach-due-ghosted-21-days-gm` **Daily digest: new prospects, top prospects, outreach due, ghosted (>21 days), Gmail events** (keep: behavior identical, proof named) source: `scripts/send_daily_digest.py`
- [ ] `telegram-sender` **Telegram sender** (keep: behavior identical, proof named) source: `scripts/send_telegram.py`
- [ ] `gmail-handler-log-as-digest-data-source` **gmail_handler.log as digest data source** (change: difference verified intentional) source: root `gmail_handler.log`
- [ ] `root-level-gmail-shims` **Root-level gmail shims** (drop: confirmed dropped on purpose) source: `gmail_handler.py`, `gmail_poller.py`

## 6. Capture server

- [ ] `localhost-capture-endpoint-for-bookmarklets-get-post-add-ping-status-con` **Localhost capture endpoint for bookmarklets: GET/POST /add, /ping, status contract 200/400/409/500** (change: difference verified intentional) source: `scripts/job_server.py`, `docs/bookmarklets.md`
- [ ] `fresh-config-reload-per-request` **Fresh config reload per request** (drop: confirmed dropped on purpose) source: `scripts/job_server.py` (importlib reload)
- [ ] `bookmarklets` **Bookmarklets** (keep: behavior identical, proof named) source: `docs/bookmarklets.md`

## 7. GUI (feature inventory, not architecture)

- [ ] `dashboard-pipeline-counts-alerts-funnel-source-effectiveness-quick-revie` **Dashboard: pipeline counts, alerts, funnel, source effectiveness, quick review, run-discovery button** (keep: behavior identical, proof named) source: `gui/page_dashboard.py`
- [ ] `tracker-page-table-filters-detail-evaluation-application-workspace-conta` **Tracker page: table, filters, detail, evaluation, application workspace, contact tools** (keep: behavior identical, proof named) source: `gui/page_tracker.py`
- [ ] `apply-page-resume-cover-letter-answers-interview-prep-bulk-generation` **Apply page: resume, cover letter, answers, interview prep, bulk generation** (keep: behavior identical, proof named) source: `gui/page_apply.py`
- [ ] `outreach-page-queue-staged-review-contacts-drafts` **Outreach page: queue, staged review, contacts, drafts** (keep: behavior identical, proof named) source: `gui/page_outreach.py`
- [ ] `system-page-scoring-settings-editor-logs-schedule-status-launchctl-contr` **System page: scoring settings editor, logs, schedule status, launchctl controls** (keep: behavior identical, proof named) source: `gui/page_system.py`, `page_scoring.py`, `page_logs.py`
- [ ] `orphaned-standalone-pages` **Orphaned standalone pages** (drop: confirmed dropped on purpose) source: `gui/page_answers.py`, `page_cover_letter.py`, `page_resume.py`, `page_contacts.py`, `page_drafts.py`
- [ ] `auto-rerun-after-state-changing-actions` **Auto-rerun after state-changing actions** (change: difference verified intentional) source: `gui/app.py` + OPERATIONS.md "GUI Usage"
- [ ] `streamlit-itself` **Streamlit itself** (drop: confirmed dropped on purpose) source: `gui/`

## 8. Scheduling and operations

- [ ] `discovery-4x-daily-apify-weekday-mornings-only-digest-20-30-gmail-watch-` **Discovery 4x daily, Apify weekday mornings only, digest 20:30, gmail watch every 5 min** (keep: behavior identical, proof named) source: `launchd/*.plist`, `scripts/run-all-intake.sh`
- [ ] `weekday-morning-cost-gate-in-the-intake-wrapper` **Weekday-morning cost gate in the intake wrapper** (keep: behavior identical, proof named) source: `scripts/run-all-intake.sh`
- [ ] `launchd-plists` **launchd plists** (change: difference verified intentional) source: `launchd/`
- [ ] `apify-count-discrepancy-50-in-run-all-intake-sh-150-in-claude-md-200-in-` **Apify count discrepancy (50 in run-all-intake.sh, 150 in CLAUDE.md, 200 in gui/constants.py)** (change: difference verified intentional) source: three files
- [ ] `docker-topology-job-server-gui-cron-scheduler` **Docker topology: job_server, gui, cron scheduler** (change: difference verified intentional) source: `Dockerfile`, `docker-compose.yml`, `docker/`
- [ ] `log-files-layout` **Log files layout** (change: difference verified intentional) source: `logs/`

## 9. Everything else in the sweep

- [ ] `bmad` **`_bmad/`** (drop: confirmed dropped on purpose)
- [ ] `downloaded-skills-skills-json` **`downloaded_skills/` + `skills.json`** (drop: confirmed dropped on purpose)
- [ ] `claude-skills` **`.claude/skills/`** (drop: confirmed dropped on purpose)
- [ ] `interview-prep` **`interview-prep/`** (keep (data): behavior identical, proof named)
- [ ] `reports` **`reports/`** (keep (data): behavior identical, proof named)
- [ ] `templates-resume-template-html-css-cover-letter-templates` **`templates/resume-template.html` + css, cover-letter templates** (keep: behavior identical, proof named)
- [ ] `templates-job-fit-rubric-job-eval` **`templates/job-fit-rubric*`, `job-eval-*`** (drop: confirmed dropped on purpose)
- [ ] `templates-openclaw-md` **`templates/openclaw-*.md`** (drop: confirmed dropped on purpose)
- [ ] `mcp-json` **`mcp.json`** (drop: confirmed dropped on purpose)
- [ ] `examples-outreach` **`examples/outreach/`** (change: difference verified intentional)
- [ ] `deep-research-report-md-idea-ds-store-files` **`deep-research-report.md`, `.idea/`, `.DS_Store` files** (drop: confirmed dropped on purpose)
- [ ] `readme-md-11-bytes` **`README.md` (11 bytes)** (change: difference verified intentional)
- [ ] `tests-22-files` **Tests (22 files)** (change: difference verified intentional)
