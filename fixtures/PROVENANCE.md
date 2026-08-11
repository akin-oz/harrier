# Fixture provenance

Every file in this directory is **authored**, not recorded. None was captured
from a live third-party service, so none carries another party's terms and
none describes anyone's real job search (ADR-008).

That matters for two separate reasons. A response recorded from a provider is
that provider's content, and redistributing it under this repository's licence
would be a claim the project cannot make. And a recorded response from a real
board is a real posting, which is the personal-data problem one level down.

| File | What it is |
|---|---|
| `demo-jobs.json` | Synthetic tracker rows for demo mode. Invented companies at `example.com` hosts. |
| `http/index.json` | Maps fixture URLs to the files below, so demo mode reaches no network. |
| `http/greenhouse-exampleco.json` | A Greenhouse board response, hand-written to the shape the importer parses. |
| `http/ashby-exampleco.json` | The same, for Ashby. |
| `http/lever-exampleco.json` | The same, for Lever. |
| `http/lever-example-eu-co.json` | The Lever EU host variant, which uses a different API base. |
| `http/remoteok.json` | The RemoteOK feed shape. |

The shapes are copied from the providers' published response formats, which is
a fact about their API rather than a copy of their content. The values are
invented.

Some fixtures do carry a real provider hostname, deliberately. The importers
route on hostname, so `jobs.ashbyhq.com` is what makes the fixture exercise
the real routing path; the host is a protocol fact. What would reveal a
recording is the board slug after it, which names an actual company and its
actual postings. Every slug here is invented.

Checked by `services/api/tests/test_publishable.py`, which asserts that every
fixture is listed here and that no fixture names a board slug outside the
invented set.
