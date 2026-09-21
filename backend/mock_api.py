"""
Target system API client backed by SQLite storage.
"""

import random
import time
from typing import Any, Dict, List

try:
    from . import database
except ImportError:
    import database

_FAIL_RATE = 0.15


def reset():
    database.reset_db()


def push_record(record: Dict[str, Any], batch_id: str) -> Dict[str, Any]:
    time.sleep(0.05)
    emp_id = record.get("employee_id")
    if random.random() < _FAIL_RATE:
        return {
            "employee_id": emp_id,
            "ok": False,
            "error": random.choice(["timeout", "422 validation error", "rate limited"])
        }
    database.save_target_employee(record, batch_id)
    return {"employee_id": emp_id, "ok": True}


def rollback_batch(batch_id: str) -> int:
    return database.delete_batch(batch_id)


def list_records() -> List[Dict[str, Any]]:
    return database.get_all_target_employees()
