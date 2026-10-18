---
spec: 159
title: A cover letter never calls a legal entity "my" or "our"
status: proposed
approved: no
milestone: M9
depends: [158]
---

# Spec 159: A cover letter never calls a legal entity "my" or "our"

## Problem

Spec 158's rule L10 refuses "my" before a legal entity type unless an
evidence line shows the entity is registered and the candidate's own
(`lint_letter`, `_owned` and `_registered` in
`services/api/src/harrier/apply/letterlint.py`). The operator's rule is
simpler: write "an Estonian OÜ", not "my OÜ", unless the profile says the
entity is registered. Their profile says it is not formed yet.

L10 fails that rule on both sides. Each case below was run against
`lint_letter` at `b1250ad` with the spec 158 test fixtures.

**The letter side matches the literal "my GmbH" only.** With no
registration in the evidence, each of these passes L10:

- "my own GmbH" and "my Exampleland GmbH": a word between.
- "my" and "GmbH" split by a line break or by two spaces.
- "our GmbH".
- "my sp. z o. o." and "my B.V.": a spelling the type list does not hold.

**The evidence side guesses ownership from free text.** With "my GmbH" in
the letter, each of these evidence lines lets it through:

- "My partner owns a GmbH registered in Exampleland."
- "My boss's GmbH is registered in Exampleland."
- "The founder of Acme GmbH, registered in Germany, hired me."
- "My GmbH will be registered in Exampleland."
- "My GmbH was registered and deregistered."

The employer filter matches "client" and "works at" but not "clients" or
"working at". And it refuses a true line: "Owner of Example GmbH,
registered in Exampleland." does not let "my GmbH" through.

Three rounds of review on PR #405 each patched one phrasing and the next
found another. Free text cannot say reliably whose entity is registered.
A letter that calls an unformed entity "my OÜ" is a false claim the
operator would send under their own name.

## Scope

- `services/api/src/harrier/apply/letterlint.py`: rule L10 only. The
  ownership and registration heuristic (`_owned`, `_registered`,
  `_OWNERSHIP_CUE`, `_SOMEONE_ELSES`, `_NEGATED`) is removed. L10 no longer
  reads the evidence.
- `services/api/src/harrier/voices.py`: the entity line in
  `INDUSTRY_LETTER_VOICE` and `FOCUSED_LETTER_VOICE`.
- `services/api/tests/test_letter_lint.py`: the L10 tests.
- `services/api/tests/test_academic_documents.py`: the industry letter
  prompt's pin in `INDUSTRY_PROMPT_SHA256` changes on purpose. The shared
  letter block's pin does not change.
- `specs/158-a-cover-letter-leads-with-a-result-and-closes-with-an-offer.md`:
  a pointer to this spec on L10, the prompt rule and amendment 16. No
  other text in it changes.

## Behavior

### L10 after this change

L10 refuses an **owner word**, then **at most two words**, then a **legal
entity type**, case-insensitive, in `full_version` and in
`short_version`. Each version is checked on its own.

- **Owner word:** "my" or "our", as a whole word.
- **Words between:** zero, one or two words. A word is a letter or digit
  followed by letters, digits, apostrophes or hyphens: "own",
  "Exampleland", "boss's", "Exampleland-based". A sentence end
  (".", "!", "?") ends the match, so "my job. GmbH" is not a match.
- **Space:** any run of whitespace, line breaks included, between the
  owner word, each word and the type.
- **Entity type:** OÜ, GmbH, Ltd, LLC, BV, SAS, SRL and sp. z o.o.
  Within a type, each letter group may be followed by a dot and by
  whitespace, so "B.V.", "L.L.C.", "Ltd.", "sp. z o. o." and "sp z oo"
  match. The type must not be followed by a letter or digit, so "my
  Ltda" and "my BVB" do not match.

Each match adds one violation:

```
calls a legal entity '<match>': write "a" or "an" before the entity type
```

`<match>` is the matched text with each whitespace run collapsed to one
space, for example `'my own GmbH'`. The same text twice gives one
message.

The evidence is not read. A letter that writes "my GmbH" is refused
whatever the evidence says, including when the evidence states the
candidate's own entity is registered.

These pass L10: "an Estonian OÜ", "a GmbH", "through a GmbH", "Example
GmbH" with no owner word before it, and "my work at Example GmbH" (three
words between).

L10 still reads both versions, as spec 158 amendment 13 says. Its place in
`cover_letter_violations` and the retry spec 085 gives a refusal do not
change.

### The prompt

The industry and focused letter voices replace the line

> Call a legal entity "a" or "an" entity of its type, never "my", unless
> the evidence states it is registered

with

> Call a legal entity "a" or "an" entity of its type, such as "an
> Estonian OÜ" or "a GmbH". Never write "my" or "our" before it, even when
> the evidence states it is registered

The example names a type, not a fact about the candidate. The shared
letter block does not change, so the academic prompt does not change.

## Failure modes

