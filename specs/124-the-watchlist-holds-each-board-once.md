---
spec: 124
title: The board watchlist holds each board once
status: accepted
approved: yes
milestone: M9
depends: [008, 023, 025, 041]
---

# Spec 124: The board watchlist holds each board once

## Problem

The `feeds` watchlist (spec 023) is a list of ATS board URLs, edited by
hand, in the browser, or imported from `config/feeds.txt`. Nothing checks
that two entries name the same board. One board is often written two ways:

- Greenhouse serves the same board at `boards.greenhouse.io/<token>`,
  `job-boards.greenhouse.io/<token>` and `job-boards.eu.greenhouse.io/<token>`,
  and discovery fetches every one of them from the same API,
  `boards-api.greenhouse.io/v1/boards/<token>/jobs`
  (`services/api/src/harrier/sources/greenhouse.py`).
- A board pasted twice, with a trailing slash or in different capitals,
  is two entries.

What that costs today, for the operator running discovery:

- **Each duplicate is fetched twice a run.** `harrier.discovery` passes
  every routed URL to its fetcher (`fetch_many`). A Greenhouse fetch asks
  for full content, so a duplicate doubles the heaviest request a board
  makes. In-run dedupe (`dedupe_normalized_jobs`) then throws the second
  copy of every posting away, so the cost buys nothing.
- **Feed health probes it twice** (spec 025) and reports it twice. A dead
  duplicated board is two dead entries.
- **The watchlist reads longer than it is.** The browser's list editor and
  `harrier config get feeds` show every copy, and nothing says which
  entries are the same board.

Duplicates create no duplicate tracker rows: the tracker's dedupe catches
those. The harm is wasted requests and a list the operator cannot trust
at a glance.

## Scope

- `services/api/src/harrier/sources/feeds.py`: `board_identity(url)`, and
  `route_ats_feeds` keeping the first entry of each identity.
- `services/api/src/harrier/userconfig/store.py`: the `feeds` kind's
  validation collapses duplicates, on write and on read.
- `services/api/src/harrier_cli/main.py`: `config set feeds` and `config
  import` name what they collapsed.
- `services/api/src/harrier/discovery.py`: the run reports what routing
  skipped as duplicate.
- Tests under `services/api/tests/`, with synthetic board slugs from the
  declared set in `tests/test_demo.py`.

No schema, contract or web change. `PUT /config/{kind}` already answers
with the stored value, so the browser's editor shows the collapsed list
after a save.

## Behavior

### A board's identity

`board_identity(url)` returns the board a watchlist entry names, or None
when it is not an ATS board URL the importers route:

| Entry | Identity |
|---|---|
| any `*.greenhouse.io` host, first path segment `<token>` | `greenhouse`, `<token>` lowercased |
| `jobs.ashbyhq.com/<board>` | `ashby`, `<board>` lowercased |
| `jobs.lever.co/<company>` | `lever`, `<company>` lowercased |
| `jobs.eu.lever.co/<company>` | `lever-eu`, `<company>` lowercased |

The host decides the provider exactly as `route_ats_feeds` routes today
(label-suffix matches, never substrings). Greenhouse's hosts share one
identity because discovery fetches all of them from one API. Lever's EU
host keeps its own, because discovery fetches it from a different API
(`extract_lever_api_base`). Query strings, fragments, trailing slashes
and further path segments do not change the identity.

An entry with no identity (unrouted, spec 041) is compared as written,
trimmed, so an exact repeat of it is still a duplicate.

### Collapsing

Of two or more entries with one identity, the first in the list is kept,
spelled as it was written; the others are dropped. Order is otherwise
unchanged. Collapsing a list with no duplicates returns it unchanged.

It happens in three places, one function behind all of them:

1. **On write.** `set_config(conn, "feeds", value)` stores the collapsed
   list. This covers the browser's editor, `harrier config set feeds`,
   `harrier config import` and the feed-health prune.
2. **On read.** `get_config` validates the stored value on the way out
   (spec 023), so a list stored before this spec reads collapsed at once,
   without a migration. The stored row changes only on its next write.
3. **On routing.** `route_ats_feeds` collapses before it groups, so the
   file fallback (`config/feeds.txt`, read when no row is stored) is
   fetched once per board too. The file itself is never rewritten.

### Reporting

- `harrier config set feeds` and `harrier config import` print one line
  per dropped entry, "dropped <url>: the same board as <kept url>", after
  the existing "stored" line. Nothing is printed when nothing was dropped.
