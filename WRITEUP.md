# Technical Write-Up: Client Data Migration Agent

## 1. Approach & Architecture
The system handles multi-source legacy data migration (e.g., HRIS and CRM exports) to a unified target schema using a hybrid approach:
- **Deterministic Core Pipeline**: Ingest, column mapping, schema transformation, field cleaning, multi-source record reconciliation, and schema validation are built with Python, Pandas, and RapidFuzz. This guarantees reproducible, testable, and inspectable data transformations.
- **Human-in-the-Loop Supervision**: The pipeline autonomously processes confident operations while surfacing genuinely ambiguous edge cases to a real-time web console.
- **AI-Assisted Resolution**: An optional LLM module (Groq / OpenAI) generates advisory recommendations and rationale for pending escalations. Recommendations are strictly advisory and require human confirmation.

## 2. Autonomy Boundary & Escalation Policy
The autonomy line is governed by a core principle: **The agent acts autonomously when there is one clear, unambiguous interpretation; it escalates whenever action would require guessing on the client's behalf.**

### Autonomous Actions (Handled Alone)
- **Column Mapping**: Auto-mapped when fuzzy string score exceeds threshold (78%) and leads the second candidate by a defensible gap (10%+).
- **Format Normalization**: Trimming whitespace, title-casing names, converting dates to ISO format (`YYYY-MM-DD`), and fixing trivial syntax errors (e.g., doubled `@` symbols in emails).
- **Deterministic Merging**: Merging duplicate employee records across sources when fields agree or complement missing values without conflict.

### Escalations (Surfaced for Human Review)
- **Mapping Ambiguity**: A column name scores closely across multiple target fields (e.g., `Contact` matching both `email` and `manager_email`).
- **Uncertain Formatting**: Complex multi-token names where surname placement is ambiguous, or severely malformed emails missing domain TLDs.
- **Data Disagreements**: Conflicting field values for the same record across different source files (e.g., differing job titles across systems).
- **Validation Blocks**: Missing required schema fields that remain empty after cleaning and reconciliation passes.

## 3. Mock System Integration & Auditability
- **Transactional Push & Retry**: Pushes ready records to a mock API with simulated network/validation failures and built-in per-record retries.
- **Batch Rollback**: Tracks batch identifiers to allow atomic rollbacks of pushed records if needed.
- **Audit Logging**: Every automated transformation, human decision, and API push is recorded in an immutable audit log with timestamped before/after values.

## 4. What To Build Next
1. **Adaptive Learning Memory**: Store resolved human mapping and cleanup choices per client tenant to dynamically increase confidence scores on future runs.
2. **Asynchronous LLM Suggestions**: Stream LLM escalation advice in the background to prevent blocking pipeline execution.
3. **Field-Level Rollbacks**: Enable targeted single-record rollbacks rather than full batch rollbacks.
4. **Schema Drift Detection**: Automatically alert operators when source export structures shift between periodic migration runs.
