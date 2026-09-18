# Client Data Migration & Integration Agent

An agent that ingests messy multi-source HR/CRM exports, works out the mapping
to a target schema, cleans and deduplicates the data, and pushes it to a mock
target API — pausing only for a human when it hits something genuinely
ambiguous. Wrapped in a single-page console for a non-technical implementation
consultant to supervise the run.

## Tech stack

- **Backend**: Python, FastAPI, pandas, `python-dateutil`, `rapidfuzz` (fuzzy
  string matching for column-name mapping). The core pipeline —
  mapping/cleaning/reconciliation/validation/escalation — is deterministic
  and rule-based, not an LLM call, which matters for a customer-facing
  migration tool: it's inspectable, unit-testable, and its behavior doesn't
  drift between runs (see write-up for why this is the autonomy boundary).
- **Optional LLM assist**: `openai` Python SDK calling **gpt-4o-mini**.
  When `OPENAI_API_KEY` is set, every escalation additionally gets an
  advisory suggestion + one-line rationale + confidence, shown right on the
  card with a "Use this suggestion" button. It's purely advisory — never
  auto-applied, the human still clicks approve/correct/reject — and if the
  key is unset or the call fails/times out, the app runs exactly as it does
  without any LLM in the loop (see `backend/llm.py`).
- **Environment & dependencies**: managed with [`uv`](https://docs.astral.sh/uv/)
  — `pyproject.toml` + `uv.lock` at the repo root, `uv sync` to create the
  venv, `uv run` to execute. No manual `pip`/`venv` steps needed.
- **Frontend**: a single static HTML file, vanilla JS, no build step. Polls
  the backend every ~900ms for a live trace, escalation queue, dataset
  preview, push results and audit trail.
- **"Target platform"**: an in-memory mock API (`mock_api.py`) with a
  simulated ~15% per-record failure rate, so push/retry/rollback have
  something real to exercise.

## Project layout

```
pyproject.toml   uv-managed dependencies
uv.lock          locked, reproducible dependency versions
.env.example     copy to .env and set OPENAI_API_KEY to enable AI assist
backend/
  main.py       FastAPI app / HTTP endpoints
  agent.py      the actual agent: ingest -> map -> reshape -> clean ->
                reconcile -> validate, plus escalation resolution
  schema.py     target schema for the 'employees' entity
  mock_api.py   stub target-system API
  llm.py        optional gpt-4o-mini escalation-suggestion helper
data/
  hris_export.csv   sample legacy HRIS export
  crm_export.csv    sample CRM export (different columns, overlapping people)
frontend/
  index.html    the consultant-facing console (single page, no build step)
run.sh          uv-based convenience launcher
```

## Running it locally

Requires Python 3.10+. [`uv`](https://docs.astral.sh/uv/) is used for
environment/dependency management — `run.sh` will install it via `pip` if
it's not already on your machine, or install it yourself first:
`curl -LsSf https://astral.sh/uv/install.sh | sh`.

```bash
git clone <this repo>
cd hr_migration_agent
cp .env.example .env     # optional: add OPENAI_API_KEY to enable AI-assisted suggestions
./run.sh
```

`run.sh` runs `uv sync` (creates `.venv/` from `pyproject.toml` + `uv.lock`,
fast and reproducible) and then `uv run uvicorn main:app`. Open
**http://localhost:8000**.

Equivalent by hand:
```bash
uv sync
cd backend && uv run --project .. uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Enabling AI-assisted suggestions (optional)

Without `OPENAI_API_KEY` set, the app is fully functional — every escalation
still has a rule-based set of options a human picks from. With it set (in
`.env`, or exported in your shell), each escalation additionally gets a
gpt-4o-mini-generated suggestion + rationale + confidence level, visible as
an "🤖 AI suggests ..." box with a one-click "Use this suggestion" button.
The header shows "AI assist on/off" so it's obvious which mode you're in.

## Using the console

1. **"Run agent on sample data"** — runs the pipeline against the two bundled
   sample exports (`data/hris_export.csv`, `data/crm_export.csv`). Watch the
   live trace panel; it logs every mapping decision, cleanup, merge, and
   escalation as it happens.
2. **Escalation queue** — each card shows exactly what's ambiguous and why,
   with the raw context (source file, row, candidate values), and — if
   `OPENAI_API_KEY` is set — an AI-suggested resolution with a rationale and
   confidence level. For each you can:
   - click the suggested option button (**approve** the agent's top
     rule-based guess / first candidate),
   - click **Use this suggestion** to accept the AI's proposed value,
   - type a correction and hit **Correct**,
   - or **Reject** to drop that record from the migration (it's not
     force-fit into the target).
3. **Dataset preview** shows records that are ready to push vs. still blocked
   on an open escalation, live as you resolve the queue.
4. **Push to target** sends all "ready" records to the mock API. Each
   record already gets up to 3 internal retry attempts on transient
   failure; **Retry failed** re-attempts anything still failing after that,
   and **Rollback last batch** deletes everything the last batch wrote to
   the mock target (simulating a transactional undo).
5. **Audit trail** is the full "what changed and why" log: every auto-merge,
   every human resolution (with before/after values), every push/retry/
   rollback — attributable back to a specific record.

There's also a raw HTTP API (see `backend/main.py`) if you want to drive it
from a script instead of the UI — useful for the demo recording or for
automated testing (see `tests/smoke_test.md` for a scripted example).

## Sample data quirks (on purpose)

The two bundled CSVs encode the specific problems the exercise asks for:
different column names per system, mixed date formats (`03/14/2019` vs
`2020-07-01` vs `14-01-2018` vs `N/A`), whitespace/casing noise, an
exact-duplicate row, records split across both files that need merging,
a genuinely ambiguous column name (`Contact`, which could mean the
employee's own email or their manager's), full names that can't be safely
split into first/last for 3-token names, a malformed email with a doubled
`@` (safely auto-fixable) vs. one missing a TLD (not safely fixable), and a
real data conflict (two sources disagree on someone's job title).
