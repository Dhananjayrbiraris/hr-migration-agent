"""
Data migration engine and escalation pipeline.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from dateutil import parser as dateparser
from rapidfuzz import fuzz

from schema import TARGET_SCHEMA, FIELD_NAMES, REQUIRED_FIELDS, FIELD_TYPES, VIRTUAL_FULL_NAME_ALIASES

try:
    from . import llm, database
except ImportError:
    import llm
    import database

# Scoring thresholds for column mapping and duplicate matching
MAP_AUTO_THRESHOLD = 78
MAP_AMBIGUITY_GAP = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
DUP_KEY_FUZZY_THRESHOLD = 92


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@dataclass
class Escalation:
    id: str
    kind: str
    title: str
    detail: str
    context: Dict[str, Any]
    options: List[str]
    status: str = "pending"
    resolution: Optional[Dict[str, Any]] = None
    ai_suggestion: Optional[Dict[str, Any]] = None


@dataclass
class AgentState:
    events: List[Dict[str, Any]] = field(default_factory=list)
    escalations: Dict[str, Escalation] = field(default_factory=dict)
    audit: List[Dict[str, Any]] = field(default_factory=list)
    column_mapping: Dict[str, Dict[str, str]] = field(default_factory=dict)
    raw_frames: Dict[str, pd.DataFrame] = field(default_factory=dict)
    working_records: List[Dict[str, Any]] = field(default_factory=list)
    final_records: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    rejected_records: List[Dict[str, Any]] = field(default_factory=list)
    stage: str = "idle"

    def log(self, level: str, message: str, **extra):
        self.events.append({
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "level": level,
            "message": message,
            **extra,
        })

    def audit_entry(self, record_id: str, action: str, detail: str, before=None, after=None):
        entry = {
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "record_id": record_id,
            "action": action,
            "detail": detail,
            "before": before,
            "after": after,
        }
        self.audit.append(entry)
        try:
            database.save_audit_log(record_id, action, detail, str(before) if before else None, str(after) if after else None)
        except Exception:
            pass

    def raise_escalation(self, kind: str, title: str, detail: str, context: dict, options: List[str]) -> str:
        eid = new_id("esc")
        esc = Escalation(eid, kind, title, detail, context, options)
        if llm is not None and llm.enabled():
            suggestion = llm.suggest_escalation_resolution(kind, title, detail, context, options)
            if suggestion and "error" not in suggestion:
                esc.ai_suggestion = suggestion
                self.log("info", f"AI suggestion for '{title}': {suggestion['suggestion']} "
                                  f"({suggestion['confidence']} confidence).", escalation_id=eid)
            elif suggestion:
                self.log("warn", f"AI suggestion unavailable for '{title}': {suggestion.get('error')}",
                          escalation_id=eid)
        self.escalations[eid] = esc
        self.log("escalate", f"Escalated: {title}", escalation_id=eid, kind=kind)
        return eid


def best_field_matches(column_name: str) -> List[Tuple[str, int]]:
    col_norm = column_name.strip().lower().replace("_", " ").replace("?", "")
    scores = []
    for f in TARGET_SCHEMA:
        alias_scores = [fuzz.token_sort_ratio(col_norm, a) for a in f["aliases"] + [f["name"].replace("_", " ")]]
        scores.append((f["name"], max(alias_scores)))
    full_name_score = max(fuzz.token_sort_ratio(col_norm, a) for a in VIRTUAL_FULL_NAME_ALIASES)
    scores.append(("__full_name__", full_name_score))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores


def clean_whitespace_case(value: Any, kind: str) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if s == "" or s.lower() in {"n/a", "na", "none", "null"}:
        return None
    s = re.sub(r"\s+", " ", s)
    if kind == "name":
        s = s.title() if s.isupper() or s.islower() else s
        s = re.sub(r"\b([a-z])", lambda m: m.group(1).upper(), s)
    return s


def try_clean_email(raw: Any) -> Tuple[Optional[str], bool, str]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, False, "missing"
    s = str(raw).strip().lower()
    if s == "" or s in {"n/a", "na"}:
        return None, False, "missing"
    note = ""
    if "@@" in s:
        s = s.replace("@@", "@")
        note = "collapsed double '@'"
    if EMAIL_RE.match(s):
        return s, True, note or "valid email"
    return s, False, "invalid email format"


def try_parse_date(raw: Any) -> Tuple[Optional[str], bool]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, False
    s = str(raw).strip()
    if s.lower() in {"n/a", "na", "not available", "none", ""}:
        return None, False
    try:
        dt = dateparser.parse(s, dayfirst=False, fuzzy=False)
        return dt.date().isoformat(), True
    except Exception:
        pass
    try:
        dt = dateparser.parse(s, fuzzy=True)
        return dt.date().isoformat(), True
    except Exception:
        return None, False


def split_full_name(name: str) -> Tuple[Optional[str], Optional[str], bool]:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if len(parts) == 2:
        return parts[0], parts[1], True
    return None, None, False


def ingest(state: AgentState, files: Dict[str, str]):
    state.stage = "ingest"
    state.log("info", f"Starting ingestion of {len(files)} source file(s).")
    for name, path in files.items():
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        df = df.rename(columns=lambda c: c.strip())
        state.raw_frames[name] = df
        state.log("action", f"Loaded '{name}': {len(df)} rows, {len(df.columns)} columns.",
                   file=name, rows=len(df), columns=list(df.columns))


def map_columns(state: AgentState):
    state.stage = "map_columns"
    state.log("info", "Mapping source columns to target schema.")
    for file_name, df in state.raw_frames.items():
        mapping: Dict[str, str] = {}
        for col in df.columns:
            ranked = best_field_matches(col)
            top_field, top_score = ranked[0]
            second_field, second_score = ranked[1] if len(ranked) > 1 else (None, 0)

            if top_score >= MAP_AUTO_THRESHOLD and (top_score - second_score) >= MAP_AMBIGUITY_GAP:
                mapping[col] = top_field
                state.log("action", f"[{file_name}] Mapped column '{col}' -> '{top_field}' ({top_score}% confidence).",
                           file=file_name, column=col, target=top_field, confidence=top_score)
            elif top_score >= MAP_AUTO_THRESHOLD and (top_score - second_score) < MAP_AMBIGUITY_GAP:
                eid = state.raise_escalation(
                    kind="mapping",
                    title=f"Ambiguous column mapping: '{col}' in {file_name}",
                    detail=f"Column '{col}' scored closely across multiple target fields ({top_field}: {top_score}%, {second_field}: {second_score}%).",
                    context={"file": file_name, "column": col, "candidates": [top_field, second_field],
                             "sample_values": df[col].head(3).tolist()},
                    options=[top_field, second_field, "ignore this column"],
                )
                mapping[col] = f"__pending__:{eid}"
            else:
                state.log("warn", f"[{file_name}] Column '{col}' unmapped (best match: '{top_field}' at {top_score}%).",
                           file=file_name, column=col)
                mapping[col] = "__unmapped__"
        state.column_mapping[file_name] = mapping


def apply_resolved_mapping(state: AgentState, file_name: str, column: str, chosen_field: str):
    state.column_mapping[file_name][column] = chosen_field
    state.audit_entry("mapping", "mapping_resolved",
                       f"Mapped '{column}' in {file_name} -> '{chosen_field}'")


def reshape_and_extract(state: AgentState):
    state.stage = "reshape"
    for file_name, df in state.raw_frames.items():
        mapping = state.column_mapping[file_name]
        for idx, row in df.iterrows():
            record: Dict[str, Any] = {"__source_file__": file_name, "__source_row__": int(idx)}
            for col, target in mapping.items():
                if target in ("__unmapped__",) or target.startswith("__pending__"):
                    continue
                raw_val = row[col]
                if target == "__full_name__":
                    first, last, confident = split_full_name(str(raw_val)) if raw_val else (None, None, False)
                    if confident:
                        record["first_name"] = first
                        record["last_name"] = last
                    else:
                        eid = state.raise_escalation(
                            kind="clean",
                            title=f"Unable to split full name '{raw_val}'",
                            detail=f"Multiple name tokens detected in '{raw_val}'. Manual resolution required.",
                            context={"file": file_name, "row": int(idx), "raw_value": raw_val,
                                      "field": "__full_name__", "employee_id": record.get("employee_id")},
                            options=["split manually", "skip record"],
                        )
                        record["first_name"] = f"__pending__:{eid}"
                        record["last_name"] = f"__pending__:{eid}"
                else:
                    record[target] = raw_val
            state.working_records.append(record)
    state.log("action", f"Extracted {len(state.working_records)} working records.")


def clean_records(state: AgentState):
    state.stage = "clean"
    state.log("info", "Cleaning fields: whitespace, date parsing, email formatting.")
    for record in state.working_records:
        for f in ("first_name", "last_name", "department", "job_title", "status", "location"):
            if f in record and not (isinstance(record[f], str) and record[f].startswith("__pending__")):
                record[f] = clean_whitespace_case(record.get(f), "name" if "name" in f else "text")

        for f in ("email", "manager_email"):
            if f in record and not (isinstance(record[f], str) and record[f].startswith("__pending__")):
                cleaned, confident, note = try_clean_email(record.get(f))
                record[f] = cleaned
                if cleaned and not confident and f == "email":
                    eid = state.raise_escalation(
                        kind="clean",
                        title=f"Invalid email '{cleaned}'",
                        detail=f"Email for record {record.get('employee_id', '?')} failed validation: {note}.",
                        context={"employee_id": record.get("employee_id"), "raw_email": cleaned,
                                  "file": record["__source_file__"]},
                        options=["correct email", "skip record"],
                    )
                    record["email"] = f"__pending__:{eid}"

        if "hire_date" in record and not (isinstance(record["hire_date"], str) and record["hire_date"].startswith("__pending__")):
            iso, ok = try_parse_date(record.get("hire_date"))
            if ok:
                record["hire_date"] = iso
            else:
                eid = state.raise_escalation(
                    kind="validation",
                    title=f"Unparseable hire date for employee {record.get('employee_id', '?')}",
                    detail=f"Raw value '{record.get('hire_date')}' could not be parsed into ISO date.",
                    context={"employee_id": record.get("employee_id"), "raw_value": record.get("hire_date"),
                              "file": record["__source_file__"]},
                    options=["enter date", "skip record"],
                )
                record["hire_date"] = f"__pending__:{eid}"

        if record.get("employee_id"):
            record["employee_id"] = str(record["employee_id"]).strip()
        if record.get("status"):
            s = str(record["status"]).strip().lower()
            record["status"] = "Active" if s in {"active", "yes", "y", "true", "1"} else \
                                 "Inactive" if s in {"inactive", "no", "n", "false", "0"} else record["status"]
    state.log("success", "Field-level cleaning pass completed.")


def _is_pending(v) -> bool:
    return isinstance(v, str) and v.startswith("__pending__")


def reconcile(state: AgentState):
    state.stage = "reconcile"
    state.log("info", "Reconciling and deduplicating employee records.")
    groups: Dict[str, List[dict]] = {}
    for r in state.working_records:
        key = r.get("employee_id") or f"__nokey__{r['__source_file__']}_{r['__source_row__']}"
        groups.setdefault(key, []).append(r)

    for emp_id, rows in groups.items():
        if emp_id.startswith("__nokey__"):
            for r in rows:
                state.final_records[new_id("noid")] = r
            continue

        merged: Dict[str, Any] = {"employee_id": emp_id}
        conflicts: Dict[str, set] = {}
        sources = []
        for r in rows:
            sources.append(r["__source_file__"])
            for f in FIELD_NAMES:
                if f == "employee_id":
                    continue
                val = r.get(f)
                if val is None or val == "":
                    continue
                if f not in merged:
                    merged[f] = val
                elif _is_pending(merged[f]) or _is_pending(val):
                    if _is_pending(val):
                        merged[f] = val
                elif str(merged[f]).strip().lower() == str(val).strip().lower():
                    continue
                else:
                    conflicts.setdefault(f, {merged[f]}).add(val)

        if len(rows) > 1:
            if conflicts:
                for f, vals in conflicts.items():
                    eid = state.raise_escalation(
                        kind="merge_conflict",
                        title=f"Conflicting field '{f}' for employee {emp_id}",
                        detail=f"Disagreement across sources {sorted(set(sources))} on field '{f}': {sorted(vals)}.",
                        context={"employee_id": emp_id, "field": f, "candidate_values": sorted(vals),
                                  "sources": sorted(set(sources))},
                        options=sorted(vals) + ["keep latest source"],
                    )
                    merged[f] = f"__pending__:{eid}"
                state.log("escalate", f"Merge conflict on employee {emp_id}: {list(conflicts.keys())}")
            else:
                state.log("action", f"Auto-merged {len(rows)} records for employee {emp_id}.",
                           employee_id=emp_id)
                state.audit_entry(emp_id, "auto_merge", f"Merged {len(rows)} records from {sorted(set(sources))}.")
        merged["__sources__"] = sorted(set(sources))
        state.final_records[emp_id] = merged
    state.log("success", f"Reconciliation complete: {len(state.final_records)} unique records.")


def _covered_by_pending_mapping(state: AgentState, field: str, sources: List[str]) -> bool:
    for e in state.escalations.values():
        if e.kind == "mapping" and e.status == "pending" and e.context.get("file") in sources:
            if field in e.context.get("candidates", []):
                return True
    return False


def validate(state: AgentState):
    state.stage = "validate"
    state.log("info", "Validating required fields.")
    for emp_id, record in list(state.final_records.items()):
        sources = record.get("__sources__", [])
        missing = [f for f in REQUIRED_FIELDS if not record.get(f) or _is_pending(record.get(f))]
        genuinely_missing = [
            f for f in missing
            if not _is_pending(record.get(f)) and not _covered_by_pending_mapping(state, f, sources)
        ]
        still_blocked_on_mapping = [
            f for f in missing
            if not _is_pending(record.get(f)) and _covered_by_pending_mapping(state, f, sources)
        ]
        if genuinely_missing:
            eid = state.raise_escalation(
                kind="validation",
                title=f"Missing required fields for employee {emp_id}",
                detail=f"Required fields {genuinely_missing} missing after reconciliation.",
                context={"employee_id": emp_id, "missing_fields": genuinely_missing, "record": record},
                options=["fill manually", "skip record"],
            )
            for f in genuinely_missing:
                record[f] = f"__pending__:{eid}"
        record["__ready__"] = (
            not any(_is_pending(record.get(f)) for f in FIELD_NAMES)
            and not still_blocked_on_mapping
        )
    ready = sum(1 for r in state.final_records.values() if r["__ready__"])
    state.log("success", f"Validation complete: {ready}/{len(state.final_records)} records ready.")


def _find_employee_id_for_row(state: AgentState, file_name: str, row_idx: int) -> Optional[str]:
    mapping = state.column_mapping.get(file_name, {})
    id_col = next((c for c, t in mapping.items() if t == "employee_id"), None)
    if not id_col:
        return None
    df = state.raw_frames[file_name]
    return str(df.iloc[row_idx][id_col]).strip()


def resolve_escalation(state: AgentState, eid: str, action: str, value: Any = None) -> dict:
    esc = state.escalations.get(eid)
    if not esc or esc.status != "pending":
        return {"ok": False, "error": "Escalation not found or already resolved"}

    if esc.kind == "mapping":
        file_name = esc.context["file"]
        column = esc.context["column"]
        if action == "reject":
            chosen_field = "__unmapped__"
        else:
            chosen_field = value if action == "correct" else esc.options[0]
        apply_resolved_mapping(state, file_name, column, chosen_field)
        if chosen_field != "__unmapped__":
            df = state.raw_frames[file_name]
            for idx, row in df.iterrows():
                raw_val = row[column]
                emp_id = _find_employee_id_for_row(state, file_name, idx)
                target_key = emp_id if emp_id and emp_id in state.final_records else None
                if not target_key:
                    continue
                cleaned = clean_whitespace_case(raw_val, "name" if "name" in chosen_field else "text")
                if FIELD_TYPES.get(chosen_field) == "email":
                    cleaned, _, _ = try_clean_email(raw_val)
                elif FIELD_TYPES.get(chosen_field) == "date":
                    cleaned, ok = try_parse_date(raw_val)
                before = state.final_records[target_key].get(chosen_field)
                state.final_records[target_key][chosen_field] = cleaned
                state.audit_entry(target_key, "mapping_applied",
                                   f"Mapped '{column}' -> '{chosen_field}'", before=before, after=cleaned)
        for rec in state.final_records.values():
            missing_required = [f for f in REQUIRED_FIELDS if not rec.get(f) or _is_pending(rec.get(f))]
            if not missing_required:
                rec["__ready__"] = True

    elif esc.kind in ("clean", "validation", "merge_conflict"):
        emp_id = esc.context.get("employee_id")
        field_name = esc.context.get("field")
        record = state.final_records.get(emp_id)
        if action == "reject":
            if record:
                state.rejected_records.append(record)
                del state.final_records[emp_id]
                state.audit_entry(emp_id, "record_rejected", f"Record rejected: '{esc.title}'")
        elif field_name == "__full_name__":
            if record is not None:
                if isinstance(value, dict):
                    first, last = value.get("first_name"), value.get("last_name")
                elif isinstance(value, str) and " " in value:
                    first, last = value.split(" ", 1)
                else:
                    first, last = value, ""
                before = (record.get("first_name"), record.get("last_name"))
                record["first_name"], record["last_name"] = clean_whitespace_case(first, "name"), clean_whitespace_case(last, "name")
                state.audit_entry(emp_id, "field_corrected", "Manually split name",
                                   before=before, after=(record["first_name"], record["last_name"]))
                record["__ready__"] = not any(_is_pending(record.get(f)) for f in FIELD_NAMES)
        else:
            if action == "approve":
                new_val = esc.options[0] if esc.options else None
            else:
                new_val = value
            if record is not None:
                if esc.kind == "validation" and esc.context.get("missing_fields"):
                    for f in esc.context["missing_fields"]:
                        before = record.get(f)
                        record[f] = new_val if len(esc.context["missing_fields"]) == 1 else new_val.get(f) if isinstance(new_val, dict) else new_val
                        state.audit_entry(emp_id, "field_corrected", f"Updated field '{f}'", before=before, after=record[f])
                elif field_name:
                    before = record.get(field_name)
                    record[field_name] = new_val
                    state.audit_entry(emp_id, "field_corrected" if action == "correct" else "field_approved",
                                       f"Resolved '{field_name}'", before=before, after=new_val)
                record["__ready__"] = not any(_is_pending(record.get(f)) for f in FIELD_NAMES)

    esc.status = "approved" if action == "approve" else ("rejected" if action == "reject" else "corrected")
    esc.resolution = {"action": action, "value": value}
    state.log("info", f"Escalation resolved ({action}): {esc.title}", escalation_id=eid)
    return {"ok": True}


def run_pipeline(state: AgentState, files: Dict[str, str]):
    ingest(state, files)
    map_columns(state)
    reshape_and_extract(state)
    clean_records(state)
    reconcile(state)
    validate(state)
    state.stage = "review"
    state.log("info", "Pipeline finished processing files.")
