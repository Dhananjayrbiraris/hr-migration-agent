"""
Supabase PostgreSQL database persistence module using SQLAlchemy connection string.
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

def get_db_url() -> Optional[str]:
    url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or os.getenv("SUPABASE_URL")
    if not url:
        try:
            import streamlit as st
            url = st.secrets.get("DATABASE_URL") or st.secrets.get("SUPABASE_DB_URL") or st.secrets.get("SUPABASE_URL")
        except Exception:
            pass
    if url and url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def get_engine():
    global _engine
    url = get_db_url()
    if not url:
        return None
    if _engine is None:
        try:
            _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
        except Exception:
            return None
    return _engine


def init_db():
    engine = get_engine()
    if engine is None:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS target_employees (
                    employee_id VARCHAR(255) PRIMARY KEY,
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    email VARCHAR(255),
                    department VARCHAR(255),
                    job_title VARCHAR(255),
                    hire_date VARCHAR(255),
                    status VARCHAR(255),
                    manager_email VARCHAR(255),
                    location VARCHAR(255),
                    pushed_at VARCHAR(255),
                    batch_id VARCHAR(255)
                );
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY,
                    ts VARCHAR(255),
                    record_id VARCHAR(255),
                    action VARCHAR(255),
                    detail TEXT,
                    before_val TEXT,
                    after_val TEXT
                );
            """))
    except Exception:
        pass


def reset_db():
    engine = get_engine()
    if engine is None:
        return
    init_db()
    try:
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE target_employees;"))
            conn.execute(text("TRUNCATE TABLE audit_logs;"))
    except Exception:
        pass


def save_target_employee(record: Dict[str, Any], batch_id: str) -> bool:
    emp_id = record.get("employee_id")
    engine = get_engine()
    if not emp_id or engine is None:
        return False
    init_db()
    now = datetime.utcnow().isoformat()
    query = text("""
        INSERT INTO target_employees (
            employee_id, first_name, last_name, email, department,
            job_title, hire_date, status, manager_email, location,
            pushed_at, batch_id
        ) VALUES (
            :employee_id, :first_name, :last_name, :email, :department,
            :job_title, :hire_date, :status, :manager_email, :location,
            :pushed_at, :batch_id
        )
        ON CONFLICT (employee_id) DO UPDATE SET
            first_name = EXCLUDED.first_name,
            last_name = EXCLUDED.last_name,
            email = EXCLUDED.email,
            department = EXCLUDED.department,
            job_title = EXCLUDED.job_title,
            hire_date = EXCLUDED.hire_date,
            status = EXCLUDED.status,
            manager_email = EXCLUDED.manager_email,
            location = EXCLUDED.location,
            pushed_at = EXCLUDED.pushed_at,
            batch_id = EXCLUDED.batch_id;
    """)
    params = {
        "employee_id": emp_id,
        "first_name": record.get("first_name"),
        "last_name": record.get("last_name"),
        "email": record.get("email"),
        "department": record.get("department"),
        "job_title": record.get("job_title"),
        "hire_date": record.get("hire_date"),
        "status": record.get("status"),
        "manager_email": record.get("manager_email"),
        "location": record.get("location"),
        "pushed_at": now,
        "batch_id": batch_id,
    }
    try:
        with engine.begin() as conn:
            conn.execute(query, params)
        return True
    except Exception:
        return False


def delete_batch(batch_id: str) -> int:
    engine = get_engine()
    if engine is None:
        return 0
    init_db()
    query = text("DELETE FROM target_employees WHERE batch_id = :batch_id;")
    try:
        with engine.begin() as conn:
            res = conn.execute(query, {"batch_id": batch_id})
            return res.rowcount
    except Exception:
        return 0


def get_all_target_employees() -> List[Dict[str, Any]]:
    engine = get_engine()
    if engine is None:
        return []
    init_db()
    query = text("SELECT * FROM target_employees ORDER BY pushed_at DESC;")
    try:
        with engine.connect() as conn:
            res = conn.execute(query)
            return [dict(row._mapping) for row in res]
    except Exception:
        return []


def save_audit_log(record_id: str, action: str, detail: str, before_val: Optional[str] = None, after_val: Optional[str] = None):
    engine = get_engine()
    if engine is None:
        return
    init_db()
    ts = datetime.utcnow().isoformat() + "Z"
    query = text("""
        INSERT INTO audit_logs (ts, record_id, action, detail, before_val, after_val)
        VALUES (:ts, :record_id, :action, :detail, :before_val, :after_val);
    """)
    params = {
        "ts": ts,
        "record_id": record_id,
        "action": action,
        "detail": detail,
        "before_val": str(before_val) if before_val else None,
        "after_val": str(after_val) if after_val else None,
    }
    try:
        with engine.begin() as conn:
            conn.execute(query, params)
    except Exception:
        pass


def get_audit_logs() -> List[Dict[str, Any]]:
    engine = get_engine()
    if engine is None:
        return []
    init_db()
    query = text("SELECT ts, record_id, action, detail, before_val, after_val FROM audit_logs ORDER BY id DESC;")
    try:
        with engine.connect() as conn:
            res = conn.execute(query)
            return [dict(row._mapping) for row in res]
    except Exception:
        return []
