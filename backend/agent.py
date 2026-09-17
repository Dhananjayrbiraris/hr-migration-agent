"""
Migration agent.

Pipeline stages, each emitting structured log events so the UI can show a
live trace of what the agent is doing:

  1. ingest        - load every source file, keep provenance per row
  2. map_columns    - score each source column against target schema fields,
                       auto-apply confident/unambiguous mappings, escalate
                       genuinely ambiguous ones
  3. reshape         - split combined full-name columns where safe
  4. clean           - per-field normalization (whitespace, case, dates,
                       emails); anything that can't be confidently cleaned
                       is escalated, not guessed
  5. reconcile       - merge rows referring to the same employee across
                       files/duplicates; safe merges are automatic, real
                       conflicts are escalated
  6. validate        - required fields / type checks; a record that still
                       fails after the clean+reconcile passes is escalated

Escalation is deliberately NOT triggered by "this cell isn't perfect" --
it's triggered by "the agent would otherwise be guessing on the client's
behalf". See ESCALATION POLICY comments inline at each decision point.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd
from dateutil import parser as dateparser
from rapidfuzz import fuzz

from schema import TARGET_SCHEMA, FIELD_NAMES, REQUIRED_FIELDS, FIELD_TYPES, VIRTUAL_FULL_NAME_ALIASES

try:
    import llm
except Exception:
    llm = None

# ---------------------------------------------------------------------------
# Confidence thresholds -- the actual "autonomy boundary" of the agent.
# Tuned here in one place so the boundary is explicit and defensible.
# ---------------------------------------------------------------------------
MAP_AUTO_THRESHOLD = 78          # column<->field score above which we auto-map
MAP_AMBIGUITY_GAP = 10           # if top-2 candidate fields are within this gap, escalate
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
DUP_KEY_FUZZY_THRESHOLD = 92     # name+email similarity to treat two rows as the same person


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@dataclass
class Escalation:
    id: str
    kind: str                # "mapping" | "clean" | "merge_conflict" | "validation"
    title: str
    detail: str
    context: dict[str, Any]
    options: list[str]
    status: str = "pending"  # pending | approved | corrected | rejected
    resolution: dict[str, Any] | None = None
    ai_suggestion: dict[str, Any] | None = None


@dataclass
class AgentState:
    events: list[dict[str, Any]] = field(default_factory=list)
    escalations: dict[str, Escalation] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    column_mapping: dict[str, dict[str, str]] = field(default_factory=dict)  # file -> {col: field}
    raw_frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    working_records: list[dict[str, Any]] = field(default_factory=list)  # cleaned, pre-reconcile
    final_records: dict[str, dict[str, Any]] = field(default_factory=dict)  # employee_id -> record
    rejected_records: list[dict[str, Any]] = field(default_factory=list)
    stage: str = "idle"

    def log(self, level: str, message: str, **extra):
        self.events.append({
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "level": level,   # info | action | escalate | warn | success
            "message": message,
            **extra,
        })

    def audit_entry(self, record_id: str, action: str, detail: str, before=None, after=None):
        self.audit.append({
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "record_id": record_id,
            "action": action,
            "detail": detail,
            "before": before,
            "after": after,
        })

    def raise_escalation(self, kind: str, title: str, detail: str, context: dict, options: list[str]) -> str:
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


def best_field_matches(column_name: str) -> list[tuple[str, int]]:
    """Score a source column name against every target field's alias list.
    Returns [(field_name, score), ...] sorted descending, plus a synthetic
    'full_name' pseudo-candidate for reshape detection."""
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
        # normalize apostrophe-name casing like O'Brien
        s = re.sub(r"\b([a-z])", lambda m: m.group(1).upper(), s)
    return s


def try_clean_email(raw: Any) -> tuple[str | None, bool, str]:
    """Returns (cleaned_value, is_confidently_fixed, note)."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, False, "missing"
    s = str(raw).strip().lower()
    if s == "" or s in {"n/a", "na"}:
        return None, False, "missing"
    note = ""
    # Safe, unambiguous fix: collapse an accidental doubled '@'
    if "@@" in s:
        s = s.replace("@@", "@")
        note = "collapsed doubled '@'"
    if EMAIL_RE.match(s):
        return s, True, note or "already valid"
    return s, False, "does not match a valid email pattern after safe cleanup"