- A discovery run whose routing dropped duplicates logs one warning naming
  them, and adds a `duplicate` entry to the run summaries with the dropped
  URLs, the way unrouted entries are reported today (spec 041). This can
  only happen while the watchlist comes from the file; a stored list is
  already collapsed.
- Feed health (spec 025) probes the collapsed list, so it reports each
  board once.

## Failure modes

- **Two boards that differ only in capitals are really different boards.**
  They are collapsed. No routed provider is known to treat a board name's
  case as significant; if one does, that provider's identity drops the
  lowercasing in an amendment (see open questions).
- **An entry the router cannot place** is never given a provider identity,
  so it can only collapse with an exact repeat of itself.
- **A malformed URL** (no host, no path) gets no identity and is compared
  as written, as today.
- **The file fallback has duplicates.** Discovery fetches each board once
  and the run names the duplicates; the file is left as it is.
- **A stored list from before this spec.** It reads collapsed. Writing it
  back is the operator's choice, made by any save; nothing writes it
  automatically, because a write would also stop the file fallback.
- **The feed-health prune** writes back `report.results` minus the dead
  boards. The report is built from the collapsed list, so the prune
  writes a collapsed list.
- Must not introduce: a write to `config/feeds.txt`; a stored row created
  by a read; a duplicate fetched twice in one run; a reordering of the
  entries kept.

## Acceptance criteria

Tests in `services/api/tests/test_sources.py` unless named otherwise. Planned
test names are given without backticks until the tests exist. Every board
slug is a declared synthetic one.

- [ ] `boards.greenhouse.io/acme`, `job-boards.greenhouse.io/acme/` and
      `job-boards.eu.greenhouse.io/ACME?gh_src=x` have one identity
      (planned test_greenhouse_hosts_share_an_identity)
- [ ] `jobs.lever.co/acme` and `jobs.eu.lever.co/acme` have different
      identities (planned test_lever_eu_is_its_own_board)
- [ ] An unrouted entry has no identity and collapses only with an exact
      repeat (planned test_an_unrouted_entry_collapses_only_when_repeated)
- [ ] Collapsing keeps the first entry of each identity, spelled as
      written, in order, and leaves a list with no duplicates unchanged
      (planned test_collapsing_keeps_the_first_spelling_and_the_order)
- [ ] `set_config(conn, "feeds", ...)` stores the collapsed list, and a
      duplicated list stored directly reads collapsed through `get_config`
      without its row changing
      (`services/api/tests/test_userconfig.py`, planned
      test_feeds_are_collapsed_on_write_and_on_read)
- [ ] A discovery run over a file watchlist with a duplicate fetches the
      board once and reports the dropped URL as `duplicate`
      (`services/api/tests/test_discovery.py`, planned
      test_a_duplicate_board_is_fetched_once)
- [ ] `harrier config set feeds` names each dropped entry and the entry it
      duplicated; with no duplicates it prints only "stored"
      (planned test_config_set_names_what_it_dropped)
- [ ] Feed health reports a duplicated board once
      (`services/api/tests/test_feed_health.py`, planned
      test_a_duplicated_board_is_probed_once)
- [ ] `config/feeds.txt` is never written
      (planned test_the_feeds_file_is_never_rewritten)
- [ ] `just check` passes

## Proof / origin

- The routing rule and its label-suffix matching: spec 008 (port of the
  old repo's `scripts/job_sources.py` feeds routing) and spec 041
  (unrouted entries reported, not dropped in silence).
- Validation on write and on read: spec 023.
- Greenhouse's single API host: `fetch_greenhouse_jobs` in
  `services/api/src/harrier/sources/greenhouse.py`.
- Lever's EU API: `extract_lever_api_base` in
  `services/api/src/harrier/sources/lever.py`.
- No old-repo origin for dedupe: the old `feeds.txt` reader kept every
  line.

## Out of scope

- **A company watchlist** with fit, geography, route, evidence and next
  action per company. Proposed separately, if at all.
- **Checking that a board exists.** That is feed health (spec 025).
- **Merging boards across providers** (one company on Greenhouse and on
  Ashby). Two providers are two boards.
- **LinkedIn searches.** The `linkedin_searches` kind is not touched.
- **Rewriting `config/feeds.txt`.**
- **The browser editor.** It already shows what the store answers.

## Migration

None. A stored watchlist reads collapsed from the first start after this
lands, and is stored collapsed on its next save. A file watchlist is
fetched collapsed and left as written.

## Decided in review (2026-10-11)

1. **Case.** Every provider's board name is compared lowercased.
2. **Reporting in the browser.** Left out: the browser shows the collapsed
   list after a save, and naming what was dropped there would need a
   contract change.
