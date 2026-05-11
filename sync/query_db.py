#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
query_db.py  —  Unified search tool for the ServiceNow Copilot agent.
======================================================================

Combines THREE search engines:

  ENGINE 1 — SQLite structured query  (instant, <1 ms)
      Handles:
        • Exact record number  (INC0012345)
        • Structured fields    (priority, state, category, table)
        • Date ranges          (--days N  or  --from/--to dates)
        • Assignment group / CI / assigned_to
        • Keyword intersection (2+ keywords both present in record)

  ENGINE 2 — SQLite full-text search  (fast, ~5 ms)
      Handles:
        • Natural-language phrase search across short_description,
          description, close_notes, keywords_text
        • Partial-match fallback when SQL engine returns nothing

  ENGINE 3 — FAISS vector search      (~500 ms, semantic)
      Handles:
        • Semantic / conceptual queries
        • When the user describes a symptom without exact keywords

SEARCH STRATEGY (auto-selected):
  1. If query looks like INC/CHG/PRB/RITM/TASK + digits → SQL Engine 1
  2. If --filter or --days or --from/--to given → SQL Engine 1 first
  3. SQL Engine 1 (keyword intersection) → if ≥1 result, stop
  4. SQL Engine 2 (FTS)                 → if ≥1 result, stop
  5. FAISS vector search                → always run as semantic fallback

USAGE
-----
  python sync/query_db.py "<query>"  [options]

OPTIONS
-------
  --top_k N          Max results to return  (default: 10)
  --min_score F      Min vector similarity  (default: 0.30)
  --filter KEY=VAL   Filter: table=incident | priority=1 | state=open
                              category=network | assignment_group=DevOps
  --days N           Only records opened/updated in last N days
  --from YYYY-MM-DD  Only records opened on or after this date
  --to   YYYY-MM-DD  Only records opened on or before this date
  --section NAME     Section filter for vector: resolution|description|keywords
  --engine sql|fts|vector   Force a specific engine (default: auto)
  --json             Machine-readable JSON output
  --debug            Show raw distances and SQL queries

EXIT CODES
----------
  0 = results found
  1 = no results
  2 = system error
