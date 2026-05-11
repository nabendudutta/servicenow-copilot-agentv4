#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
servicenow_syncv3.py
Fetches ALL records from ServiceNow and writes structured Markdown files
optimised for FAISS vector search and GitHub Copilot agent retrieval.

KEY CHANGE: every .md file now has a ## Search Keywords section written
FIRST, containing short_description verbatim plus extracted tech keywords.
This guarantees tool names like 'prometheus', 'alertmanager', 'terraform'
are always present in a compact, high-signal chunk that scores strongly
against matching queries.

Unique key per record: sys_id (ServiceNow globally unique identifier).
Filename key: number (INC/CHG/PRB prefix + digits), falling back to sys_id.

Output layout
-------------
knowledge/
  incident/       -- INC*.md
  change_request/ -- CHG*.md
  problem/        -- PRB*.md
  kb_knowledge/   -- KB*.md
  sc_req_item/    -- RITM*.md
  sc_task/        -- TASK*.md
  _meta/
    manifest.json
"""

import os
import re
import json
import time
import datetime
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# -----------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------

SNOW_INSTANCE = os.getenv("SNOW_INSTANCE", "")
SNOW_USER     = os.getenv("SNOW_USER", "")
SNOW_PASSWORD = os.getenv("SNOW_PASSWORD", "")

missing = [v for v, k in [
    ("SNOW_INSTANCE", SNOW_INSTANCE),
    ("SNOW_USER",     SNOW_USER),
    ("SNOW_PASSWORD", SNOW_PASSWORD),
] if not k]
if missing:
    raise EnvironmentError(f"Missing environment variables: {missing}")

BASE_URL = (
    SNOW_INSTANCE.rstrip("/")
    if SNOW_INSTANCE.startswith("https://")
    else f"https://{SNOW_INSTANCE}.service-now.com"
)

KNOWLEDGE_DIR = Path("knowledge")
META_DIR      = KNOWLEDGE_DIR / "_meta"

# -----------------------------------------------------------------------
# Table definitions  (query="" = ALL records, no state filter)
# -----------------------------------------------------------------------

TABLES = {
    "incident": {
        "query":     "",
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

# -----------------------------------------------------------------------
# HTTP helpers
# -----------------------------------------------------------------------

AUTH    = HTTPBasicAuth(SNOW_USER, SNOW_PASSWORD)
HEADERS = {"Accept": "application/json"}
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
            print(f"    attempt {attempt} failed ({exc}) - retry in {wait}s")
            time.sleep(wait)
    return []


def fetch_all(table, config):
    records, offset = [], 0
    ps    = config["page_size"]
    q     = config.get("query", "")
    label = repr(q) if q else "NONE - ALL records"
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

# -----------------------------------------------------------------------
# Value extraction
# -----------------------------------------------------------------------

def _val(field_data):
    if isinstance(field_data, dict):
        dv = field_data.get("display_value", "")
        rv = field_data.get("value", "")
        return dv if dv else rv
    return str(field_data) if field_data is not None else ""


def _raw(field_data):
    if isinstance(field_data, dict):
        return str(field_data.get("value", ""))
    return str(field_data) if field_data is not None else ""


def _record_id(item):
    """
    Returns the canonical filename-safe record ID.
    Priority: number (INC/CHG/PRB...) > name > sys_id
    sys_id is always unique in ServiceNow -- final fallback.
    """
    for key in ("number", "name", "sys_id"):
        v = _val(item.get(key, ""))
        if v:
            return re.sub(r'[^\w\-]', '_', v)
    return "unknown"

# -----------------------------------------------------------------------
# Keyword extractor
# Only true English filler is stripped -- NEVER tech tool names.
# -----------------------------------------------------------------------

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
    Extract unique tech keywords from text.
    Includes tool names, error codes, system names, version strings.
    Returns list sorted by length descending (longer = more specific).
    """
    if not text:
        return []
    # Match: words 3+ chars including dots and dashes (e.g. v2.5, force-unlock)
    words = re.findall(r'\b[A-Za-z][A-Za-z0-9_\-\.]{2,}\b', text.lower())
    freq  = {}
    for w in words:
        if w not in _FILLER:
            freq[w] = freq.get(w, 0) + 1
    # Sort by frequency desc, then length desc (more specific terms first)
    return sorted(freq.keys(), key=lambda w: (-freq[w], -len(w)))

