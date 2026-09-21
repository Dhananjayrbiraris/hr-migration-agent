# Client Data Migration Agent

An automated data ingestion and migration system built with FastAPI and Pandas. Ingests multi-source HR and CRM exports, maps schema fields, normalizes data, handles record deduplication, and pushes validated records to target APIs with human-in-the-loop escalation handling.

## System Architecture

- **Database**: **Supabase PostgreSQL Cloud Database** connected via standard PostgreSQL connection string (`DATABASE_URL`) using **SQLAlchemy** and **psycopg2**.
- **Backend**: Python 3.10+, FastAPI, Pandas, RapidFuzz, DateUtil, SQLAlchemy, Psycopg2.
- **LLM Integration**: Optional Groq (`llama-3.3-70b-versatile`) or OpenAI (`gpt-4o-mini`) integration for escalation resolution suggestions.
- **Frontend**: Lightweight single-page web console for file uploads, real-time event tracing, escalation reviews, and audit trail inspection.

## Project Structure

```text
backend/
  main.py       FastAPI application and REST endpoints
  agent.py      Data processing pipeline and escalation engine
  schema.py     Target employee schema definition
  mock_api.py   Target API integration simulator
  llm.py        AI advisory service (Groq / OpenAI)
frontend/
  index.html    Console UI
my_upload_files/
  hr_employees.csv   Sample CSV file for testing
  crm_contacts.csv   Sample CSV file for testing
pyproject.toml   Dependencies configuration
run.ps1          PowerShell runner script for Windows
run.sh           Shell runner script
```

## Setup & Running

### Requirements
- Python 3.10+
- `uv` package manager (recommended) or standard `pip`

### Execution Commands

#### Using PowerShell (Windows)
```powershell
.\run.ps1
```

#### Using Python / Uvicorn Directly
```powershell
python -m uvicorn backend.main:app --reload --port 8000
```

#### Using `uv`
```powershell
uv run --project backend uvicorn backend.main:app --reload --port 8000
```

The web console will be available at `http://localhost:8000`.

## Environment Variables

Copy `.env.example` to `.env` to configure AI resolution suggestions:

```env
# Groq configuration
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=llama-3.3-70b-versatile

# OpenAI configuration
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=gpt-4o-mini
```
