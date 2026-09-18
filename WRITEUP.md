# Write-up

## Approach

The agent runs a deterministic pipeline — ingest → propose column mapping →
reshape (split combined columns) → clean (whitespace/case/dates/emails) →
reconcile (merge duplicates across sources) → validate — and logs every
decision as a structured event the UI streams live. Each stage can raise
**escalations**: small, self-contained objects with a kind, a plain-English
reason, the exact context needed to decide (raw values, candidate options,
source file/row), and a resolve endpoint. A human resolves one with
*approve* (take the agent's top suggestion), *correct* (supply the right
value), or *reject* (drop the record) — the pipeline doesn't re-run from
scratch; the resolution patches the affected record(s) directly and the
dataset view updates immediately. Everything not escalated is applied
automatically and written to an audit trail with before/after values, so
"autonomous" never means "invisible."

I used a deterministic, rule-and-fuzzy-match engine for the core pipeline
rather than routing every field through an LLM call. For a customer-facing
migration tool, I'd rather the mapping/cleaning logic be inspectable and
unit-testable than probabilistic end-to-end. The one place an LLM (gpt-4o-mini)
*is* wired in is deliberately narrow and additive: when an escalation is
raised, the agent optionally asks gpt-4o-mini for a suggested resolution and
a one-line rationale, shown on the card with a confidence level and a
"use this suggestion" button. It never resolves anything by itself — it's
strictly advisory, degrades silently to "no suggestion" if no API key is
set or the call fails, and the human still has to click approve/correct/
reject either way. That split — deterministic core, LLM only to speed up
the human's decision at the edges — is the same boundary described below,
just applied one level up: the LLM doesn't get more autonomy than the rule
engine does.

## Where I drew the autonomy line, and why

The test I applied at every decision point: **would acting on this without
asking require the agent to guess on the client's behalf, and would a wrong
guess be expensive or hard to notice later?** If no, it acts. If yes, it stops.

Concretely, the agent acts alone on:
- **Column mapping** when exactly one target field is a clearly best match
  (score above threshold *and* meaningfully ahead of the next candidate) —
  even if the score isn't a perfect 100. `DOH` → `hire_date` doesn't need a
  human's blessing.
- **Formatting fixes** with one unambiguous correct output: trimming
  whitespace, normalizing casing, parsing `03/14/2019` / `2020-07-01` /
  `14-01-2018` into ISO dates, collapsing an accidental double `@` in an
  email. There's no second plausible interpretation.
- **Merging duplicate/complementary records** when sources either agree or
  fill in each other's gaps (one file has the last name, the other doesn't) —
  auto-merged and logged, no conflict to arbitrate.

The agent escalates instead of guessing when:
- **A column name genuinely fits two target fields** (`Contact` scores
  similarly against `email` and `manager_email`) — auto-mapping one is a
  coin flip that silently corrupts a field.
- **A value can't be confidently cleaned**, e.g. a name with 3+ tokens
  (which part is the surname?), or an email missing a TLD (which domain was
  intended?) — versus the double-`@` case above, which *is* confidently
  fixable, so it isn't escalated. The line is "is there one clearly correct
  interpretation," not "is this value imperfect."
- **A record fails validation twice** — direct date parse, then a fuzzy
  fallback parse, both fail — rather than escalating on the first miss,
  which would flood the queue with things a slightly smarter parse handles.
- **Two sources genuinely disagree** on a field value (not a
  formatting difference — `"Software Engineer"` vs `"Senior Software
  Engineer"` for the same employee ID) — this is a real data conflict a
  client needs to arbitrate, not something to silently pick a winner on.

In the bundled sample data (19 raw rows across 2 files → 14 candidate
employee records), this boundary produces **9 escalations** — one for the
genuinely ambiguous column, three for un-splittable names, two for
unparseable dates, one for an unfixable email, and two for real field
conflicts — while 10 of 14 records reach "ready to push" with zero human
input. That ratio is the thing I'd defend in review: enough escalations to
prove the agent isn't rubber-stamping bad data, few enough that a consultant
isn't triaging noise.

## What I'd build next

- **Learn from corrections.** Every human resolution is already captured
  with before/after values; the natural next step is feeding that history
  back into the mapping/cleaning confidence scores (and into the LLM
  prompt as few-shot examples) so the same client's future files need
  fewer escalations over time.
- **Async/streamed LLM calls.** Suggestions are currently fetched
  synchronously when an escalation is raised, which adds latency to
  `/api/run` when AI assist is enabled. Fetching them lazily per-card (or
  in parallel) would keep the pipeline itself instant regardless of LLM
  latency.
- **LLM for genuinely unstructured fields** — free-text notes columns,
  inconsistent department names that need semantic (not just fuzzy-string)
  matching to the target taxonomy — scoped the same way as the escalation
  suggestions: proposal, not autonomous action.
- **Field-level rollback**, not just batch rollback — today rollback undoes
  an entire push; a targeted "revert this one record" would be safer once
  volumes grow.
- **Schema drift detection** across migration runs (the client re-exports
  next week with a renamed column) and a persisted mapping "memory" per
  client so the agent doesn't re-litigate settled decisions.
- **Real auth + multi-user review** (right now it's a single in-memory
  session) and a proper job queue instead of running the pipeline
  synchronously in the request thread.
