# Scripted end-to-end check

With the server running (`./run.sh`), this exercises the full flow —
including resolving every escalation type and push/retry/rollback — purely
via the HTTP API. Useful as a regression check or as a basis for the demo
recording's narration.

By default this runs with AI-assisted suggestions **off** (no
`OPENAI_API_KEY`); the resolution logic below doesn't depend on them either
way — it resolves the rule-based options directly. If you do have
`OPENAI_API_KEY` set, `GET /api/escalations?status=pending` will also
include a populated `ai_suggestion` field per item you can inspect.

```bash
python3 - <<'PY'
import httpx
base = "http://127.0.0.1:8000"
c = httpx.Client(timeout=10)

c.post(base + "/api/reset")
run = c.post(base + f"{base}/api/run?use_sample=true".replace(base, "")).json()
print("run:", run)

pending = c.get(base + "/api/escalations?status=pending").json()["items"]
print(f"{len(pending)} escalations to resolve")

for e in pending:
    eid = e["id"]
    if e["kind"] == "mapping":
        c.post(f"{base}/api/escalations/{eid}/resolve", json={"action": "correct", "value": "email"})
    elif e["kind"] == "clean" and e["context"].get("field") == "__full_name__":
        raw = e["context"]["raw_value"]
        if raw == "Omar Al Farsi":
            c.post(f"{base}/api/escalations/{eid}/resolve",
                   json={"action": "correct", "value": {"first_name": "Omar", "last_name": "Al Farsi"}})
        elif raw == "Wei Zhang Li":
            c.post(f"{base}/api/escalations/{eid}/resolve",
                   json={"action": "correct", "value": {"first_name": "Wei", "last_name": "Zhang Li"}})
        else:
            c.post(f"{base}/api/escalations/{eid}/resolve", json={"action": "reject"})
    elif e["kind"] == "clean" and "email" in e["title"]:
        c.post(f"{base}/api/escalations/{eid}/resolve", json={"action": "correct", "value": "david.chen@acme.com"})
    elif e["kind"] == "validation" and "hire date" in e["title"]:
        c.post(f"{base}/api/escalations/{eid}/resolve", json={"action": "correct", "value": "2020-01-01"})
    elif e["kind"] == "merge_conflict":
        c.post(f"{base}/api/escalations/{eid}/resolve", json={"action": "approve"})

print("dataset summary:", c.get(base + "/api/dataset").json()["summary"])
push = c.post(base + "/api/push").json()
print("push:", push["succeeded"], "ok /", len(push["results"]))
print("rollback:", c.post(base + "/api/push/rollback").json())
PY
```

Expected shape of the result (exact counts may shift a hair with the mock
API's randomized failures, but the pattern should hold):

- 9 real escalations raised out of 19 raw source rows / 14 candidate
  employee records — i.e. most of the migration is handled autonomously.
- After resolving the queue: 10 records ready, 1 rejected (the record with
  a genuinely blank name), 0 pending escalations.
- Push succeeds for all ready records (with internal per-record retries on
  the mock API's simulated flakiness); rollback removes everything the
  batch wrote.
