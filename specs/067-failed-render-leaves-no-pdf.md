---
spec: 067
title: A render or PDF gate failure leaves no PDF where the Apply page would serve it
status: accepted
approved: yes
milestone: M8
depends: [013, 014, 034, 047, 065]
---

# Spec 067: A render or PDF gate failure leaves no PDF where the Apply page would serve it

## Problem

The product invariant says resume and cover letter generation succeed only
if the PDF exists and validates. The generation step honours it: it raises.
The files it leaves behind do not.

Both writers render straight to the final path, then validate the file that
is already there:

- `write_cover_letter_artifacts` in
  `services/api/src/harrier/apply/letters.py` writes the markdown and HTML,
  renders to the final PDF path, then calls the PDF gate. On errors it raises
  `RuntimeError("invalid cover letter PDF: ...")` and leaves the PDF that
  failed, plus the new markdown and HTML, on disk.
- `run_tailor` in `services/api/src/harrier/resume/tailor.py` does the same
  with `"resume render validation failed: ..."`. The tracker row is correctly
  left unchanged (`test_failing_pdf_gate_leaves_tracker_row_unchanged`), but
  the PDF stays.

The Apply page (spec 047) does not know a run failed. `artifacts_for_job` and
`artifact_for_job` in `services/api/src/harrier/artifacts.py` report a kind as
present when `path.is_file()`, and the endpoint serves that file. So:

1. **A PDF that failed its gate is offered for download.** A two-page letter,
   or one with an unresolved template placeholder, is listed as
   `cover-letter-pdf` with `exists: true`. The run shows as failed; the
   artifact row beside it says the PDF is there.
2. **A render that raises leaves an earlier run's PDF.** If Playwright is
   missing or crashes, nothing is written to the PDF path, but the PDF from
   the last successful run is still there. It now sits beside a markdown
   draft it was not rendered from, and the page offers it as current.
3. **The PDF is visible before it is judged.** Between the render and the end
   of validation (which runs `pdfinfo` with a 10 second timeout), the final
   path holds an unvalidated file. A process killed in that window leaves it
   there permanently.

Spec 065 fixed this shape for one path only: a placeholder run now unlinks
the earlier HTML and PDF before raising `NeedsInputError`
(`test_a_placeholder_run_removes_the_pdf_and_html_of_an_earlier_run`). Every
other failure after the markdown write still has the problem. This spec
covers the rest.

## Scope

### The final PDF path only ever holds a PDF that passed the gate

Both writers render to a temporary file in the same directory as the final
PDF, validate the temporary file, and move it onto the final path only when
the gate returns no errors. The move is a rename within one directory, so it
is atomic: a reader sees either no PDF or a validated one, never a file
mid-render or mid-validation.

The temporary file's name starts with a dot and ends in `.tmp`, so it never
matches any path the `*_paths_for` helpers return, and the artifact endpoint
cannot serve it.

This is chosen over "render in place, unlink on failure" because unlinking
only fixes the failures the code sees. It does not fix problem 3: a process
killed during validation still leaves an unvalidated PDF at the final path.
Rendering to a temporary path fixes all three with one rule.

### An earlier run's PDF is removed before the new render starts

Once a run has written its new markdown, the earlier PDF no longer describes
it. The writer unlinks the final PDF path before rendering, not only on
failure, so a crash at any later point leaves no PDF rather than a stale one.
On success the validated temporary file takes its place.

### What a failed run leaves on disk

"Failed" here means any exception raised after the markdown is written:
the render raising (Playwright missing, Chromium crash), the gate returning
errors, or anything else, including an interrupt.

| File | Cover letter | Resume |
| --- | --- | --- |
| markdown | kept: the new draft | kept: the new draft |
| HTML | removed | removed |
| PDF at the final path | absent | absent |
| temporary PDF | removed | removed |
| `.metadata.json` sidecar | n/a | kept: it describes the kept markdown |
| `.evaluation.md` report | n/a | an earlier run's report is removed; none is written |

The markdown is kept because it is the operator's way to see what failed.
The commonest gate failure is page count, and the fix is to shorten the
draft. This matches spec 065, which keeps the draft and removes HTML and PDF
when a letter needs input.

The HTML is removed because it is only an intermediate for the PDF, and an
HTML file from this run beside no PDF, or from an earlier run beside a new
draft, describes nothing.

The resume evaluation report is in scope because it has the same shape: it
is written only after the gate passes, so a failed run currently leaves an
earlier run's report beside the new markdown, and the Apply page serves it
as `resume-evaluation`.

### CLI and run outcome

Unchanged in form:

- `harrier tailor` exits 1 and prints `tailor failed: resume render validation
  failed: ...` (or the render error) to stderr. No `tailored_pdf=` line.
- `harrier cover-letter` exits 1 and prints `cover letter failed: invalid
  cover letter PDF: ...` (or the render error) to stderr. No `pdf=` line.
- A run started from the Apply page ends in state `failed`, because the exit
  code is non-zero (`services/api/src/harrier_api/runs.py`).
- The tracker row does not change on a failed resume run (already true).

What changes is what the Apply page shows after the failure: `resume-pdf` or
`cover-letter-pdf` reports `exists: false`, so the page offers to run the
operation again instead of offering a file that failed. The markdown kind
reports `exists: true`.

The placeholder path from spec 065 keeps its exit code 3 and its output.

### Where the rule lives

