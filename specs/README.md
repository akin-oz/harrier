# Specs

Spec-gated execution: nothing changes observable behavior without an approved spec.

## Format

`NNN-kebab-slug.md`, zero-padded, monotonically increasing. Frontmatter:

```yaml
spec: NNN
title: short imperative title
status: proposed | in-progress | shipped
approved: no
milestone: M0..M5
depends: [NNN, ...]
```

Body sections: Problem, Scope, Acceptance criteria (checkboxes), Proof / origin
(the old-repo file or ADR that defines correct behavior), Out of scope.

## Lifecycle

1. Propose: `/spec` or spec-author writes the file with `approved: no`. Nothing is built.
2. Approve: a human flips `approved: yes`. This is the only step the agent never performs.
3. Implement: strictly within the approved scope; gaps become spec amendments, stated explicitly.
4. Commit: every commit carries `Spec: NNN` (enforced by `.claude/hooks/guard-commit.sh`
   and the lefthook commit-msg check); CI resolves each trailer to an approved spec.

## Milestones

- **M0** governance, scaffold, CI: 001, 002, 003
- **M1** walking skeleton across every risky seam: 004, 005, 006
- **M2** discovery pipeline complete: 007, 008, 009, 010, 011
- **M3** artifact generation: 012, 013, 014, 015
- **M4** outreach, Gmail, digest: 016, 017, 018, 019
- **M5** scheduling, demo, parity, cutover: 020, 021, 022, 024

Stubs sequence the backlog; they do not define final scope. Refine a stub into a
real spec before asking for approval, and expect the scope to narrow or split at
that point. Awaiting approval today: 024. Everything else is approved and
either shipped or in progress; `ls specs/` and the frontmatter are the record,
not this paragraph.
- **M5 (added)** user configuration in the database: 023 (ADR-009; approved separately)

M5 status: 020, 021, 022, 023 and 024 all shipped. Every spec in the backlog
is delivered.

That does not mean the cutover happened. Spec 024 delivered the tooling for it;
the event itself is an operational step whose criteria are listed separately in
that spec and are Akin's to satisfy. `harrier cutover preflight` reports where
it stands.

Spec 022 was split while being drafted: verification (022) is read-only and
repeatable, cutover (024) is one irreversible sitting. They need different gates.
