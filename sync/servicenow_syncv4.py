#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
servicenow_syncv4.py
====================
Fetches ALL records from ServiceNow and writes:
  1. Structured Markdown files  → knowledge/<table>/<number>.md
  2. A SQLite metadata database → vectordb/servicenow.db
  3. A manifest JSON            → knowledge/_meta/manifest.json

WHY SQLite?
-----------
  The agent can now run fast structured queries BEFORE touching FAISS:
    - "show all P1 open incidents"        → SQL WHERE priority=1 AND state=open
    - "INC0012345"                        → SQL WHERE record_id='INC0012345'
    - "incidents from last 7 days"        → SQL WHERE opened_at > date('now','-7 days')
    - "Terraform failures"                → SQL WHERE keywords LIKE '%terraform%'
  SQL hits are instant (<1 ms) vs vector search (~500 ms model load + search).
  FAISS is used for semantic / natural-language queries only.

MARKDOWN FORMAT (agent-optimised)
----------------------------------
  Every .md file now has a ## Search Keywords section FIRST, containing:
    - short_description verbatim (highest signal for embedding)
    - category / subcategory / ci / team
    - up to 300 tech keywords extracted from all text fields
  This makes the first embedding chunk very dense and specific.

UNIQUE KEYS
-----------
  sys_id     = globally unique (ServiceNow primary key)
  record_id  = human-readable number (INC/CHG/PRB + digits)
  Filename   = record_id.md  (falling back to sys_id)

OUTPUT LAYOUT
-------------
  knowledge/
    incident/           INC*.md
    change_request/     CHG*.md
    problem/            PRB*.md
    kb_knowledge/       KB*.md
    sc_req_item/        RITM*.md
    sc_task/            TASK*.md
    _meta/
      manifest.json
  vectordb/
    servicenow.db       ← NEW: SQLite structured store
