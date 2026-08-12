---
description: Writing style for docs, specs, comments, and commit messages
---

- No em dashes and no double dashes as punctuation, anywhere: docs, specs, comments,
  commit messages. Use colons, parentheses, or a new sentence. CLI flags like
  `--dry-run` are syntax, not punctuation, and are fine. So is a section divider
  comment, `# --- name ---`, which is a rule drawn across a file rather than a mark
  inside a sentence; the codebase uses it throughout and a review read it as a
  breach of this line, so it is named here rather than argued again.
- Short sentences. Plain claims. No marketing language.
- Every doc that claims a behavior names the file or test that proves it.
- Honest limitations sections wherever relevant. No invented numbers anywhere.