- **The candidate's entity really is registered.** The letter still may
  not write "my GmbH". It writes "a GmbH" or the entity's name. Allowing
  "my" for a registered entity needs a structured field, not free text
  (Out of scope).
- **A legitimate phrase within two words of a type.** "my employer
  Example GmbH" and "my experience with SAS" are refused. The model gets
  the one retry and rewrites, for example "at Example GmbH, my employer".
  If the retry is refused too, the command exits 1 naming L10, as for any
  refusal (spec 085).
- **Ownership stated without an owner word.** "an OÜ I own", "the GmbH is
  mine" and "my newly registered Estonian OÜ" (three words between) pass
  L10. The prompt line forbids the first two in spirit; nothing in code
  catches them.
- **A type outside the list.** "my S.A.", "my AB" and "my Inc" pass, as
  today. The list is spec 158's.
- **OÜ written without the umlaut.** "my OU" passes. See open decision 2.
- **A violation spans the two versions.** It cannot: each version is
  checked alone.

## Acceptance criteria

- [ ] With no entity in the evidence, each of "my own GmbH", "my
  Exampleland GmbH", "my" and "GmbH" split by a line break, "my  GmbH"
  with two spaces, "our GmbH", "my sp. z o. o." and "my B.V." is refused
  by L10 in the full version, and "our GmbH" in the short version
  (planned test_an_owner_word_before_an_entity_type_is_refused).
- [ ] "my GmbH" is refused with each of these evidence lines: "My partner
  owns a GmbH registered in Exampleland.", "My boss's GmbH is registered
  in Exampleland.", "The founder of Acme GmbH, registered in Germany,
  hired me.", "My GmbH will be registered in Exampleland.", "My GmbH was
  registered and deregistered.", "Owner of Example GmbH, registered in
  Exampleland." and "My GmbH is registered in Exampleland."
  (planned test_the_evidence_never_permits_my_entity).
- [ ] "an Estonian OÜ", "a GmbH", "Example GmbH", "my work at Example
  GmbH", "my Ltda" and "That was my job. GmbH registration took a week."
  pass L10 (planned test_an_article_or_a_name_passes).
- [ ] The message for "my\nown  GmbH" is `calls a legal entity 'my own
  GmbH': write "a" or "an" before the entity type`
  (planned test_an_owner_word_before_an_entity_type_is_refused).
- [ ] `test_an_unregistered_entity_is_not_mine`, which spec 158 cites,
  keeps its name and checks that "my GmbH" is refused whether or not the
  evidence states a registered GmbH, and that "a GmbH" passes.
- [ ] Each test above fails against `letterlint.py` at `b1250ad`.
- [ ] The industry and focused letter prompts carry the new entity line;
  the industry letter pin in `INDUSTRY_PROMPT_SHA256` changes and the
  shared letter block's pin does not
  (`services/api/tests/test_academic_documents.py::test_both_kinds_share_every_truth_rule`).
- [ ] `letterlint.py` no longer defines `_owned`, `_registered`,
  `_OWNERSHIP_CUE`, `_SOMEONE_ELSES` or `_NEGATED`. Checked by reading the
  diff.

## Proof / origin

- The operator's rule: "an Estonian OÜ", not "my OÜ", unless the profile
  says it is registered. Their profile says it is not formed. The profile
  is personal data and is not quoted here.
- The holes: an honesty review of PR #405 after it merged (`58f7cb7`),
  reproduced against `lint_letter` at `b1250ad` for this spec.
- The current rule: spec 158, L10 and amendments 12 and 16;
  `services/api/src/harrier/apply/letterlint.py`.

## Open decisions for Akin

1. **Two words between, or more.** Two catches "my own Estonian OÜ" and
   refuses "my employer Example GmbH". Three would also catch "my newly
   registered Estonian OÜ" and would refuse "my work at Example GmbH". The
   spec says two, as the request did.
2. **"OU" for OÜ.** A model copying evidence writes "OÜ". Adding "OU"
   catches an ASCII spelling and risks refusing an unrelated "my OU".
   The spec leaves it out.
3. **"SAS" is also a software product.** "my SAS models" is refused. The
   spec keeps SAS, since it is in spec 158's list. Dropping it would let
   "my SAS" through for the French company form.

## Out of scope

- A structured "registered entity" field in the candidate document that
  would let a letter say "my OÜ" when the operator has recorded one. A
  later spec can add it; L10 would then permit the owner word for that
  field's type only. Nothing here reads `candidate.eu_entity_status`
  differently from spec 158.
- Ownership stated without "my" or "our" ("an OÜ I own").
- New entity types, and the answers generator (`harrier.apply.answers`).
- L1 to L9 and L11, and the evidence spec 158 lets a letter cite.

## Honest limitations

- L10 is a pattern over words, so a false refusal costs a retry and the
  phrases under Failure modes slip through. The prompt line is what keeps
  most letters from writing "my" at all.
- No test can prove the model follows the new prompt line. The linter is
  the check.

## Migration

None. Letters already on disk are not re-linted. The next `cover-letter`
run on an industry or focused track uses the strict rule.
