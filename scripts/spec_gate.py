#!/usr/bin/env python3
"""Resolve every commit's `Spec: NNN` trailer against the base branch (spec 039).

The gate used to read the spec file from the checked-out pull request, which
is the branch asking for approval. So a pull request could add
`approved: yes` in one commit and implement the spec in the next, and pass.
The most recent merges show that shape: the approval commit is an ancestor of
the implementing commit inside the same pull request.

This repository has one author who is also its approver, so the gate never
had a disagreement to detect, which is exactly why nobody noticed it could
not detect one.

The fix is not a second reviewer, which would be theatre here. It is that
approval must already exist on the base branch when the work is proposed.
Self-approval stays possible and cheap; it stops being something a proposal
can grant itself.

One exemption, and it is what makes the rule usable: a commit that touches
only `specs/` is a governance commit, and it is allowed to reference a spec
that is not yet approved on the base. That is how an approval reaches the
base in the first place. A commit that touches a spec and any other file is
not a governance commit, so approving and implementing in one commit is
refused rather than waved through.

Run by `.github/workflows/spec-gate.yml`. Proved by
`services/api/tests/test_spec_gate.py`, which builds real repositories and
runs this against them, because a gate tested against a mock of the thing it
gates proves nothing about the gate.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

TRAILER = re.compile(r"Spec:[ \t]*([0-9]{3})", re.IGNORECASE)
APPROVED = re.compile(r"^approved:[ \t]*yes[ \t]*$", re.IGNORECASE | re.MULTILINE)


class GateError(RuntimeError):
    """The gate could not be run. Distinct from a commit failing it."""


@dataclass(frozen=True)
class Verdict:
    commit: str
    subject: str
    ok: bool
    detail: str


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GateError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def spec_number(message: str) -> str | None:
    match = TRAILER.search(message)
    return match.group(1) if match else None


def touches_only_specs(repo: str, commit: str) -> bool:
    """Whether a commit changes nothing outside `specs/`.

    The exemption is deliberately narrow. A commit that edits a spec and a
    source file is proposing work, whatever its message says, so it does not
    get to grant itself the approval it is proposing under.
    """
    changed = _git(repo, "show", "--pretty=", "--name-only", commit).split()
    return bool(changed) and all(path.startswith("specs/") for path in changed)


def approved_on(repo: str, ref: str, number: str) -> bool:
    """Whether spec NNN reads `approved: yes` in `ref`'s tree.

    Read through `git show` rather than from the working tree: the working
    tree is the pull request, and the pull request is what we are checking.
    """
    listing = _git(repo, "ls-tree", "-r", "--name-only", ref, "specs/").splitlines()
    matches = [path for path in listing if re.match(rf"specs/{number}-.*\.md$", path)]
    if not matches:
        return False
    return bool(APPROVED.search(_git(repo, "show", f"{ref}:{matches[0]}")))


def check(repo: str, base: str, head: str) -> list[Verdict]:
    """One verdict per non-merge commit between base and head."""
    commits = _git(repo, "rev-list", "--no-merges", f"{base}..{head}").split()
    verdicts: list[Verdict] = []
    for commit in commits:
        subject = _git(repo, "log", "-1", "--format=%s", commit).strip()
        number = spec_number(_git(repo, "log", "-1", "--format=%B", commit))
        if number is None:
            verdicts.append(Verdict(commit, subject, False, "no 'Spec: NNN' trailer"))
            continue
        if approved_on(repo, base, number):
            verdicts.append(
                Verdict(commit, subject, True, f"spec {number} approved on the base")
            )
            continue
        if touches_only_specs(repo, commit):
            verdicts.append(
                Verdict(
                    commit,
                    subject,
                    True,
                    f"governance commit for spec {number}, specs/ only",
                )
            )
            continue
        verdicts.append(
            Verdict(
                commit,
                subject,
                False,
                f"spec {number} is not 'approved: yes' on the base branch. "
                f"Approval has to land on the base before the work is proposed: "
                f"open a separate pull request that changes only specs/{number}-*.md, "
                f"or push the approval to the base directly. A pull request cannot "
                f"approve the spec it is implementing.",
            )
        )
    return verdicts


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: spec_gate.py <repo> <base-sha> <head-sha>", file=sys.stderr)
        return 2
    repo, base, head = argv
    try:
        verdicts = check(repo, base, head)
    except GateError as error:
        print(f"::error::the spec gate could not run: {error}", file=sys.stderr)
        return 2
    if not verdicts:
        print("No commits to check.")
        return 0
    failed = 0
    for verdict in verdicts:
        if verdict.ok:
            print(f'ok: {verdict.commit[:9]} "{verdict.subject}" -> {verdict.detail}')
        else:
            failed += 1
            print(
                f'::error::Commit {verdict.commit[:9]} ("{verdict.subject}"): {verdict.detail}'
            )
    if failed:
        print()
        print("Every commit must reference a spec approved on the base branch.")
        print("See specs/README.md.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
