---
description: The contract seams and the files that require a paused, human-reviewed edit
---

Three seams are the source of truth. Types and behavior derive from them; an invented
field must become a compile error, never a hand-edit.

1. **API contract**: `packages/contract/openapi.json` is exported from FastAPI;
   `packages/contract/src/schema.d.ts` is generated from it. Both are committed and
   never hand-edited. The web app speaks only in generated types (ADR-005).
2. **Tracker schema**: one definition with migrations in
   `services/api/src/harrier/tracker/`. All mutation goes through its write path;
   nothing else opens the database for writing (ADR-003).
3. **Governance sources**: `.ai/` compiles to `CLAUDE.md`, `AGENTS.md`, `.claude/`
   agents and commands. Generated output is never hand-edited.

A pre-edit guard (`.claude/hooks/guard-source-of-truth.sh`) pauses for human review on:
the contract package, tracker schema and migrations, the classification config
(`config/data-classification.json`, `.gitignore`), CI workflows, and the
governance layer itself. Approve such an edit only when an
approved spec covers it.
