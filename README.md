# ServiceNow Copilot Agent v4

Enterprise DevOps AI assistant with **internal-first** search using
**SQLite + FAISS** — fast structured queries and semantic vector search.

---

## What Changed (v3 → v4)

| Component | v3 | v4 |
|-----------|----|----|
| **Database** | FAISS + `keyword_index.json` only | **SQLite** (`servicenow.db`) + FAISS + keyword index |
| **Sync script** | `servicenow_syncv3.py` — MD files only | `servicenow_syncv4.py` — MD files + **SQLite** |
| **Query script** | `query_vecordb.py` — FAISS + JSON scan | `query_db.py` — **SQL → FTS → FAISS** auto-strategy |
| **Structured queries** | Not possible without FAISS model | `query_structured.py` — **instant SQL, no ML needed** |
| **Exact record lookup** | FAISS vector search (~500 ms) | SQL `WHERE record_id = ?` (**<1 ms**) |
| **"All P1 incidents"** | Not supported | `--priority 1` SQL filter |
| **Date-range queries** | Not supported | `--days 7` or `--from/--to` |
| **Count / aggregate** | Not supported | `--aggregate priority` or `--stats` |

---

## Architecture

```
ServiceNow API
     │
     ▼
servicenow_syncv4.py
     ├── knowledge/<table>/<record>.md   (Markdown for FAISS embedding)
     └── vectordb/servicenow.db          (SQLite — instant structured queries)
              ├── records                 (one row per record, all fields)
              ├── records_fts             (FTS5 full-text index)
              └── record_keywords         (normalised keyword rows)

embedding_builder_githubv31.py
     └── vectordb/index.faiss + keyword_index.json
```

---

## SQLite Schema (key tables)

```sql
records(
    sys_id, record_id, table_name,
    short_description, state, priority, category, subcategory,
    cmdb_ci, assignment_group, assigned_to,
    severity, urgency, impact,          -- incident
    change_type, phase, risk,           -- change_request
    opened_date,   -- ISO YYYY-MM-DD for SQL date comparison
    resolved_date, closed_date, updated_date,
    opened_at, resolved_at, ...,        -- original display strings
    description, close_notes,
    keywords_text, file_path, synced_at
)

record_keywords(sys_id, keyword)        -- for keyword intersection queries
records_fts USING fts5(...)             -- full-text search
```

---

## Search Tools

### query_structured.py — SQL only, instant, no ML

```bash
python sync/query_structured.py --table incident --priority 1 --state open
python sync/query_structured.py --table incident --days 7
python sync/query_structured.py --table incident --aggregate priority
python sync/query_structured.py --table incident --stats
python sync/query_structured.py --table change_request --ci "prod-db-01"
python sync/query_structured.py --table incident --group "Platform Team"
python sync/query_structured.py --table incident --from 2024-01-01 --to 2024-03-31
```

### query_db.py — SQL + FTS + FAISS auto strategy

```bash
python sync/query_db.py "INC0012345"                              # SQL <1ms
python sync/query_db.py "prometheus alertmanager not sending alerts"  # semantic
python sync/query_db.py "terraform state lock" --section resolution
python sync/query_db.py "network outage" --filter table=incident --days 30
python sync/query_db.py "kubernetes crashloop" --engine vector    # force FAISS
python sync/query_db.py "sonarqube quality gate" --json
```

---

## How the Agent Chooses a Search Engine

```
User Query
    ├─► Structured (priority/state/date/count)?
    │       └─► query_structured.py   (SQL, instant)
    └─► Semantic / natural language?
            ├── SQL keyword intersection  (<1 ms)
            ├── FTS5 full-text search     (~5 ms)
            └── FAISS vector search       (~500 ms)
```

---

## File Structure

```
.github/
  agents/servicenow-copilot.agentv4.md   Agent instructions v4
  workflows/sync-servicenow-v4.yml        Sync 3x daily + auto-rebuild

sync/
  servicenow_syncv4.py                    Sync → MD files + SQLite DB
  embedding_builder_githubv31.py          Build FAISS + keyword index
  query_db.py                             Unified SQL+FTS+FAISS search
  query_structured.py                     SQL-only structured queries
  internet_search.py                      Last-resort internet fallback

vectordb/
  servicenow.db                           SQLite database (NEW in v4)
  index.faiss                             FAISS semantic index
  keyword_index.json                      Keyword metadata cache

knowledge/<table>/<record>.md             Markdown backup files
```

---

## Required Secrets

| Secret | Purpose |
|--------|---------|
| `SNOW_INSTANCE` | ServiceNow instance URL or name |
| `SNOW_USER` | ServiceNow API username |
| `SNOW_PASSWORD` | ServiceNow API password |
| `GH_PAT` | GitHub PAT for workflow pushes |

---

## Local Setup

```bash
export SNOW_INSTANCE=your-instance
export SNOW_USER=your-user
export SNOW_PASSWORD=your-password

# 1. Sync from ServiceNow (MD + SQLite)
python sync/servicenow_syncv4.py

# 2. Build FAISS
python sync/embedding_builder_githubv31.py

# 3. Test
python sync/query_structured.py --table incident --stats
python sync/query_db.py "prometheus alertmanager not sending alerts"
```