"""

import os
import re
import json
import time
import sqlite3
import datetime
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# ─────────────────────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────────────────────

SNOW_INSTANCE = os.getenv("SNOW_INSTANCE", "")
SNOW_USER     = os.getenv("SNOW_USER", "")
SNOW_PASSWORD = os.getenv("SNOW_PASSWORD", "")

missing = [name for name, val in [
    ("SNOW_INSTANCE", SNOW_INSTANCE),
    ("SNOW_USER",     SNOW_USER),
    ("SNOW_PASSWORD", SNOW_PASSWORD),
] if not val]
if missing:
    raise EnvironmentError(f"Missing environment variables: {missing}")

BASE_URL = (
    SNOW_INSTANCE.rstrip("/")
    if SNOW_INSTANCE.startswith("https://")
    else f"https://{SNOW_INSTANCE}.service-now.com"
)

KNOWLEDGE_DIR = Path("knowledge")
META_DIR      = KNOWLEDGE_DIR / "_meta"
VECTORDB_DIR  = Path("vectordb")

# ─────────────────────────────────────────────────────────────────────────────
# Table definitions
# ─────────────────────────────────────────────────────────────────────────────

TABLES = {
    "incident": {
        "query":     "",          # empty = ALL records
        "page_size": 1000,
        "headline_fields": [
            "number", "short_description", "description",
            "state", "priority", "severity", "urgency",
            "category", "subcategory",
            "assignment_group", "assigned_to",
            "caller_id", "opened_by", "opened_at",
            "resolved_at", "closed_at", "close_notes",
            "cmdb_ci", "impact", "active",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
    "change_request": {
        "query":     "",
        "page_size": 1000,
        "headline_fields": [
            "number", "short_description", "description",
            "state", "type", "phase", "risk", "impact",
            "priority", "category",
            "assignment_group", "assigned_to",
            "requested_by", "start_date", "end_date",
            "opened_at", "closed_at",
            "cmdb_ci", "justification", "implementation_plan",
            "backout_plan", "test_plan",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
    "problem": {
        "query":     "",
        "page_size": 500,
        "headline_fields": [
            "number", "short_description", "description",
            "state", "priority", "impact",
            "assignment_group", "assigned_to",
            "opened_at", "resolved_at",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
    "kb_knowledge": {
        "query":     "workflow_state=published",
        "page_size": 500,
        "headline_fields": [
            "number", "short_description", "text",
            "category", "kb_category", "author",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
    "sc_req_item": {
        "query":     "",
        "page_size": 500,
        "headline_fields": [
            "number", "short_description", "description",
            "state", "stage", "priority",
            "cat_item", "request", "assigned_to",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
    "sc_task": {
        "query":     "",
        "page_size": 500,
        "headline_fields": [
            "number", "short_description", "description",
            "state", "priority",
            "assignment_group", "assigned_to",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

AUTH          = HTTPBasicAuth(SNOW_USER, SNOW_PASSWORD)
HEADERS       = {"Accept": "application/json"}
MAX_RETRIES   = 4
RETRY_BACKOFF = 2


def _fetch_page(table, query, limit, offset):
    url    = f"{BASE_URL}/api/now/table/{table}"
    params = {
        "sysparm_limit":                  limit,
        "sysparm_offset":                 offset,
        "sysparm_display_value":          "all",
        "sysparm_exclude_reference_link": "true",
    }
    if query:
        params["sysparm_query"] = query
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, auth=AUTH, headers=HEADERS,
                             params=params, timeout=120)
            r.raise_for_status()
            return r.json().get("result", [])
        except requests.exceptions.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF ** attempt
            print(f"    attempt {attempt} failed ({exc}) – retry in {wait}s")
            time.sleep(wait)
    return []


def fetch_all(table, config):
    records, offset = [], 0
    ps    = config["page_size"]
    q     = config.get("query", "")
    label = repr(q) if q else "NONE – ALL records"
    print(f"\n[SYNC] {table}  (filter={label})")
    while True:
        print(f"   offset={offset} ...", end=" ", flush=True)
        page = _fetch_page(table, q, ps, offset)
        print(f"{len(page)} records")
        records.extend(page)
        if len(page) < ps:
            break
        offset += ps
    print(f"   [OK] {len(records)} total records")
    return records

# ─────────────────────────────────────────────────────────────────────────────
# Value extraction
# ─────────────────────────────────────────────────────────────────────────────

def _val(field_data):
    """Display value (human-readable label from reference fields)."""
    if isinstance(field_data, dict):
        dv = field_data.get("display_value", "")
        rv = field_data.get("value", "")
        return dv if dv else rv
    return str(field_data) if field_data is not None else ""


def _raw(field_data):
    """Raw value (sys_id, internal code)."""
    if isinstance(field_data, dict):
        return str(field_data.get("value", ""))
    return str(field_data) if field_data is not None else ""


def _record_id(item):
    for key in ("number", "name", "sys_id"):
        v = _val(item.get(key, ""))
        if v:
            return re.sub(r'[^\w\-]', '_', v)
    return "unknown"


def _parse_snow_date(raw_str):
    """
    Convert ServiceNow datetime string to ISO-8601 date (YYYY-MM-DD).
    ServiceNow returns e.g. '2024-03-15 10:23:44'. Returns '' on failure.
    """
    if not raw_str:
        return ""
    # Try common formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(raw_str[:19], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw_str[:10]   # best-effort: first 10 chars

# ─────────────────────────────────────────────────────────────────────────────
# Keyword extractor
# ─────────────────────────────────────────────────────────────────────────────

_FILLER = {
    "that", "this", "with", "from", "have", "will", "were", "been",
    "your", "they", "when", "what", "which", "also", "more", "than",
    "then", "into", "some", "none", "true", "false", "after", "before",
    "about", "above", "below", "there", "their", "these", "those",
    "would", "could", "should", "shall", "while", "where", "doing",
    "using", "being", "having", "making", "taking", "getting",
}


def _extract_tech_keywords(text):
    """
    Return unique tech keywords sorted by frequency desc, then length desc.
    Never strips tool names (prometheus, terraform, alertmanager, etc.).
    """
    if not text:
        return []
    words = re.findall(r'\b[A-Za-z][A-Za-z0-9_\-\.]{2,}\b', text.lower())
    freq  = {}
    for w in words:
        if w not in _FILLER:
            freq[w] = freq.get(w, 0) + 1
    return sorted(freq.keys(), key=lambda w: (-freq[w], -len(w)))

# ─────────────────────────────────────────────────────────────────────────────
# SQLite database setup
#
# SCHEMA DESIGN for agent queries
# --------------------------------
# records          — one row per ServiceNow record; all structured fields;
#                    full-text-indexed short_description and close_notes
# record_keywords  — one row per (record, keyword) for keyword search
#
# Agent can use:
#   SELECT * FROM records WHERE table_name='incident' AND priority='1'
#   SELECT * FROM records WHERE state='open' AND opened_date > '2024-01-01'
#   SELECT r.* FROM records r JOIN record_keywords k ON r.sys_id=k.sys_id
#          WHERE k.keyword IN ('terraform','state','lock')
#          GROUP BY r.sys_id HAVING COUNT(DISTINCT k.keyword)>=2
#   SELECT * FROM records WHERE short_description LIKE '%prometheus%'
# ─────────────────────────────────────────────────────────────────────────────

def init_db(conn):
    conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous  = NORMAL;

        CREATE TABLE IF NOT EXISTS records (
            -- Primary keys
            sys_id          TEXT PRIMARY KEY,
            record_id       TEXT NOT NULL,          -- INC/CHG/PRB number
            table_name      TEXT NOT NULL,          -- incident | change_request | ...

            -- Core display fields (stored as display_value)
            short_description TEXT NOT NULL DEFAULT '',
            state             TEXT DEFAULT '',
            priority          TEXT DEFAULT '',
            category          TEXT DEFAULT '',
            subcategory       TEXT DEFAULT '',
            cmdb_ci           TEXT DEFAULT '',
            assignment_group  TEXT DEFAULT '',
            assigned_to       TEXT DEFAULT '',

            -- Incident-specific
            severity          TEXT DEFAULT '',
            urgency           TEXT DEFAULT '',
            impact            TEXT DEFAULT '',
            caller_id         TEXT DEFAULT '',

            -- Change-specific
            change_type       TEXT DEFAULT '',
            phase             TEXT DEFAULT '',
            risk              TEXT DEFAULT '',

            -- Date fields (ISO-8601 YYYY-MM-DD for easy SQL comparison)
            opened_date       TEXT DEFAULT '',
            resolved_date     TEXT DEFAULT '',
            closed_date       TEXT DEFAULT '',
            updated_date      TEXT DEFAULT '',
            created_date      TEXT DEFAULT '',

            -- Full datetime strings (original from ServiceNow)
            opened_at         TEXT DEFAULT '',
            resolved_at       TEXT DEFAULT '',
            closed_at         TEXT DEFAULT '',
            updated_at        TEXT DEFAULT '',
            created_at        TEXT DEFAULT '',

            -- Long text fields (stored for full-text search)
            description       TEXT DEFAULT '',
            close_notes       TEXT DEFAULT '',
            resolution_text   TEXT DEFAULT '',     -- close_notes alias for clarity

            -- Aggregated keywords string (space-separated, for LIKE queries)
            keywords_text     TEXT DEFAULT '',

            -- File path in knowledge/ directory
            file_path         TEXT DEFAULT '',

            -- Sync provenance
            synced_at         TEXT NOT NULL
        );

        -- Fast lookups
        CREATE INDEX IF NOT EXISTS idx_records_table
            ON records (table_name);
        CREATE INDEX IF NOT EXISTS idx_records_priority
            ON records (priority);
        CREATE INDEX IF NOT EXISTS idx_records_state
            ON records (state);
        CREATE INDEX IF NOT EXISTS idx_records_opened
            ON records (opened_date);
        CREATE INDEX IF NOT EXISTS idx_records_cmdb_ci
            ON records (cmdb_ci);
        CREATE INDEX IF NOT EXISTS idx_records_assignment_group
            ON records (assignment_group);
        CREATE INDEX IF NOT EXISTS idx_records_category
            ON records (category);
        CREATE INDEX IF NOT EXISTS idx_records_table_priority
            ON records (table_name, priority);
        CREATE INDEX IF NOT EXISTS idx_records_table_state
            ON records (table_name, state);

        -- Full-text search on key text columns
        CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
            sys_id UNINDEXED,
            record_id,
            table_name UNINDEXED,
            short_description,
            description,
            close_notes,
            keywords_text,
            content='records',
            content_rowid='rowid'
        );

        -- Normalised keyword table for multi-keyword intersection queries
        CREATE TABLE IF NOT EXISTS record_keywords (
            sys_id    TEXT NOT NULL,
            keyword   TEXT NOT NULL,
            PRIMARY KEY (sys_id, keyword),
            FOREIGN KEY (sys_id) REFERENCES records(sys_id)
        );
        CREATE INDEX IF NOT EXISTS idx_kw_keyword
            ON record_keywords (keyword);
        CREATE INDEX IF NOT EXISTS idx_kw_sys_id
            ON record_keywords (sys_id);
    """)
    conn.commit()


