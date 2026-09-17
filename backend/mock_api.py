"""
Stand-in for the client's new target platform's API. Deliberately flaky
(simulated ~15% per-record failure) so the push step has something real to
retry against, and keeps its own store so rollback can be demonstrated by
actually deleting committed records.
"""
import random
import time
from datetime import datetime
from typing import Any

_TARGET_DB: dict[str, dict[str, Any]] = {}
_FAIL_RATE = 0.15


def reset():
    _TARGET_DB.clear()


def push_record(record: dict[str, Any], batch_id: str) -> dict[str, Any]:
    """Simulates one API call to the target system for a single record."""
    time.sleep(0.05)
    emp_id = record.get("employee_id")
    if random.random() < _FAIL_RATE:
        return {"employee_id": emp_id, "ok": False,
                "error": random.choice(["timeout", "422 validation error", "rate limited"])}
    _TARGET_DB[emp_id] = {**record, "_pushed_at": datetime.utcnow().isoformat(), "_batch_id": batch_id}
    return {"employee_id": emp_id, "ok": True}


def rollback_batch(batch_id: str) -> int:
    to_remove = [k for k, v in _TARGET_DB.items() if v.get("_batch_id") == batch_id]
    for k in to_remove:
        del _TARGET_DB[k]
    return len(to_remove)


def list_records() -> list[dict[str, Any]]:
    return list(_TARGET_DB.values())
