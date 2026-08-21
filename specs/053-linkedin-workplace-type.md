---
spec: 053
title: LinkedIn discovery rejects postings that declare hybrid or on-site
status: accepted
approved: yes
milestone: M8
depends: [009, 032, 033]
---

# Spec 053: LinkedIn discovery rejects postings that declare hybrid or on-site

## Problem

Discovery surfaces hybrid and on-site LinkedIn postings as prospects. The
remote-only invariant says these are rejected on location signals, but for
the Apify source the location signal never arrives.

Two facts combine into the leak:

1. The gate trusts the query. `remote_region_allowed`
   (services/api/src/harrier/screening/rules.py) accepts any job whose
   `remote_signal` is `linkedin_search` outright, on the recorded rationale
   that the search URLs carry LinkedIn's remote filter (`f_WT=2`). LinkedIn
   has retired that filter: its AI job search removed the classic workplace
   filter, and the actor (`curious_coder/linkedin-jobs-scraper`, the default
   in services/api/src/harrier/sources/apify_linkedin.py) now converts
   `f_WT=2` into natural-language search keywords. A keyword is a relevance
   hint, not a filter, so hybrid and on-site postings arrive in the dataset.
2. The normalizer drops the field that would catch them. The actor emits a
   workplace declaration per item (`workplaceTypes`, with
   `workRemoteAllowed` beside it; both listed on the actor's output
   schema). `normalize_apify_job` reads neither. The `location` it forwards
   is a bare geography ("Berlin, Germany": the shape observed in this
   machine's discovery output), so the location gate has no negative hint to
   match, and the unconditional `remote_signal="linkedin_search"` then
   accepts the job.

Every source is affected only through this one: the other sources either
publish remote-only boards or carry the workplace type in their location
field already (lever.py joins `workplaceType` into `location`; spec 032).

## Scope

Three files change, plus their tests:

- `services/api/src/harrier/sources/apify_linkedin.py`: the normalizer reads
  the workplace declaration and renders it into `location`.
- `services/api/src/harrier/screening/rules.py`: the `linkedin_search`
  branch of `remote_region_allowed` stops accepting unconditionally; the
  floor derivation comment is corrected.
- `config/linkedin_search_urls.example.txt`: the comment stops claiming the
  retired query-level guarantee.
- Tests in `services/api/tests/test_apify_linkedin.py`,
  `test_screening_location.py`, and `test_scoring.py`.

No other source module, no pipeline change, no tracker change, no contract
change.

## Behavior

Two changes, one per seam the leak crosses. Ingestion stays ingestion: the
source module normalizes the provider's workplace declaration into the
shared job shape, and the decision stays in the shared screening path.

### 1. The normalizer renders the workplace declaration into `location`

`normalize_apify_job` reads the item's workplace declaration and prefixes
it to the location string, following the lever.py precedent:

- Recognized `workplaceTypes` entries, matched case-insensitively after
  trimming: `remote`, `hybrid`, `on-site` (also accepted as `onsite`).
  Unrecognized entries are ignored. A bare string value is treated as a
  one-entry list.
- When no recognized entry exists and `workRemoteAllowed` is `true`, the
  declaration is `Remote`. A `false` or missing `workRemoteAllowed` adds
  nothing: `false` is what the scraper reports for hybrid, on-site, and
  missing data alike, so it cannot name which one and is not treated as a
  declaration.
- The declared types are joined with `" | "` (the existing alternative
  separator in `LOCATION_SEPARATORS`) and prefixed to the geographic
  location with `", "`. Examples:
  - `workplaceTypes: ["Hybrid"]`, location `Berlin, Germany` becomes
    `Hybrid, Berlin, Germany`
  - `workplaceTypes: ["Remote"]`, location `Warsaw, Poland` becomes
    `Remote, Warsaw, Poland`
  - `workplaceTypes: ["Remote", "Hybrid"]` becomes
    `Remote | Hybrid, Warsaw, Poland`: alternatives, per the existing
    any-alternative-qualifies rule of `split_locations`
  - no declaration: location is unchanged
- An empty geographic location with a declaration yields the declaration
  alone (`Hybrid`).

This makes the existing location gate fire with no new rule: `Hybrid,
Berlin, Germany` is one segment, matches the `hybrid` negative hint, and is
rejected as "location says hybrid/on-site". The dataset-file replay path
uses the same normalizer, so replayed runs are covered identically.

### 2. `linkedin_search` stops meaning "remote is query-guaranteed"

In `remote_region_allowed`, the `linkedin_search` branch changes from
unconditional acceptance to: accept only when `REMOTE_POSITIVE_PATTERNS`
matches the combined title, location, and description text; otherwise
reject with the existing "remote signal missing" reason.

The region bypass stays. `geoId` and `f_TPR` remain real URL filters on
LinkedIn's AI search (the actor's schema carries `geoId` as a live input),
so the searches are still EMEA-scoped at query level, and requiring
preferred-region wording in the text would re-reject valid EMEA postings
whose text names only a city: the defect specs 032 and 033 removed. The
branch comment in rules.py is rewritten to state the new rationale, since
the current one records a guarantee that no longer exists.

A posting that declares remote passes this test by construction: its
location now begins with `Remote`. A posting with no declaration passes
only if its own text says remote. A posting that declares hybrid or
on-site never reaches this branch: the location gate rejected it first.

### 3. The example config stops claiming the retired guarantee

The comment in `config/linkedin_search_urls.example.txt` claims
`f_WT=2 = Remote`. It is rewritten to say the parameter is a relevance
hint under LinkedIn's AI search and that remote-only enforcement happens
in screening. The URLs themselves keep `f_WT=2`: as a converted keyword it
still biases results toward remote, which reduces paid results discarded
by screening.