def upsert_record(conn, table, item, file_path, synced_at):
    """Insert or replace one ServiceNow record into SQLite."""
    sys_id   = _raw(item.get("sys_id", ""))
    rec_id   = _record_id(item)

    short_desc  = _val(item.get("short_description", "")) or "(no description)"
    state       = _val(item.get("state", ""))
    priority    = _val(item.get("priority", ""))
    category    = _val(item.get("category", ""))
    subcategory = _val(item.get("subcategory", ""))
    cmdb_ci     = _val(item.get("cmdb_ci", ""))
    asgn_grp    = _val(item.get("assignment_group", ""))
    asgnd_to    = _val(item.get("assigned_to", ""))
    severity    = _val(item.get("severity", ""))
    urgency     = _val(item.get("urgency", ""))
    impact      = _val(item.get("impact", ""))
    caller_id   = _val(item.get("caller_id", ""))
    chg_type    = _val(item.get("type", ""))
    phase       = _val(item.get("phase", ""))
    risk        = _val(item.get("risk", ""))

    opened_at   = _val(item.get("opened_at", ""))
    resolved_at = _val(item.get("resolved_at", ""))
    closed_at   = _val(item.get("closed_at", ""))
    updated_at  = _val(item.get("sys_updated_on", ""))
    created_at  = _val(item.get("sys_created_on", ""))

    description  = _val(item.get("description", "")) or _val(item.get("text", ""))
    close_notes  = _val(item.get("close_notes", ""))

    # Build keywords
    kw_sources = [short_desc, description, close_notes, subcategory, cmdb_ci]
    all_kw: list[str] = []
    for src in kw_sources:
        all_kw.extend(_extract_tech_keywords(src))
    seen_kw: dict[str, int] = {}
    for kw in all_kw:
        seen_kw[kw] = seen_kw.get(kw, 0) + 1
    sorted_kw = sorted(seen_kw.keys(), key=lambda w: (-seen_kw[w], -len(w)))[:300]
    keywords_text = " ".join(sorted_kw)

    conn.execute("""
        INSERT OR REPLACE INTO records (
            sys_id, record_id, table_name,
            short_description, state, priority, category, subcategory,
            cmdb_ci, assignment_group, assigned_to,
            severity, urgency, impact, caller_id,
            change_type, phase, risk,
            opened_date, resolved_date, closed_date, updated_date, created_date,
            opened_at, resolved_at, closed_at, updated_at, created_at,
            description, close_notes, resolution_text,
            keywords_text, file_path, synced_at
        ) VALUES (
            ?,?,?,
            ?,?,?,?,?,
            ?,?,?,
            ?,?,?,?,
            ?,?,?,
            ?,?,?,?,?,
            ?,?,?,?,?,
            ?,?,?,
            ?,?,?
        )
    """, (
        sys_id, rec_id, table,
        short_desc, state, priority, category, subcategory,
        cmdb_ci, asgn_grp, asgnd_to,
        severity, urgency, impact, caller_id,
        chg_type, phase, risk,
        _parse_snow_date(opened_at), _parse_snow_date(resolved_at),
        _parse_snow_date(closed_at), _parse_snow_date(updated_at),
        _parse_snow_date(created_at),
        opened_at, resolved_at, closed_at, updated_at, created_at,
        description, close_notes, close_notes,   # resolution_text mirrors close_notes
        keywords_text, str(file_path), synced_at,
    ))

    # Normalised keywords
    conn.execute("DELETE FROM record_keywords WHERE sys_id = ?", (sys_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO record_keywords (sys_id, keyword) VALUES (?,?)",
        [(sys_id, kw) for kw in sorted_kw],
    )

    return sorted_kw

# ─────────────────────────────────────────────────────────────────────────────
# Markdown renderer
# ─────────────────────────────────────────────────────────────────────────────

def render_markdown(table, item, headline_fields):
    """
    Render one ServiceNow record as an agent-optimised Markdown file.

    Sections (in order):
      YAML front-matter   — unique keys + all structured fields
      ## Search Keywords  — FIRST SECTION, dense signal for embedding
      ## Summary          — all headline fields
      ## Description      — full description
      ## Resolution Notes — close_notes with root cause
      ## Implementation Plan / Backout Plan / Test Plan  (changes only)
      ## All Fields       — complete field table
      ## Raw JSON         — verbatim API payload
    """
    rid        = _record_id(item)
    sys_id_val = _raw(item.get("sys_id", ""))
    short_desc = _val(item.get("short_description", "")) or "(no description)"

    lines = []

    # ── YAML front-matter ─────────────────────────────────────────────────
    fm = {
        "record_id":         rid,
        "sys_id":            sys_id_val,
        "table":             table,
        "short_description": short_desc,
        "state":             _val(item.get("state", "")),
        "priority":          _val(item.get("priority", "")),
        "category":          _val(item.get("category", "")),
        "subcategory":       _val(item.get("subcategory", "")),
        "cmdb_ci":           _val(item.get("cmdb_ci", "")),
        "assignment_group":  _val(item.get("assignment_group", "")),
        "assigned_to":       _val(item.get("assigned_to", "")),
        "opened_at":         _val(item.get("opened_at", "")),
        "updated_at":        _val(item.get("sys_updated_on", "")),
    }
    if table == "incident":
        fm["severity"] = _val(item.get("severity", ""))
        fm["urgency"]  = _val(item.get("urgency",  ""))
        fm["impact"]   = _val(item.get("impact",   ""))
        fm["caller_id"] = _val(item.get("caller_id", ""))
        fm["resolved_at"] = _val(item.get("resolved_at", ""))
        fm["close_notes_preview"] = (
            _val(item.get("close_notes", ""))[:200].replace("\n", " ")
        )
    if table == "change_request":
        fm["change_type"] = _val(item.get("type",  ""))
        fm["phase"]       = _val(item.get("phase", ""))
        fm["risk"]        = _val(item.get("risk",  ""))
        fm["start_date"]  = _val(item.get("start_date", ""))
        fm["end_date"]    = _val(item.get("end_date",   ""))

    lines.append("---")
    for k, v in fm.items():
        safe_v = str(v).replace('"', "'").replace('\n', ' ')
        lines.append(f'{k}: "{safe_v}"')
    lines.append("---")
    lines.append("")

    # ── Title ─────────────────────────────────────────────────────────────
    lines.append(f"# {table.upper()} {rid}")
    lines.append(f"**{short_desc}**")
    lines.append("")

    # ── Search Keywords (dense, placed first for highest embedding weight) ─
    lines.append("## Search Keywords")
    lines.append("")
    lines.append(f"short_description: {short_desc}")

    cat    = _val(item.get("category",         ""))
    subcat = _val(item.get("subcategory",      ""))
    ci     = _val(item.get("cmdb_ci",          ""))
    ag     = _val(item.get("assignment_group", ""))
    pri    = _val(item.get("priority",         ""))
    sev    = _val(item.get("severity",         ""))
    state  = _val(item.get("state",            ""))

    if cat:    lines.append(f"category: {cat}")
    if subcat: lines.append(f"subcategory: {subcat}")
    if ci:     lines.append(f"affected_system: {ci}")
    if ag:     lines.append(f"team: {ag}")
    if pri:    lines.append(f"priority: {pri}")
    if sev:    lines.append(f"severity: {sev}")
    if state:  lines.append(f"state: {state}")

    kw_sources = [
        _val(item.get("short_description", "")),
        _val(item.get("description", "")) or _val(item.get("text", "")),
        _val(item.get("close_notes", "")),
        subcat, ci,
    ]
    all_kw_flat: list[str] = []
    for src in kw_sources:
        all_kw_flat.extend(_extract_tech_keywords(src))

    seen_kw: dict[str, int] = {}
    for kw in all_kw_flat:
        seen_kw[kw] = seen_kw.get(kw, 0) + 1
    sorted_kw = sorted(seen_kw.keys(),
                       key=lambda w: (-seen_kw[w], -len(w)))[:300]
    if sorted_kw:
        lines.append(f"keywords: {' '.join(sorted_kw)}")

    lines.append("")

    # ── Summary ───────────────────────────────────────────────────────────
    lines.append("## Summary")
    lines.append("")
    skip_in_summary = {
        "description", "text", "close_notes",
        "justification", "implementation_plan", "backout_plan", "test_plan",
    }
    for f in headline_fields:
        if f in skip_in_summary:
            continue
        v = _val(item.get(f, ""))
        if v:
            label = f.replace("_", " ").title()
            lines.append(f"- **{label}**: {v}")
    lines.append("")

    # ── Description ───────────────────────────────────────────────────────
    desc = _val(item.get("description", "")) or _val(item.get("text", ""))
    if desc and desc.strip():
        lines.append("## Description")
        lines.append("")
        lines.append(desc.strip())
        lines.append("")

    # ── Resolution Notes ──────────────────────────────────────────────────
    close_notes = _val(item.get("close_notes", ""))
    if close_notes and close_notes.strip():
        lines.append("## Resolution Notes")
        lines.append("")
        lines.append(close_notes.strip())
        lines.append("")

    # ── Change plan sections ──────────────────────────────────────────────
    for field, label in [
        ("justification",       "Justification"),
        ("implementation_plan", "Implementation Plan"),
        ("backout_plan",        "Backout Plan"),
        ("test_plan",           "Test Plan"),
    ]:
        text_val = _val(item.get(field, ""))
        if text_val and text_val.strip():
            lines.append(f"## {label}")
            lines.append("")
            lines.append(text_val.strip())
            lines.append("")

    # ── All Fields ────────────────────────────────────────────────────────
    lines.append("## All Fields")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    for field_name in sorted(item.keys()):
        disp = _val(item[field_name])
        raw  = _raw(item[field_name])
        cell = f"{disp} *(raw: {raw})*" if (raw and raw != disp) else disp
        cell = cell.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{field_name}` | {cell} |")
    lines.append("")

    # ── Raw JSON ──────────────────────────────────────────────────────────
    lines.append("## Raw JSON")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(item, indent=2, default=str))
    lines.append("```")
    lines.append("")

    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# Write records
# ─────────────────────────────────────────────────────────────────────────────

def write_records(table, records, headline_fields, conn, synced_at):
    table_dir = KNOWLEDGE_DIR / table
    table_dir.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    sample_fields: set[str] = set()

    for item in records:
        sample_fields.update(item.keys())
        rid = _record_id(item)
        try:
            path = table_dir / f"{rid}.md"
            md   = render_markdown(table, item, headline_fields)
            path.write_text(md, encoding="utf-8")
            upsert_record(conn, table, item, path, synced_at)
            written += 1
        except Exception as exc:
            print(f"  [WARN] could not write {rid}: {exc}")
            skipped += 1

    conn.commit()
    print(f"   [OK] {written} files written to knowledge/{table}/ "
          f"(skipped {skipped})")
    return {"fields": sorted(sample_fields)}

# ─────────────────────────────────────────────────────────────────────────────
# Rebuild FTS index after all rows are committed
# ─────────────────────────────────────────────────────────────────────────────

def rebuild_fts(conn):
    print("[DB] Rebuilding FTS5 index ...")
    conn.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
    conn.commit()
    print("[DB] FTS5 index rebuilt.")

# ─────────────────────────────────────────────────────────────────────────────
# Manifest
# ─────────────────────────────────────────────────────────────────────────────

def write_manifest(results, schema, synced_at):
    META_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "synced_at": synced_at,
        "base_url":  BASE_URL,
        "tables":    results,
        "schema":    schema,
    }
    (META_DIR / "manifest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n[OK] Manifest written → {META_DIR}/manifest.json")

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    KNOWLEDGE_DIR.mkdir(exist_ok=True)
    VECTORDB_DIR.mkdir(exist_ok=True)

    synced_at = datetime.datetime.utcnow().isoformat() + "Z"
    db_path   = VECTORDB_DIR / "servicenow.db"

    print(f"[DB] Opening SQLite database: {db_path}")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    print("[DB] Schema ready.")

    results: dict = {}
    schema:  dict = {}

    for table, config in TABLES.items():
        try:
            records        = fetch_all(table, config)
            s              = write_records(table, records,
                                           config["headline_fields"],
                                           conn, synced_at)
            results[table] = {"count": len(records), "status": "ok"}
            schema[table]  = s
        except Exception as exc:
            print(f"\n[ERROR] {table}: {exc}")
            results[table] = {"count": 0, "status": f"FAILED: {exc}"}

    rebuild_fts(conn)
    conn.close()

    write_manifest(results, schema, synced_at)

    print("\n" + "=" * 60)
    print("ServiceNow sync complete")
    print("=" * 60)
    for t, r in results.items():
        ok = "[OK]" if r["status"] == "ok" else "[FAIL]"
        print(f"  {ok}  {t:<22}  {r['count']:>6} records")
    print("=" * 60)
    print(f"\n  SQLite DB : {db_path}")
    print(f"  Knowledge : {KNOWLEDGE_DIR}/")
    print("=" * 60)


if __name__ == "__main__":
    main()