One function in `services/api/src/harrier/resume/pdf.py` renders to a
temporary path, validates it, and moves it into place or cleans up. Both
writers call it. Two copies of the cleanup would drift, which is how this
gap appeared: spec 065 fixed one path and not the other.

## Failure modes

- **Render raises before writing anything.** The temporary file may not
  exist. Cleanup tolerates its absence. The final PDF is absent because it
  was unlinked before the render.
- **Gate fails.** The temporary file is removed. The final PDF is absent.
  The error message is unchanged.
- **Process killed during render or validation.** The final PDF is absent
  (unlinked before the render). A hidden `.tmp` file may remain. It is never
  served. See limitations.
- **Second run after a failure.** The first run left no PDF; the second run
  renders as normal. Nothing from the failed run blocks it.
- **Successful run over an earlier successful run.** The earlier PDF is
  unlinked, the new one is validated, then moved into place. The end state
  is the same as today.
- **The move itself fails** (permissions, disk full). The temporary file is
  removed and the error propagates. The final PDF is absent.
- **`pdfinfo` is not installed.** The gate already reports this as an error
  (`test_a_missing_pdfinfo_is_reported_rather_than_ignored`), so every render
  fails. Today an unvalidated PDF is still left on disk and offered. After
  this change no PDF is left. That is the invariant working, and it will be
  more visible than before.

## Acceptance criteria

The five letter, resume and Apply page tests below fail on the code before
this change. The four `test_pdf_gate.py` tests cannot run against it,
because the function they exercise is new.

| Criterion | Proof |
| --- | --- |
| a PDF that fails the gate is not at the final path afterwards | `services/api/tests/test_pdf_gate.py::test_a_pdf_that_fails_the_gate_is_never_moved_into_place` |
| the gate validates a temporary path, and the final path is absent while it runs | `services/api/tests/test_pdf_gate.py::test_the_final_path_is_empty_while_the_gate_runs` |
| a render that raises leaves no PDF and no temporary file | `services/api/tests/test_pdf_gate.py::test_a_render_that_raises_leaves_no_pdf_and_no_temporary_file` |
| a passing render ends at the final path and leaves no temporary file | `services/api/tests/test_pdf_gate.py::test_a_passing_render_is_moved_into_place` |
| a letter that fails the gate removes an earlier run's PDF and HTML and keeps the new markdown | `services/api/tests/test_apply.py::test_a_letter_that_fails_the_gate_removes_the_earlier_pdf_and_keeps_the_draft` |
| a letter whose render raises removes an earlier run's PDF | `services/api/tests/test_apply.py::test_a_letter_render_that_raises_removes_the_earlier_pdf` |
| a resume that fails the gate removes an earlier run's PDF, HTML and evaluation report, keeps markdown and sidecar, and leaves the tracker row unchanged | `services/api/tests/test_resume.py::test_a_resume_that_fails_the_gate_removes_the_earlier_pdf_html_and_evaluation` |
| a resume whose render raises removes an earlier run's PDF | `services/api/tests/test_resume.py::test_a_resume_render_that_raises_removes_the_earlier_pdf` |
| after a failed letter run the artifact list reports the PDF absent and the markdown present | `services/api/tests/test_ui_apply.py::test_a_failed_letter_run_reports_its_pdf_as_absent` |

The letter, resume and Apply page tests seed an earlier run's files at the
final paths before the run. The tests use synthetic companies and roles and
stub `render` and `validate`; none needs Playwright or `pdfinfo`.

- [x] every criterion above has its test, and each behavioural test fails
      on the code before this change
- [x] spec 065's placeholder test still passes unchanged
- [x] `just gate` passes

## Out of scope

- **Answers.** `harrier answers` writes markdown only; there is no PDF and no
  gate.
- **Making the gate stricter or looser.** What the gate checks is spec 013
  and spec 034. This spec changes only what happens to the file after the
  gate decides.
- **Keeping the last good PDF on failure.** An alternative is to write the
  markdown to a temporary path too and leave the whole earlier set untouched
  when a run fails. That loses the new draft, which is what the operator
  needs to fix a page-count failure, and it differs from spec 065. Not
  chosen; see open questions.
- **Sweeping leftover `.tmp` files.** See limitations.
- **Changing error messages or adding paths to them.** Resume paths contain
  the candidate's name, and run output is streamed to the page.
- **The Apply page UI.** It already renders `exists: false` as "run this".
  No frontend or contract change.

## Limitations

- A process killed between render and move leaves a hidden `.tmp` file in
  the resumes or cover letters directory. It is never served and never read.
  Nothing removes it. The cost is disk space only, and only after a crash.
- The rename is atomic only within one filesystem. The temporary file is in
  the same directory as the final file, so this holds for every path the
  writers use today.

## Open questions for review

1. **Keep the draft, or keep the last good set?** This spec keeps the new
   markdown and removes the earlier PDF. The alternative keeps the earlier
   markdown and PDF together and discards the new draft. Recommendation:
   keep the draft, for the reason above.
2. **Should the resume sidecar be removed too?** It is internal metadata,
   never served, and it describes the kept markdown, so this spec keeps it.

## Proof / origin

- Product invariant "Artifact gates" in `CLAUDE.md`.
- Spec 065, `test_a_placeholder_run_removes_the_pdf_and_html_of_an_earlier_run`:
  the same fix for the placeholder path, found in review of
  akin-oz/harrier#84.
- Spec 040 and `services/api/src/harrier/atomicio.py`: the write, then rename
  pattern this repository already uses for state files.