### Scoring side effect

The LinkedIn arithmetic floor derived in rules.py (`SCORE_FLOOR_NOTE`, spec
033) changes: a LinkedIn posting that passes the gate now always matched
`REMOTE_POSITIVE_PATTERNS` over the text the remote bonus reads, so its
floor rises by the remote bonus. The derivation comment and
`tests/test_scoring.py::test_the_arithmetic_floor_is_derived_from_the_rules`
are updated in the same change; the test derives the floor from the rules,
so it fails until the derivation is corrected, which is the point of it.

## Failure modes

- `workplaceTypes` holds an unrecognized value (a future rename, a numeric
  code): the entry is ignored. If nothing recognized remains, the job takes
  the no-declaration path and must evidence remote in its own text.
- `workplaceTypes` is present but not a list or string: ignored, same
  no-declaration path. Malformed provider data never crashes ingestion.
- Declaration contradicts the flag (`workplaceTypes: ["Hybrid"]`,
  `workRemoteAllowed: true`): `workplaceTypes` wins; the explicit
  declaration outranks the derived flag.
- Both remote and a non-remote type are declared: treated as alternatives;
  the remote alternative qualifies the posting, consistent with how one
  location field naming several offices already works.
- Location text that itself contains a workplace word ("Remote, Berlin"
  from the provider) plus a declaration: the prefix duplicates a word, the
  gates match the same way, no harm.
- A posting with no declaration, a bare-city location, and no remote
  wording anywhere: rejected as "remote signal missing". This will reject
  some genuinely remote postings whose text never says so; that is the
  chosen trade-off and is stated in Limitations.

## Acceptance criteria

- An Apify item with `workplaceTypes: ["Hybrid"]` and location
  `Berlin, Germany` normalizes to location `Hybrid, Berlin, Germany` and
  `remote_region_allowed` rejects it with "location says hybrid/on-site".
  Same for `["On-site"]` and for `["Onsite"]` casing variants.
- An item with `workplaceTypes: ["Remote"]` normalizes to
  `Remote, <original location>` and is accepted.
- An item with `workplaceTypes: ["Remote", "Hybrid"]` is accepted.
- An item with no `workplaceTypes` and `workRemoteAllowed: true` is
  accepted via the `Remote` prefix.
- An item with no declaration and no remote wording in title, location, or
  description is rejected with "remote signal missing" even though its
  `remote_signal` is `linkedin_search`.
- An item with no declaration whose description says remote is accepted,
  and no preferred-region wording is required of it (region stays
  query-scoped for `linkedin_search`).
- `remote_signal="remote_only_board"` still accepts with no text
  requirement: the other sources' behavior is unchanged, pinned by the
  existing `test_a_remote_only_board_signal_is_honoured`.
- The LinkedIn floor derivation in
  `tests/test_scoring.py::test_the_arithmetic_floor_is_derived_from_the_rules`
  reflects the new gate and passes.
- New tests live in `services/api/tests/test_apify_linkedin.py` (normalizer
  mapping) and `services/api/tests/test_screening_location.py` (gate
  semantics), and each fails without its change.

## Proof / origin

Hybrid and on-site prospects were observed in this machine's discovery
output; the surviving rows there carry bare-city locations ("Berlin,
Germany", "Istanbul, Türkiye": `data/incoming/apify_linkedin_latest.json`,
never-in-git), which is the shape the gate cannot catch.

The retirement of the workplace filter is stated by the actor itself: the
input schema for `curious_coder/linkedin-jobs-scraper` documents
`autoConvertToAiSearch` as "LinkedIn is now forcing AI job search which
removed many classic filters (experience, job type, workplace, salary,
sort, etc.)", converting them to natural-language keywords. The same
actor's output documentation lists `workplaceTypes` ("Types of workplace
arrangements possible (on-site, remote, hybrid)") and `workRemoteAllowed`
among the per-item fields.

The trusting branch is `remote_region_allowed` in
`services/api/src/harrier/screening/rules.py`; the unconditional stamp is
`normalize_apify_job` in `services/api/src/harrier/sources/apify_linkedin.py`.
The lever precedent for carrying a provider workplace type in `location` is
spec 032 and `services/api/src/harrier/sources/lever.py`.

## Out of scope

- Scanning descriptions for hybrid or on-site words. The negative-hint
  scoping to the location field is deliberate and pinned (spec 032); a
  description saying "unlike hybrid roles, we are fully remote" must keep
  passing.
- Re-screening existing tracker rows or stored seen verdicts. Hybrid
  prospects already in the tracker stay until handled by hand.
- The actor input drift (`count` vs the schema's current `limitPerSource`,
  `autoConvertToAiSearch` not being pinned). This is a cost and coverage
  concern, not a correctness one, and belongs to its own spec.
- Any change to the other job sources.

## Limitations

- "Never surface hybrid or on-site" is enforceable only over what the data
  declares. A posting that declares remote (or says remote in its text)
  while actually requiring office days passes; no filter on this data can
  catch a lie. The guarantee this spec makes, stated precisely: no posting
  whose workplace declaration or location text offers hybrid or on-site
  without a remote alternative is surfaced. A posting declared as remote
  or hybrid qualifies on its remote alternative, by the same
  any-alternative rule `split_locations` already applies to a location
  naming several offices; the title's "declare hybrid or on-site" is this
  guarantee's shorthand, not a stricter one.
- The stricter no-declaration path trades recall for precision: a real
  remote posting whose text never says remote is now rejected. Rejections
  are visible in the existing rejected-debug output for tuning.

## Migration

None. Config files keep their shape; the example comment change is
documentation. Existing tracker rows are untouched.