def try_parse_date(raw: Any) -> tuple[str | None, bool]:
    """Two-attempt date parsing. Returns (iso_date_or_None, succeeded)."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, False
    s = str(raw).strip()
    if s.lower() in {"n/a", "na", "not available", "none", ""}:
        return None, False
    # Attempt 1: direct dateutil parse (handles most real-world formats)
    try:
        dt = dateparser.parse(s, dayfirst=False, fuzzy=False)
        return dt.date().isoformat(), True
    except Exception:
        pass
    # Attempt 2: pull the first date-like substring out with fuzzy parsing
    try:
        dt = dateparser.parse(s, fuzzy=True)
        return dt.date().isoformat(), True
    except Exception:
        return None, False


def split_full_name(name: str) -> tuple[str | None, str | None, bool]:
    """Returns (first, last, confident). Only 2-token names are safe to
    auto-split; anything else is genuinely ambiguous (which token is the
    surname?) and must be escalated."""
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if len(parts) == 2:
        return parts[0], parts[1], True
    return None, None, False


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def ingest(state: AgentState, files: dict[str, str]):
    """files: {display_name: path}"""
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
    state.log("info", "Proposing source-to-target column mapping.")
    for file_name, df in state.raw_frames.items():
        mapping: dict[str, str] = {}
        for col in df.columns:
            ranked = best_field_matches(col)
            top_field, top_score = ranked[0]
            second_field, second_score = ranked[1] if len(ranked) > 1 else (None, 0)

            # ESCALATION POLICY (mapping): only escalate when two *different*
            # target fields are both plausible candidates for the same
            # column -- i.e. the agent would be silently guessing which one
            # is right. A single clear best match, even if not a perfect
            # 100, is applied autonomously.
            if top_score >= MAP_AUTO_THRESHOLD and (top_score - second_score) >= MAP_AMBIGUITY_GAP:
                if top_field == "__full_name__":
                    mapping[col] = "__full_name__"
                else:
                    mapping[col] = top_field
                state.log("action", f"[{file_name}] Mapped column '{col}' -> '{top_field}' (confidence {top_score}%).",
                           file=file_name, column=col, target=top_field, confidence=top_score)
            elif top_score >= MAP_AUTO_THRESHOLD and (top_score - second_score) < MAP_AMBIGUITY_GAP:
                # genuinely ambiguous: two candidate target fields, close scores
                eid = state.raise_escalation(
                    kind="mapping",
                    title=f"Ambiguous column mapping: '{col}' in {file_name}",
                    detail=(f"Column '{col}' scores similarly against multiple target fields "
                            f"('{top_field}': {top_score}%, '{second_field}': {second_score}%). "
                            f"Auto-mapping risks silently sending data to the wrong field."),
                    context={"file": file_name, "column": col, "candidates": [top_field, second_field],
                             "sample_values": df[col].head(3).tolist()},
                    options=[top_field, second_field, "ignore this column"],
                )
                mapping[col] = f"__pending__:{eid}"
            else:
                state.log("warn", f"[{file_name}] Column '{col}' had no confident match "
                                   f"(best: '{top_field}' at {top_score}%) -- left unmapped.",
                           file=file_name, column=col)
                mapping[col] = "__unmapped__"
        state.column_mapping[file_name] = mapping


def apply_resolved_mapping(state: AgentState, file_name: str, column: str, chosen_field: str):
    """Called when a human resolves a mapping escalation."""
    state.column_mapping[file_name][column] = chosen_field
    state.audit_entry("mapping", "mapping_resolved",
                       f"Human mapped '{column}' in {file_name} -> '{chosen_field}'")


def reshape_and_extract(state: AgentState):
    """Turn each raw row into a dict of target-field-ish values (still raw/dirty),
    using the resolved column mapping. Splits full-name columns where safe."""
    state.stage = "reshape"
    for file_name, df in state.raw_frames.items():
        mapping = state.column_mapping[file_name]
        for idx, row in df.iterrows():
            record: dict[str, Any] = {"__source_file__": file_name, "__source_row__": int(idx)}
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
                            title=f"Cannot confidently split full name '{raw_val}'",
                            detail=(f"'{raw_val}' has {'0 or 1' if len(str(raw_val).split())<2 else '3+'} "
                                    f"name tokens -- which token(s) are the surname is not obvious "
                                    f"(compound surnames, middle names, missing name)."),
                            context={"file": file_name, "row": int(idx), "raw_value": raw_val,
                                      "field": "__full_name__", "employee_id": record.get("employee_id")},
                            options=["let me split it manually", "skip this record"],
                        )
                        record["first_name"] = f"__pending__:{eid}"
                        record["last_name"] = f"__pending__:{eid}"
                else:
                    record[target] = raw_val
            state.working_records.append(record)
    state.log("action", f"Reshaped {len(state.working_records)} raw records from all sources.")


def clean_records(state: AgentState):
    state.stage = "clean"
    state.log("info", "Cleaning fields: whitespace/casing, dates, emails.")
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
                        title=f"Unrecognizable email '{cleaned}'",
                        detail=f"Email for record {record.get('employee_id','?')} ({note}) could not be "
                               f"confidently repaired without guessing the intended address.",
                        context={"employee_id": record.get("employee_id"), "raw_email": cleaned,
                                  "file": record["__source_file__"]},
                        options=["correct the email", "skip this record"],
                    )
                    record["email"] = f"__pending__:{eid}"

        if "hire_date" in record and not (isinstance(record["hire_date"], str) and record["hire_date"].startswith("__pending__")):
            iso, ok = try_parse_date(record.get("hire_date"))
            if ok:
                record["hire_date"] = iso
            else:
                eid = state.raise_escalation(
                    kind="validation",
                    title=f"Unparseable hire date for {record.get('employee_id','?')}",
                    detail=(f"Raw value '{record.get('hire_date')}' failed both the direct-parse and "
                             f"fuzzy-parse attempts. Required field, agent will not guess a date."),
                    context={"employee_id": record.get("employee_id"), "raw_value": record.get("hire_date"),
                              "file": record["__source_file__"]},
                    options=["enter correct date", "skip this record"],
                )
                record["hire_date"] = f"__pending__:{eid}"

        if record.get("employee_id"):
            record["employee_id"] = str(record["employee_id"]).strip()
        if record.get("status"):
            s = str(record["status"]).strip().lower()
            record["status"] = "Active" if s in {"active", "yes", "y", "true", "1"} else \
                                 "Inactive" if s in {"inactive", "no", "n", "false", "0"} else record["status"]
    state.log("success", "Field-level cleaning pass complete.")


def _is_pending(v) -> bool:
    return isinstance(v, str) and v.startswith("__pending__")


def reconcile(state: AgentState):
    """Merge working_records referring to the same employee. Safe merges
    (filling gaps, resolving exact/whitespace-only duplicates) happen
    automatically. Real value conflicts on the same field are escalated."""
    state.stage = "reconcile"
    state.log("info", "Reconciling records across sources (dedup + merge).")
    groups: dict[str, list[dict]] = {}
    for r in state.working_records:
        key = r.get("employee_id") or f"__nokey__{r['__source_file__']}_{r['__source_row__']}"
        groups.setdefault(key, []).append(r)

    for emp_id, rows in groups.items():
        if emp_id.startswith("__nokey__"):
            for r in rows:
                state.final_records[new_id("noid")] = r
            continue

        merged: dict[str, Any] = {"employee_id": emp_id}
        conflicts: dict[str, set] = {}
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
                    # keep pending marker so the escalation still resolves it
                    if _is_pending(val):
                        merged[f] = val
                elif str(merged[f]).strip().lower() == str(val).strip().lower():
                    continue  # identical (case/space-insensitive) -> ignore, not a conflict
                else:
                    conflicts.setdefault(f, {merged[f]}).add(val)

        if len(rows) > 1:
            if conflicts:
                for f, vals in conflicts.items():
                    eid = state.raise_escalation(
                        kind="merge_conflict",
                        title=f"Conflicting '{f}' for employee {emp_id} across sources",
                        detail=(f"Sources {sorted(set(sources))} disagree on '{f}': {sorted(vals)}. "
                                f"This is a genuine data conflict, not a formatting difference -- "
                                f"picking one silently could push wrong data to the target system."),
                        context={"employee_id": emp_id, "field": f, "candidate_values": sorted(vals),
                                  "sources": sorted(set(sources))},
                        options=sorted(vals) + ["keep most recent source"],
                    )
                    merged[f] = f"__pending__:{eid}"
                state.log("escalate", f"Merge conflict on employee {emp_id}: {list(conflicts.keys())}")
            else:
                state.log("action", f"Auto-merged {len(rows)} records for employee {emp_id} "
                                     f"from {sorted(set(sources))} (no conflicting fields).",
                           employee_id=emp_id)
                state.audit_entry(emp_id, "auto_merge",
                                   f"Merged {len(rows)} rows from {sorted(set(sources))}; "
                                   f"exact/near-duplicate or complementary fields, no conflicts.")
        merged["__sources__"] = sorted(set(sources))
        state.final_records[emp_id] = merged
    state.log("success", f"Reconciliation complete -> {len(state.final_records)} candidate employee records.")


def _covered_by_pending_mapping(state: AgentState, field: str, sources: list[str]) -> bool:
    """True if a field's absence is already explained by an unresolved
    mapping-ambiguity escalation on one of this record's source files --
    in which case we must NOT raise a second, duplicate escalation for the
    same root cause. Resolving the mapping escalation will backfill the
    field automatically (see apply_resolved_mapping)."""
    for e in state.escalations.values():
        if e.kind == "mapping" and e.status == "pending" and e.context.get("file") in sources:
            if field in e.context.get("candidates", []):
                return True
    return False


def validate(state: AgentState):
    state.stage = "validate"
    state.log("info", "Running required-field / type validation.")
    for emp_id, record in list(state.final_records.items()):
        sources = record.get("__sources__", [])
        missing = [f for f in REQUIRED_FIELDS if not record.get(f) or _is_pending(record.get(f))]
        # A field can be missing for three reasons, and only one of them
        # should mint a *new* escalation:
        #   (a) already __pending__ from an earlier stage -> already queued, skip
        #   (b) absent because an unresolved mapping-ambiguity escalation
        #       covers it -> same root cause, already queued, skip (this is
        #       the fix for a duplicate-escalation bug found during testing)
        #   (c) genuinely absent with nothing else explaining it -> escalate
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
                title=f"Missing required field(s) for employee {emp_id}",
                detail=f"Required field(s) {genuinely_missing} are empty after cleaning and reconciliation.",
                context={"employee_id": emp_id, "missing_fields": genuinely_missing, "record": record},
                options=["fill in manually", "skip this record"],
            )
            for f in genuinely_missing:
                record[f] = f"__pending__:{eid}"
        record["__ready__"] = (
            not any(_is_pending(record.get(f)) for f in FIELD_NAMES)
            and not still_blocked_on_mapping
        )
    ready = sum(1 for r in state.final_records.values() if r["__ready__"])
    state.log("success", f"Validation complete: {ready}/{len(state.final_records)} records ready to push, "
                          f"{len(state.escalations)} total escalation(s) raised so far.")


# ---------------------------------------------------------------------------
# Human resolution of escalations
# ---------------------------------------------------------------------------

def _find_employee_id_for_row(state: AgentState, file_name: str, row_idx: int) -> str | None:
    mapping = state.column_mapping.get(file_name, {})
    id_col = next((c for c, t in mapping.items() if t == "employee_id"), None)
    if not id_col:
        return None
    df = state.raw_frames[file_name]
    return str(df.iloc[row_idx][id_col]).strip()


def resolve_escalation(state: AgentState, eid: str, action: str, value: Any = None) -> dict:
    """action: 'approve' (accept agent's best guess / suggested option),
               'correct' (human supplies the value), or 'reject' (drop the record)."""
    esc = state.escalations.get(eid)
    if not esc or esc.status != "pending":
        return {"ok": False, "error": "escalation not found or already resolved"}

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
                                   f"Applied human-resolved mapping '{column}'->'{chosen_field}' "
                                   f"from {file_name}", before=before, after=cleaned)
        # Resolving a mapping ambiguity can newly satisfy a required field
        # for every record drawn from that file -- recompute readiness.
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
                state.audit_entry(emp_id, "record_rejected", f"Record dropped by human via escalation '{esc.title}'")
        elif field_name == "__full_name__":
            # value expected as {"first_name": ..., "last_name": ...} or "First Last" string
            if record is not None:
                if isinstance(value, dict):
                    first, last = value.get("first_name"), value.get("last_name")
                elif isinstance(value, str) and " " in value:
                    first, last = value.split(" ", 1)
                else:
                    first, last = value, ""
                before = (record.get("first_name"), record.get("last_name"))
                record["first_name"], record["last_name"] = clean_whitespace_case(first, "name"), clean_whitespace_case(last, "name")
                state.audit_entry(emp_id, "field_corrected", "Human split full name",
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
                        state.audit_entry(emp_id, "field_corrected", f"Human supplied '{f}'", before=before, after=record[f])
                elif field_name:
                    before = record.get(field_name)
                    record[field_name] = new_val
                    state.audit_entry(emp_id, "field_corrected" if action == "correct" else "field_approved",
                                       f"Human resolved '{field_name}'", before=before, after=new_val)
                record["__ready__"] = not any(_is_pending(record.get(f)) for f in FIELD_NAMES)

    esc.status = "approved" if action == "approve" else ("rejected" if action == "reject" else "corrected")
    esc.resolution = {"action": action, "value": value}
    state.log("info", f"Escalation resolved ({action}): {esc.title}", escalation_id=eid)
    return {"ok": True}


def run_pipeline(state: AgentState, files: dict[str, str]):
    ingest(state, files)
    map_columns(state)
    # Mapping escalations must be resolved before reshape can use them fully;
    # reshape simply skips __pending__ mapped columns until resolved, and the
    # UI lets the human resolve + re-run reshape/clean afterward via /api/continue.
    reshape_and_extract(state)
    clean_records(state)
    reconcile(state)
    validate(state)
    state.stage = "review"
    state.log("info", "Pipeline paused for human review of escalation queue." if state.escalations
               else "Pipeline complete with zero escalations.")
