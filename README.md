# HR Data Migration Agent

A human-in-the-loop data migration pipeline that reads raw HR/CRM exports, maps columns to a target schema, cleans and validates records, surfaces conflicts as escalations for human review, and pushes approved records to a target system backed by Supabase PostgreSQL.

Built with Python + Streamlit. Deployable on [Streamlit Community Cloud](https://streamlit.io/cloud).

---

## Features

- Ingests multiple CSV exports with inconsistent column names and mixed date formats
- Fuzzy column mapping with automatic confidence scoring
- Field-level cleaning: whitespace normalisation, date parsing, email validation
- Duplicate detection and cross-source record reconciliation
- Escalation queue with per-record review UI (approve / correct / reject)
- Optional AI-assisted suggestions via Groq or OpenAI
- Push to target with retry logic and batch rollback
- Full audit trail persisted to Supabase PostgreSQL

---

## Setup

**Requirements:** Python 3.10+, [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/<you>/hr-migration-agent
cd hr-migration-agent
cp .env.example .env          # fill in your values
uv sync
uv run streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501).

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | Supabase PostgreSQL connection string |
| `GROQ_API_KEY` | No | Groq API key — enables AI escalation suggestions |
| `GROQ_MODEL` | No | Groq model name (default: `llama-3.3-70b-versatile`) |
| `OPENAI_API_KEY` | No | OpenAI fallback if Groq key not set |

---

## Project Structure

```
app.py               # Streamlit entry point
backend/
  agent.py           # ETL pipeline: ingest → map → clean → reconcile → validate
  schema.py          # Target schema definition
  mock_api.py        # Target system client (backed by Supabase)
  database.py        # SQLAlchemy + psycopg2 persistence layer
  llm.py             # Groq / OpenAI integration for AI suggestions
data/
  hris_export.csv    # Demo HRIS data with intentional quality issues
  crm_export.csv     # Demo CRM data for reconciliation testing
```

---

## Deployment (Streamlit Community Cloud)

1. Push the repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app
3. Select repo, set **Main file path**: `app.py`
4. Add secrets in the dashboard:

```toml
DATABASE_URL = "postgresql://..."
GROQ_API_KEY = "gsk_..."
GROQ_MODEL = "llama-3.3-70b-versatile"
```

5. Click Deploy — live URL in ~2 minutes.
