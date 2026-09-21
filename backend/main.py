import os
import shutil
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from . import agent, mock_api
except ImportError:
    import agent
    import mock_api

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Client Data Migration Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATE = agent.AgentState()
LAST_BATCH_ID: str | None = None
PUSH_RESULTS: list[dict[str, Any]] = []


def _record_public(r: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in r.items() if not k.startswith("__")}


@app.post("/api/reset")
def reset():
    global STATE, PUSH_RESULTS, LAST_BATCH_ID
    STATE = agent.AgentState()
    PUSH_RESULTS = []
    LAST_BATCH_ID = None
    mock_api.reset()
    return {"ok": True}


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    saved = {}
    for f in files:
        dest = os.path.join(UPLOAD_DIR, f.filename)
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved[f.filename] = dest
    return {"ok": True, "files": list(saved.keys())}


@app.post("/api/run")
def run(use_sample: bool = False):
    global STATE
    STATE = agent.AgentState()
    uploaded = [f for f in os.listdir(UPLOAD_DIR) if f.endswith(".csv")]
    if use_sample or not uploaded:
        if os.path.exists(DATA_DIR):
            files = {name: os.path.join(DATA_DIR, name) for name in os.listdir(DATA_DIR) if name.endswith(".csv")}
        else:
            files = {}
    else:
        files = {name: os.path.join(UPLOAD_DIR, name) for name in uploaded}

    if not files:
        return {"ok": False, "error": "No CSV files found to process. Please upload CSV files."}
    agent.run_pipeline(STATE, files)
    return {"ok": True, "stage": STATE.stage, "escalation_count": len(STATE.escalations)}


@app.get("/api/log")
def get_log(since: int = 0):
    return {"events": STATE.events[since:], "total": len(STATE.events), "stage": STATE.stage}


@app.get("/api/escalations")
def get_escalations(status: str = "pending"):
    items = [asdict(e) for e in STATE.escalations.values() if status == "all" or e.status == status]
    return {"items": items, "count": len(items)}


class ResolveBody(BaseModel):
    action: str            # approve | correct | reject
    value: Any = None


@app.post("/api/escalations/{eid}/resolve")
def resolve(eid: str, body: ResolveBody):
    result = agent.resolve_escalation(STATE, eid, body.action, body.value)
    return result


@app.get("/api/dataset")
def get_dataset():
    ready = [_record_public(r) for r in STATE.final_records.values() if r.get("__ready__")]
    pending = [_record_public(r) for r in STATE.final_records.values() if not r.get("__ready__")]
    return {
        "ready": ready,
        "pending_escalation": pending,
        "rejected": [_record_public(r) for r in STATE.rejected_records],
        "summary": {
            "total_candidates": len(STATE.final_records) + len(STATE.rejected_records),
            "ready_to_push": len(ready),
            "blocked_on_escalation": len(pending),
            "rejected": len(STATE.rejected_records),
            "open_escalations": sum(1 for e in STATE.escalations.values() if e.status == "pending"),
        },
    }


@app.post("/api/push")
def push():
    global LAST_BATCH_ID, PUSH_RESULTS
    batch_id = f"batch_{uuid.uuid4().hex[:8]}"
    LAST_BATCH_ID = batch_id
    ready = [r for r in STATE.final_records.values() if r.get("__ready__")]
    results = []
    for record in ready:
        pub = _record_public(record)
        res = None
        attempts = 0
        while attempts < 3:
            attempts += 1
            res = mock_api.push_record(pub, batch_id)
            if res["ok"]:
                break
        res["attempts"] = attempts
        results.append(res)
        emp_id = record["employee_id"]
        if res["ok"]:
            STATE.audit_entry(emp_id, "pushed", f"Pushed to target system after {attempts} attempt(s).")
        else:
            STATE.audit_entry(emp_id, "push_failed", f"Failed after {attempts} attempt(s): {res['error']}")
    PUSH_RESULTS = results
    STATE.log("success" if all(r["ok"] for r in results) else "warn",
               f"Push batch {batch_id}: {sum(1 for r in results if r['ok'])}/{len(results)} succeeded.")
    return {"batch_id": batch_id, "results": results,
            "succeeded": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}


@app.post("/api/push/retry")
def retry_push():
    global PUSH_RESULTS
    failed = [r for r in PUSH_RESULTS if not r["ok"]]
    ready_by_id = {r["employee_id"]: r for r in STATE.final_records.values() if r.get("__ready__")}
    new_results = []
    for f in failed:
        record = ready_by_id.get(f["employee_id"])
        if not record:
            continue
        pub = _record_public(record)
        res = mock_api.push_record(pub, LAST_BATCH_ID)
        res["attempts"] = 1
        new_results.append(res)
        STATE.audit_entry(f["employee_id"], "retry_pushed" if res["ok"] else "retry_failed",
                           f"Retry {'succeeded' if res['ok'] else 'failed: ' + res.get('error','')}")
    PUSH_RESULTS = [r for r in PUSH_RESULTS if r["ok"]] + new_results
    return {"results": new_results, "succeeded": sum(1 for r in new_results if r["ok"])}


@app.post("/api/push/rollback")
def rollback():
    if not LAST_BATCH_ID:
        return {"ok": False, "error": "no batch to roll back"}
    removed = mock_api.rollback_batch(LAST_BATCH_ID)
    STATE.audit_entry("__batch__", "rollback", f"Rolled back batch {LAST_BATCH_ID}: {removed} record(s) removed.")
    STATE.log("warn", f"Rolled back batch {LAST_BATCH_ID} ({removed} records).")
    return {"ok": True, "removed": removed}


@app.get("/api/target")
def target_snapshot():
    return {"records": mock_api.list_records()}


@app.get("/api/audit")
def get_audit():
    return {"items": STATE.audit}


@app.get("/api/mapping")
def get_mapping():
    return {"mapping": STATE.column_mapping}


@app.get("/api/llm-status")
def llm_status():
    import llm
    return {"enabled": llm.enabled(), "model": llm.MODEL, "provider": getattr(llm, "PROVIDER", None)}


# Serve the frontend
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