# -----------------------------------------------------------------------
# Markdown renderer
#
# DB structure per record:
#
#   YAML front-matter       -- unique keys: sys_id, record_id (number)
#                              structured fields: state, priority, category,
#                              subcategory, severity, urgency, impact,
#                              short_description, cmdb_ci, assignment_group
#
#   ## Search Keywords      -- FIRST SECTION (new, critical for search)
#                              short_description verbatim on its own line
#                              category, subcategory, CI, tech keywords
#                              This becomes a short focused embedding chunk
#                              matching queries like "prometheus alertmanager"
#
#   ## Summary              -- all headline fields as bullet list
#   ## Description          -- full description / long text
#   ## Resolution Notes     -- close_notes with root cause + steps
#   ## Implementation Plan  -- change records only
#   ## Backout Plan         -- change records only
#   ## Test Plan            -- change records only
#   ## All Fields           -- complete field table (all API fields)
#   ## Raw JSON             -- verbatim API payload
# -----------------------------------------------------------------------

def render_markdown(table, item, headline_fields):
    rid        = _record_id(item)
    sys_id_val = _raw(item.get("sys_id", ""))
    short_desc = _val(item.get("short_description", "")) or "(no description)"

    lines = []

    # -- YAML front-matter ------------------------------------------------
    # sys_id = globally unique key in ServiceNow
    # record_id = number (INC/CHG/PRB) = human-readable unique key
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
        fm["urgency"]  = _val(item.get("urgency", ""))
        fm["impact"]   = _val(item.get("impact", ""))
    if table == "change_request":
        fm["change_type"] = _val(item.get("type", ""))
        fm["phase"]       = _val(item.get("phase", ""))
        fm["risk"]        = _val(item.get("risk", ""))

    lines.append("---")
    for k, v in fm.items():
        safe_v = str(v).replace('"', "'").replace('\n', ' ')
        lines.append(f'{k}: "{safe_v}"')
    lines.append("---")
    lines.append("")

    # -- Title ------------------------------------------------------------
    lines.append(f"# {table.upper()} {rid}")
    lines.append(f"**{short_desc}**")
    lines.append("")

    # -- Search Keywords (CRITICAL NEW SECTION) ---------------------------
    # This is the primary target for keyword and vector search.
    # Kept SHORT and FOCUSED so its embedding vector strongly represents
    # the key terms. Placed first so it's the first chunk the splitter
    # produces -- highest weight in retrieval.
    lines.append("## Search Keywords")
    lines.append("")
    lines.append(f"short_description: {short_desc}")

    cat    = _val(item.get("category", ""))
    subcat = _val(item.get("subcategory", ""))
    ci     = _val(item.get("cmdb_ci", ""))
    ag     = _val(item.get("assignment_group", ""))
    pri    = _val(item.get("priority", ""))
    sev    = _val(item.get("severity", ""))

    if cat:    lines.append(f"category: {cat}")
    if subcat: lines.append(f"subcategory: {subcat}")
    if ci:     lines.append(f"affected_system: {ci}")
    if ag:     lines.append(f"team: {ag}")
    if pri:    lines.append(f"priority: {pri}")
    if sev:    lines.append(f"severity: {sev}")

    # Collect tech keywords from the three most important text fields
    # short_description, description/text, close_notes
    # These are merged and deduplicated, limited to 300 terms
    kw_sources = [
        _val(item.get("short_description", "")),
        _val(item.get("description", "")) or _val(item.get("text", "")),
        _val(item.get("close_notes", "")),
        subcat,
        ci,
    ]
    all_kw_flat = []
    for src in kw_sources:
        all_kw_flat.extend(_extract_tech_keywords(src))

    # Deduplicate while preserving highest-frequency order
    seen_kw = {}
    for kw in all_kw_flat:
        seen_kw[kw] = seen_kw.get(kw, 0) + 1
    sorted_kw = sorted(seen_kw.keys(),
                       key=lambda w: (-seen_kw[w], -len(w)))[:300]
    if sorted_kw:
        lines.append(f"keywords: {' '.join(sorted_kw)}")

    lines.append("")

    # -- Summary ----------------------------------------------------------
    lines.append("## Summary")
    lines.append("")
    for f in headline_fields:
        v = _val(item.get(f, ""))
        if v and f not in ("description", "text", "close_notes",
                           "justification", "implementation_plan",
                           "backout_plan", "test_plan"):
            label = f.replace("_", " ").title()
            lines.append(f"- **{label}**: {v}")
    lines.append("")

    # -- Description ------------------------------------------------------
    desc = _val(item.get("description", "")) or _val(item.get("text", ""))
    if desc and desc.strip():
        lines.append("## Description")
        lines.append("")
        lines.append(desc.strip())
        lines.append("")

    # -- Resolution Notes -------------------------------------------------
    close_notes = _val(item.get("close_notes", ""))
    if close_notes and close_notes.strip():
        lines.append("## Resolution Notes")
        lines.append("")
        lines.append(close_notes.strip())
        lines.append("")

    # -- Change plan sections ---------------------------------------------
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

    # -- All Fields -------------------------------------------------------
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

    # -- Raw JSON ---------------------------------------------------------
    lines.append("## Raw JSON")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(item, indent=2, default=str))
    lines.append("```")
    lines.append("")

    return "\n".join(lines)