"""

import os
import re
import sys
import json
import sqlite3
import argparse
from pathlib import Path

try:
    from langchain_community.vectorstores import FAISS
    from langchain_huggingface import HuggingFaceEmbeddings
    from dotenv import load_dotenv
except ImportError as e:
    print(f"[SYSTEM ERROR] Missing dependency: {e}")
    print("Run: pip install langchain-community langchain-huggingface "
          "sentence-transformers faiss-cpu python-dotenv")
    sys.exit(2)

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

VECTORDB_DIR  = Path("vectordb")
DB_PATH       = VECTORDB_DIR / "servicenow.db"
KEYWORD_INDEX = VECTORDB_DIR / "keyword_index.json"
HF_CACHE_DIR  = Path(".hf_cache")
HF_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

SECTION_ALIASES = {
    "keywords":    ["search keywords", "keywords"],
    "resolution":  ["resolution notes", "resolution", "close notes", "close_notes"],
    "description": ["description", "short description"],
    "summary":     ["summary"],
    "all_fields":  ["all fields"],
    "plans":       ["implementation plan", "backout plan", "test plan"],
}

# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description="Search the ServiceNow internal database (SQL + FTS + FAISS)"
)
parser.add_argument("query",       type=str,  help="Search query or record number")
parser.add_argument("--top_k",     type=int,  default=10)
parser.add_argument("--min_score", type=float,default=0.30)
parser.add_argument("--filter",    type=str,  default=None,
                    help="KEY=VAL filter e.g. table=incident or priority=1")
parser.add_argument("--days",      type=int,  default=None,
                    help="Restrict to records opened/updated in last N days")
parser.add_argument("--from",      dest="date_from", type=str, default=None,
                    help="Records opened on or after YYYY-MM-DD")
parser.add_argument("--to",        dest="date_to",   type=str, default=None,
                    help="Records opened on or before YYYY-MM-DD")
parser.add_argument("--section",   type=str, default=None)
parser.add_argument("--engine",    type=str, default="auto",
                    choices=["auto","sql","fts","vector"])
parser.add_argument("--json",      action="store_true")
parser.add_argument("--debug",     action="store_true")
args = parser.parse_args()

# ─────────────────────────────────────────────────────────────────────────────
# Parse --filter
# ─────────────────────────────────────────────────────────────────────────────

meta_filter: dict[str, str] = {}
if args.filter:
    try:
        key, val       = args.filter.split("=", 1)
        meta_filter[key.strip().lower()] = val.strip()
    except ValueError:
        print(f"[WARN] Invalid --filter '{args.filter}' — ignored.")

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def conf_label(score: float) -> str:
    if score >= 0.85: return "[OK] HIGH (95%)"
    if score >= 0.70: return "[OK] GOOD (80%)"
    if score >= 0.55: return "[!]  MODERATE (65%)"
    if score >= 0.40: return "[!]  WEAK (50%)"
    if score >= 0.25: return "[X]  VERY WEAK (30%)"
    return "[X]  BELOW THRESHOLD"


def detect_record_number(query: str):
    """Return e.g. 'INC0012345' if query contains a ServiceNow record number."""
    m = re.search(r'\b(INC|CHG|PRB|RITM|TASK|KB)\d+\b', query, re.IGNORECASE)
    return m.group(0).upper() if m else None


def row_to_dict(cursor, row) -> dict:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

# ─────────────────────────────────────────────────────────────────────────────
# ENGINE 1 — SQLite structured query
# ─────────────────────────────────────────────────────────────────────────────

def sql_search(query_str: str) -> list[dict]:
    """
    Run a structured SQL query against servicenow.db.
    Returns list of row dicts (no duplicates, ordered by opened_date DESC).

    Strategies (tried in order, results merged):
      1. Exact record_id match           (INC/CHG/PRB number)
      2. Filter-field WHERE clauses      (table, priority, state, category,
                                          assignment_group, cmdb_ci)
      3. Date range filter               (opened_date between --from and --to)
      4. Keyword intersection            (records that have ALL query keywords
                                          in record_keywords table)
      5. LIKE search on short_description
    """
    if not DB_PATH.exists():
        return []

    conn   = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur    = conn.cursor()

    results: list[dict] = []
    seen:    set[str]   = set()

    def add_rows(rows):
        for row in rows:
            d = dict(row)
            if d["sys_id"] not in seen:
                seen.add(d["sys_id"])
                results.append(d)

    # ── Build base WHERE clause from filters ──────────────────────────────
    where_parts: list[str] = []
    bind_vals:   list      = []

    filter_col_map = {
        "table":            "table_name",
        "table_name":       "table_name",
        "priority":         "priority",
        "state":            "state",
        "category":         "category",
        "subcategory":      "subcategory",
        "assignment_group": "assignment_group",
        "cmdb_ci":          "cmdb_ci",
        "assigned_to":      "assigned_to",
        "severity":         "severity",
        "urgency":          "urgency",
        "impact":           "impact",
        "change_type":      "change_type",
        "phase":            "phase",
        "risk":             "risk",
    }
    for fk, fv in meta_filter.items():
        col = filter_col_map.get(fk)
        if col:
            where_parts.append(f"LOWER({col}) = LOWER(?)")
            bind_vals.append(fv)

    if args.days:
        where_parts.append(
            "opened_date >= date('now', ?)"
        )
        bind_vals.append(f"-{args.days} days")

    if args.date_from:
        where_parts.append("opened_date >= ?")
        bind_vals.append(args.date_from)

    if args.date_to:
        where_parts.append("opened_date <= ?")
        bind_vals.append(args.date_to)

    base_where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    # ── Strategy 1: Exact record number ──────────────────────────────────
    rec_num = detect_record_number(query_str)
    if rec_num:
        sql = f"""
            SELECT * FROM records
            {base_where}
            {'AND' if base_where else 'WHERE'} UPPER(record_id) = ?
            LIMIT ?
        """
        if args.debug:
            print(f"[DEBUG SQL-1] {sql.strip()} | params: {bind_vals + [rec_num, args.top_k]}")
        cur.execute(sql, bind_vals + [rec_num, args.top_k])
        add_rows(cur.fetchall())

    # ── Strategy 2: Filter-only query (when only filters given) ──────────
    if base_where and not query_str.strip():
        sql = f"""
            SELECT * FROM records {base_where}
            ORDER BY opened_date DESC LIMIT ?
        """
        cur.execute(sql, bind_vals + [args.top_k])
        add_rows(cur.fetchall())

    # ── Strategy 3: Keyword intersection ─────────────────────────────────
    q_lower  = query_str.lower()
    tokens   = [t for t in re.split(r'[\s\-_/]+', q_lower) if len(t) >= 3]
    # Remove very common English filler
    _FILLER = {"that","this","with","from","have","will","were","been",
               "your","they","when","what","which","also","more","than",
               "then","into","some","none","about","after","before",
               "would","could","should","shall","while","where"}
    tokens = [t for t in tokens if t not in _FILLER]

    if tokens:
        # Find sys_ids that have ≥2 matching keywords (reduces noise)
        threshold = max(1, min(2, len(tokens)))
        placeholders = ",".join("?" * len(tokens))
        kw_sql = f"""
            SELECT r.* FROM records r
            INNER JOIN (
                SELECT sys_id, COUNT(DISTINCT keyword) AS kw_cnt
                FROM record_keywords
                WHERE keyword IN ({placeholders})
                GROUP BY sys_id
                HAVING kw_cnt >= {threshold}
            ) kw ON r.sys_id = kw.sys_id
            {('AND'.join(['', base_where.replace('WHERE','')]) if base_where else '')}
            ORDER BY kw.kw_cnt DESC, r.opened_date DESC
            LIMIT ?
        """
        # Rebuild a cleaner version of this query
        if base_where:
            filter_clause = " AND " + " AND ".join(where_parts)
        else:
            filter_clause = ""
        kw_sql2 = f"""
            SELECT r.* FROM records r
            INNER JOIN (
                SELECT sys_id, COUNT(DISTINCT keyword) AS kw_cnt
                FROM record_keywords
                WHERE keyword IN ({placeholders})
                GROUP BY sys_id
                HAVING kw_cnt >= {threshold}
            ) kw ON r.sys_id = kw.sys_id
            WHERE 1=1 {filter_clause}
            ORDER BY kw.kw_cnt DESC, r.opened_date DESC
            LIMIT ?
        """
        if args.debug:
            print(f"[DEBUG SQL-3] tokens={tokens} threshold={threshold}")
        cur.execute(kw_sql2, tokens + bind_vals + [args.top_k])
        add_rows(cur.fetchall())

    # ── Strategy 4: short_description LIKE ───────────────────────────────
    if tokens and len(results) < args.top_k:
        for tok in tokens[:3]:   # top 3 most relevant tokens
            like_sql = f"""
                SELECT * FROM records
                WHERE short_description LIKE ?
                {'AND ' + ' AND '.join(where_parts) if where_parts else ''}
                ORDER BY opened_date DESC LIMIT ?
            """
            cur.execute(like_sql,
                        [f"%{tok}%"] + bind_vals + [args.top_k - len(results)])
            add_rows(cur.fetchall())
            if len(results) >= args.top_k:
                break

    conn.close()
    return results[:args.top_k]

# ─────────────────────────────────────────────────────────────────────────────
# ENGINE 2 — SQLite Full-Text Search (FTS5)
# ─────────────────────────────────────────────────────────────────────────────

def fts_search(query_str: str) -> list[dict]:
    """
    Run FTS5 MATCH query across short_description, description,
    close_notes, keywords_text.
    Returns list of row dicts.
    """
    if not DB_PATH.exists() or not query_str.strip():
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()

    results: list[dict] = []
    seen:    set[str]   = set()

    # Build FTS query — use OR between tokens for broader match
    tokens = [t for t in re.split(r'[\s\-_/]+', query_str.lower())
              if len(t) >= 3]
    if not tokens:
        conn.close()
        return []

    fts_query = " OR ".join(tokens)

    # Apply table/priority/state filters if given
    where_parts: list[str] = []
    bind_vals:   list      = []
    filter_col_map = {
        "table":            "r.table_name",
        "table_name":       "r.table_name",
        "priority":         "r.priority",
        "state":            "r.state",
        "category":         "r.category",
        "assignment_group": "r.assignment_group",
    }
    for fk, fv in meta_filter.items():
        col = filter_col_map.get(fk)
        if col:
            where_parts.append(f"LOWER({col}) = LOWER(?)")
            bind_vals.append(fv)
    if args.days:
        where_parts.append("r.opened_date >= date('now', ?)")
        bind_vals.append(f"-{args.days} days")

    filter_clause = ("AND " + " AND ".join(where_parts)) if where_parts else ""

    fts_sql = f"""
        SELECT r.* FROM records r
        JOIN records_fts f ON r.rowid = f.rowid
        WHERE records_fts MATCH ?
        {filter_clause}
        ORDER BY rank
        LIMIT ?
    """
    if args.debug:
        print(f"[DEBUG FTS] query='{fts_query}'  filter={filter_clause}")

    try:
        cur.execute(fts_sql, [fts_query] + bind_vals + [args.top_k])
        for row in cur.fetchall():
            d = dict(row)
            if d["sys_id"] not in seen:
                seen.add(d["sys_id"])
                results.append(d)
    except sqlite3.OperationalError as e:
        if args.debug:
            print(f"[DEBUG FTS error] {e}")
        # FTS5 syntax error — try simpler query with first token only
        try:
            cur.execute(fts_sql,
                        [tokens[0]] + bind_vals + [args.top_k])
            for row in cur.fetchall():
                d = dict(row)
                if d["sys_id"] not in seen:
                    seen.add(d["sys_id"])
                    results.append(d)
        except Exception:
            pass

    conn.close()
    return results[:args.top_k]

# ─────────────────────────────────────────────────────────────────────────────
# ENGINE 3 — FAISS vector search
# ─────────────────────────────────────────────────────────────────────────────

def load_vector_db():
    """Load FAISS index. Returns (vector_db, embeddings) or (None, None)."""
    if not (VECTORDB_DIR / "index.faiss").exists():
        return None, None
    try:
        embeddings = HuggingFaceEmbeddings(
            model_name    = HF_MODEL_NAME,
            cache_folder  = str(HF_CACHE_DIR),
            model_kwargs  = {"device": "cpu"},
            encode_kwargs = {"normalize_embeddings": True},
        )
        vector_db = FAISS.load_local(
            str(VECTORDB_DIR),
            embeddings,
            allow_dangerous_deserialization=True,
        )
        return vector_db, embeddings
    except Exception as e:
        print(f"[WARN] FAISS load failed: {e}")
        return None, None


def detect_and_norm(vector_db, dist: float) -> float:
    try:
        idx_type = type(vector_db.index).__name__
    except Exception:
        idx_type = ""
    if "IP" in idx_type or "InnerProduct" in idx_type:
        score = float(dist)
    else:
        score = 1.0 - float(dist) / 2.0
    return max(0.0, min(1.0, score))


def matches_section(section_meta: str, requested: str) -> bool:
    if not requested:
        return True
    aliases = SECTION_ALIASES.get(requested.lower(), [requested.lower()])
    s = (section_meta or "").lower()
    return any(alias in s for alias in aliases)


def vector_search(query_str: str, vector_db) -> list[tuple]:
    """
    Returns list of (doc, score) tuples sorted by score DESC.
    """
    if vector_db is None or not query_str.strip():
        return []

    fetch_k = args.top_k * 3 if args.section else args.top_k * 2

    try:
        if meta_filter and "table" in meta_filter:
            raw = vector_db.similarity_search_with_score(
                query_str, k=fetch_k, filter={"table": meta_filter["table"]}
            )
        elif meta_filter and "table_name" in meta_filter:
            raw = vector_db.similarity_search_with_score(
                query_str, k=fetch_k, filter={"table": meta_filter["table_name"]}
            )
        else:
            raw = vector_db.similarity_search_with_score(query_str, k=fetch_k)
    except Exception as e:
        print(f"[WARN] Vector search error: {e}")
        return []

    if args.debug and raw:
        try:
            idx_type = type(vector_db.index).__name__
        except Exception:
            idx_type = "unknown"
        print(f"[DEBUG VECTOR] Index type: {idx_type}")
        for doc, dist in raw[:5]:
            score = detect_and_norm(vector_db, dist)
            print(f"  dist={dist:.4f}  score={score:.4f}  "
                  f"record={doc.metadata.get('record_id','?')}  "
                  f"section={doc.metadata.get('section','?')}")

    results = []
    seen    = set()
    for doc, dist in raw:
        score   = detect_and_norm(vector_db, dist)
        section = doc.metadata.get("section", "")
        rec_id  = doc.metadata.get("record_id", "")
        if score < args.min_score:
            continue
        if args.section and not matches_section(section, args.section):
            continue
        key = f"{rec_id}::{section}"
        if key in seen:
            continue
        seen.add(key)
        results.append((doc, score))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:args.top_k]

# ─────────────────────────────────────────────────────────────────────────────
# Output formatters
# ─────────────────────────────────────────────────────────────────────────────

def format_sql_row(row: dict, rank: int, source: str = "SQL") -> str:
    lines = []
    W = 68
    score_str = "(exact match)" if source == "SQL" else "(FTS match)"
    lines.append(f"\n  -- RESULT {rank} --- [{source}] {score_str}")
    lines.append(f"  Record ID    : {row.get('record_id', 'N/A')}")
    lines.append(f"  Sys ID       : {row.get('sys_id', 'N/A')}")
    lines.append(f"  Table        : {row.get('table_name', 'N/A')}")
    sd = row.get('short_description', '')
    if sd:
        lines.append(f"  Short Desc   : {sd[:80]}")
    lines.append(f"  State        : {row.get('state', 'N/A')}")
    lines.append(f"  Priority     : {row.get('priority', 'N/A')}")
    lines.append(f"  Category     : {row.get('category', 'N/A')}")
    subcat = row.get('subcategory', '')
    if subcat:
        lines.append(f"  Subcategory  : {subcat}")
    tbl = row.get('table_name', '')
    if tbl == 'incident':
        lines.append(f"  Severity     : {row.get('severity', 'N/A')}")
        lines.append(f"  Urgency      : {row.get('urgency',  'N/A')}")
        lines.append(f"  Impact       : {row.get('impact',   'N/A')}")
    if tbl == 'change_request':
        lines.append(f"  CHG Type     : {row.get('change_type', 'N/A')}")
        lines.append(f"  Phase        : {row.get('phase',       'N/A')}")
        lines.append(f"  Risk         : {row.get('risk',        'N/A')}")
    ci = row.get('cmdb_ci', '')
    ag = row.get('assignment_group', '')
    if ci: lines.append(f"  CI / Asset   : {ci}")
    if ag: lines.append(f"  Assign Group : {ag}")
    lines.append(f"  Opened       : {row.get('opened_at', 'N/A')}")
    lines.append(f"  Updated      : {row.get('updated_at', 'N/A')}")
    lines.append(f"  File         : {row.get('file_path', 'N/A')}")
    notes = row.get('close_notes', '') or ''
    if notes.strip():
        lines.append("")
        lines.append("  Resolution Notes:")
        for ln in notes.strip().splitlines():
            lines.append(f"    {ln}")
    lines.append("")
    lines.append("  " + "-" * (W - 2))
    return "\n".join(lines)


def sql_row_to_json(row: dict, rank: int, source: str = "SQL") -> dict:
    return {
        "rank":              rank,
        "source":            source,
        "score":             1.0,
        "confidence":        "[OK] SQL EXACT",
        "record_id":         row.get("record_id",         ""),
        "sys_id":            row.get("sys_id",            ""),
        "table":             row.get("table_name",        ""),
        "short_description": row.get("short_description", ""),
        "state":             row.get("state",             ""),
        "priority":          row.get("priority",          ""),
        "category":          row.get("category",          ""),
        "subcategory":       row.get("subcategory",       ""),
        "severity":          row.get("severity",          ""),
        "urgency":           row.get("urgency",           ""),
        "impact":            row.get("impact",            ""),
        "cmdb_ci":           row.get("cmdb_ci",           ""),
        "assignment_group":  row.get("assignment_group",  ""),
        "opened_at":         row.get("opened_at",         ""),
        "updated_at":        row.get("updated_at",        ""),
        "resolved_at":       row.get("resolved_at",       ""),
        "change_type":       row.get("change_type",       ""),
        "phase":             row.get("phase",             ""),
        "risk":              row.get("risk",              ""),
        "file":              row.get("file_path",         ""),
        "close_notes":       row.get("close_notes",       "")[:800],
    }


def vector_result_to_json(doc, score: float, rank: int) -> dict:
    m = doc.metadata
    return {
        "rank":              rank,
        "source":            "FAISS",
        "score":             round(score, 4),
        "confidence":        conf_label(score),
        "record_id":         m.get("record_id",         ""),
        "sys_id":            m.get("sys_id",            ""),
        "table":             m.get("table",             ""),
        "section":           m.get("section",           ""),
        "short_description": m.get("short_description", ""),
        "state":             m.get("state",             ""),
        "priority":          m.get("priority",          ""),
        "category":          m.get("category",          ""),
        "subcategory":       m.get("subcategory",       ""),
        "severity":          m.get("severity",          ""),
        "urgency":           m.get("urgency",           ""),
        "impact":            m.get("impact",            ""),
        "cmdb_ci":           m.get("cmdb_ci",           ""),
        "assignment_group":  m.get("assignment_group",  ""),
        "opened_at":         m.get("opened_at",         ""),
        "updated_at":        m.get("updated_at",        ""),
        "change_type":       m.get("change_type",       ""),
        "phase":             m.get("phase",             ""),
        "risk":              m.get("risk",              ""),
        "file":              m.get("file",              ""),
        "content":           doc.page_content[:800],
    }

# ─────────────────────────────────────────────────────────────────────────────
# Main search orchestration
# ─────────────────────────────────────────────────────────────────────────────

def main():
    query_str  = args.query
    engine     = args.engine

    sql_results    = []
    fts_results    = []
    vector_results = []
    engines_run    = []

    # ── Always check DB exists ─────────────────────────────────────────────
    db_available     = DB_PATH.exists()
    vector_available = (VECTORDB_DIR / "index.faiss").exists()

    if not db_available and not vector_available:
        print("[SYSTEM ERROR] Neither SQLite DB nor FAISS index found.")
        print("  Run:  python sync/servicenow_syncv4.py          (sync + build DB)")
        print("  Then: python sync/embedding_builder_githubv31.py (build FAISS)")
        sys.exit(2)

    # ── Auto strategy ─────────────────────────────────────────────────────
    if engine == "auto":
        # SQL always first (instant)
        if db_available:
            sql_results = sql_search(query_str)
            engines_run.append("SQL")

        # FTS if SQL didn't find enough
        if db_available and len(sql_results) < args.top_k:
            fts_results = fts_search(query_str)
            engines_run.append("FTS")

        # FAISS — run when query is not a bare record number lookup
        rec_num = detect_record_number(query_str)
        run_vector = (
            vector_available
            and (rec_num is None or len(sql_results) == 0)
        )
        if run_vector:
            vector_db, _ = load_vector_db()
            vector_results = vector_search(query_str, vector_db)
            engines_run.append("FAISS")

    elif engine == "sql":
        sql_results = sql_search(query_str)
        engines_run.append("SQL")
    elif engine == "fts":
        fts_results = fts_search(query_str)
        engines_run.append("FTS")
    elif engine == "vector":
        vector_db, _ = load_vector_db()
        vector_results = vector_search(query_str, vector_db)
        engines_run.append("FAISS")

    # ── Merge & de-duplicate results ──────────────────────────────────────
    # Priority: SQL (exact structured) > FTS > FAISS
    all_record_ids: set[str] = set()
    merged_sql:    list[dict]  = []
    merged_fts:    list[dict]  = []
    merged_vector: list[tuple] = []

    for row in sql_results:
        rid = row.get("record_id", row.get("sys_id", ""))
        if rid not in all_record_ids:
            all_record_ids.add(rid)
            merged_sql.append(row)

    for row in fts_results:
        rid = row.get("record_id", row.get("sys_id", ""))
        if rid not in all_record_ids:
            all_record_ids.add(rid)
            merged_fts.append(row)

    for doc, score in vector_results:
        rid = doc.metadata.get("record_id", "")
        if rid not in all_record_ids:
            all_record_ids.add(rid)
            merged_vector.append((doc, score))

    total = len(merged_sql) + len(merged_fts) + len(merged_vector)

    # ── No results ────────────────────────────────────────────────────────
    if total == 0:
        if args.json:
            print(json.dumps({
                "query": query_str, "result_count": 0,
                "engines": engines_run, "results": [],
            }, indent=2))
        else:
            print(f"\n[NO RESULTS] '{query_str}'")
            print(f"  Engines tried : {', '.join(engines_run)}")
            if meta_filter:
                print(f"  Filter        : {meta_filter}")
            print("  Suggestions:")
            print("    1. Lower --min_score to 0.20")
            print("    2. Remove --filter to search all tables")
            print("    3. Check DB: python sync/servicenow_syncv4.py")
            print("    4. Rebuild FAISS: python sync/embedding_builder_githubv31.py")
        sys.exit(1)

    # ── JSON output ───────────────────────────────────────────────────────
    if args.json:
        rank = 1
        out_results = []
        for row in merged_sql:
            out_results.append(sql_row_to_json(row, rank, "SQL"))
            rank += 1
        for row in merged_fts:
            out_results.append(sql_row_to_json(row, rank, "FTS"))
            rank += 1
        for doc, score in merged_vector:
            out_results.append(vector_result_to_json(doc, score, rank))
            rank += 1

        output = {
            "query":        query_str,
            "filter":       meta_filter or None,
            "section":      args.section,
            "engines_run":  engines_run,
            "result_count": total,
            "sql_count":    len(merged_sql),
            "fts_count":    len(merged_fts),
            "vector_count": len(merged_vector),
            "results":      out_results,
        }
        print(json.dumps(output, indent=2))
        sys.exit(0)

    # ── Human-readable output ─────────────────────────────────────────────
    W = 68
    print()
    print("=" * W)
    print("  INTERNAL DB SEARCH RESULTS")
    print(f"  Query        : {query_str}")
    if meta_filter:
        print(f"  Filter       : {meta_filter}")
    if args.section:
        print(f"  Section      : {args.section}")
    print(f"  Engines      : {', '.join(engines_run)}")
    print(f"  Results      : {len(merged_sql)} SQL  |  "
          f"{len(merged_fts)} FTS  |  "
          f"{len(merged_vector)} vector")
    print("=" * W)

    rank = 1
    for row in merged_sql:
        print(format_sql_row(row, rank, "SQL"))
        rank += 1
    for row in merged_fts:
        print(format_sql_row(row, rank, "FTS"))
        rank += 1

    for doc, score in merged_vector:
        m = doc.metadata
        print(f"\n  -- RESULT {rank} --- {conf_label(score)}  (score {score:.3f}) [FAISS]")
        print(f"  Record ID    : {m.get('record_id', 'N/A')}")
        print(f"  Sys ID       : {m.get('sys_id',    'N/A')}")
        print(f"  Table        : {m.get('table',     'N/A')}")
        print(f"  Section      : {m.get('section',   'N/A')}")
        sd = m.get('short_description', '')
        if sd:
            print(f"  Short Desc   : {sd[:80]}")
        print(f"  State        : {m.get('state',     'N/A')}")
        print(f"  Priority     : {m.get('priority',  'N/A')}")
        print(f"  Category     : {m.get('category',  'N/A')}")
        subcat = m.get('subcategory', '')
        if subcat:
            print(f"  Subcategory  : {subcat}")
        if m.get("table") == "incident":
            print(f"  Severity     : {m.get('severity', 'N/A')}")
            print(f"  Urgency      : {m.get('urgency',  'N/A')}")
            print(f"  Impact       : {m.get('impact',   'N/A')}")
        if m.get("table") == "change_request":
            print(f"  CHG Type     : {m.get('change_type', 'N/A')}")
            print(f"  Phase        : {m.get('phase',       'N/A')}")
            print(f"  Risk         : {m.get('risk',        'N/A')}")
        ci = m.get('cmdb_ci', '')
        ag = m.get('assignment_group', '')
        if ci: print(f"  CI / Asset   : {ci}")
        if ag: print(f"  Assign Group : {ag}")
        print(f"  Opened       : {m.get('opened_at',  'N/A')}")
        print(f"  Updated      : {m.get('updated_at', 'N/A')}")
        print(f"  File         : {m.get('file', 'N/A')}")
        print()
        content   = doc.page_content.strip()
        sec_lower = (m.get("section") or "").lower()
        is_res    = any(k in sec_lower for k in ["resolution", "close", "root cause"])
        preview   = len(content) if is_res else 700
        for line in content[:preview].splitlines():
            print(f"  {line}")
        if len(content) > preview:
            print(f"  ... [{len(content) - preview} more chars in {m.get('file')}]")
        print()
        print("  " + "-" * (W - 2))
        rank += 1

    print("=" * W)
    print(f"  DB: {DB_PATH}  |  FAISS: {VECTORDB_DIR}/  |  min_score: {args.min_score}")
    print("=" * W)
    sys.exit(0)


if __name__ == "__main__":
    main()
