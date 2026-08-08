---
description: Zero-tolerance verification discipline
---

Never claim work is complete without executing the checks and seeing them pass.

- After any Python change: `uv run ruff check` and `uv run pyright` on the service.
- After any TypeScript change: `pnpm type-check` and `pnpm lint` in the workspace.
- After contract-affecting changes: regenerate the contract and confirm no unexpected
  diff (`just contract`).
- The turn-end gate (`.claude/hooks/verify-on-stop.sh` running `just gate`) fails the
  turn while checks are red. Do not work around it; fix the failure.

Forbidden vocabulary: "this should work now", "untested but looks correct",
"I have implemented X, please run the tests to check". Run them yourself and report
the actual output.
