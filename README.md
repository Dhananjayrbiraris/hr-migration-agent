# Client Data Migration & Integration Agent

An agent that ingests messy multi-source HR/CRM exports, works out the mapping
to a target schema, cleans and deduplicates the data, and pushes it to a mock
target API — pausing only for a human when it hits something genuinely
ambiguous. Wrapped in a single-page console for a non-technical implementation
consultant to supervise the run.

## Tech stack

- **Backend**: Python, FastAPI, pandas, `python-dateutil`, `rapidfuzz` (fuzzy
  string matching for column-name mapping). No LLM call in the hot path —
  the mapping/cleaning/escalation logic is deterministic and explainable,
  which matters for a customer-facing migration tool (see write-up). An LLM
  (e.g. via the Claude/OpenAI API) is a natural next step for phrasing
  escalation summaries or handling free-text fields; it wasn't required to
  meet the acceptance criteria for this exercise so it isn't wired in, to
  keep the decision logic auditable and testable.
- **Frontend**: a single static HTML file, vanilla JS, no build step. Polls
  the backend every ~900ms for a live trace, escalation queue, dataset
  preview, push results and audit trail.
- **"Target platform"**: an in-memory mock API (`mock_api.py`) with a
  simulated ~15% per-record failure rate, so push/retry/rollback have
  something real to exercise.

## Project layout

```
backend/
  main.py       FastAPI app / HTTP endpoints
  agent.py      the actual agent: ingest -> map -> reshape -> clean ->
                reconcile -> validate, plus escalation resolution
  schema.py     target schema for the 'employees' entity
  mock_api.py   stub target-system API
data/
  hris_export.csv   sample legacy HRIS export
  crm_export.csv    sample CRM export (different columns, overlapping people)
frontend/
  index.html    the consultant-facing console (single page, no build step)
run.sh          convenience launcher
```

## Running it locally

Requires Python 3.10+.

```bash
git clone <this repo>
cd hr_migration_agent
./run.sh
```

Then open **http://localhost:8000**.

(If you'd rather do it by hand: `cd backend && pip install -r
requirements.txt && uvicorn main:app --reload`, then open the same URL —
`main.py` serves the frontend directly, no separate frontend server needed.)

## Using the console

1. **"Run agent on sample data"** — runs the pipeline against the two bundled
   sample exports (`data/hris_export.csv`, `data/crm_export.csv`). Watch the
   live trace panel; it logs every mapping decision, cleanup, merge, and
   escalation as it happens.
2. **Escalation queue** — each card shows exactly what's ambiguous and why,
   with the raw context (source file, row, candidate values). For each you
   can:
   - click the suggested option button (**approve** the agent's top guess /
     first candidate),
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