# -----------------------------------------------------------------------
# Write records
# -----------------------------------------------------------------------

def write_records(table, records, headline_fields):
    table_dir = KNOWLEDGE_DIR / table
    table_dir.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    sample_fields = set()

    for item in records:
        sample_fields.update(item.keys())
        rid = _record_id(item)
        try:
            md   = render_markdown(table, item, headline_fields)
            path = table_dir / f"{rid}.md"
            path.write_text(md, encoding="utf-8")
            written += 1
        except Exception as exc:
            print(f"  [WARN] could not write {rid}: {exc}")
            skipped += 1

    print(f"   [OK] {written} files written to knowledge/{table}/ "
          f"(skipped {skipped})")
    return {"fields": sorted(sample_fields)}

# -----------------------------------------------------------------------
# Manifest
# -----------------------------------------------------------------------

def write_manifest(results, schema):
    META_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "synced_at": datetime.datetime.utcnow().isoformat() + "Z",
        "base_url":  BASE_URL,
        "tables":    results,
        "schema":    schema,
    }
    (META_DIR / "manifest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n[OK] Manifest written -> {META_DIR}/manifest.json")

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    KNOWLEDGE_DIR.mkdir(exist_ok=True)
    results, schema = {}, {}

    for table, config in TABLES.items():
        try:
            records        = fetch_all(table, config)
            s              = write_records(table, records,
                                           config["headline_fields"])
            results[table] = {"count": len(records), "status": "ok"}
            schema[table]  = s
        except Exception as exc:
            print(f"\n[ERROR] {table}: {exc}")
            results[table] = {"count": 0, "status": f"FAILED: {exc}"}

    write_manifest(results, schema)

    print("\n" + "=" * 55)
    print("ServiceNow sync complete")
    print("=" * 55)
    for t, r in results.items():
        ok = "[OK]" if r["status"] == "ok" else "[FAIL]"
        print(f"  {ok}  {t:<22}  {r['count']:>6} records")
    print("=" * 55)


if __name__ == "__main__":
    main()