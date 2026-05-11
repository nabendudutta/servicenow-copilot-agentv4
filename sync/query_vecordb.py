#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
query_vectordb.py
CLI search tool for the GitHub Copilot agent.
Searches the FAISS vector database built by embedding_builder_githubv3.py.

Embedding model (MUST match builder): sentence-transformers/all-MiniLM-L6-v2

Key fixes vs previous version
------------------------------
1. short_description checked in keyword pre-screen (was missing entirely)
2. sd_tokens (short_description tokens) matched with threshold=1
   (single matching token in short_description = hit)
3. General body keywords still require 2+ token overlap to reduce noise
4. Score normalisation detects FAISS index type to apply correct formula
5. Diagnostic output shows index type and raw distances for debugging

Usage
-----
  python sync/query_vectordb.py "<query>" [options]

Options
-------
  --top_k N          Results to return (default 10)
  --min_score F      Min similarity 0.0-1.0 (default 0.30)
  --filter KEY=VAL   Metadata filter e.g. --filter table=incident
  --section NAME     Section filter: resolution|description|summary|keywords
  --json             Raw JSON output
  --debug            Print raw FAISS distances and index type

Exit codes: 0=found, 1=not found, 2=system error
"""

import os
import sys
import json
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

# -----------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------

VECTORDB_DIR  = Path("vectordb")
KEYWORD_INDEX = VECTORDB_DIR / "keyword_index.json"
HF_CACHE_DIR  = Path(".hf_cache")
HF_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

SECTION_ALIASES = {
    "keywords":    ["search keywords", "keywords"],
    "resolution":  ["resolution notes", "resolution", "close notes",
                    "close_notes"],
    "description": ["description", "short description"],
    "summary":     ["summary"],
    "all_fields":  ["all fields"],
    "plans":       ["implementation plan", "backout plan", "test plan"],
}

# -----------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------

parser = argparse.ArgumentParser(
    description="Search the ServiceNow internal FAISS vector database"
)
parser.add_argument("query",       type=str,   help="Search query")
parser.add_argument("--top_k",     type=int,   default=10)
parser.add_argument("--min_score", type=float, default=0.30)
parser.add_argument("--filter",    type=str,   default=None)
parser.add_argument("--section",   type=str,   default=None)
parser.add_argument("--json",      action="store_true")
parser.add_argument("--debug",     action="store_true")
args = parser.parse_args()

# -----------------------------------------------------------------------
# Validate DB
# -----------------------------------------------------------------------

if not VECTORDB_DIR.exists() or not (VECTORDB_DIR / "index.faiss").exists():
    print("[SYSTEM ERROR] Vector DB not found at vectordb/")
    print("               Run: python sync/embedding_builder_githubv3.py")
    sys.exit(2)

# -----------------------------------------------------------------------
# Model consistency check
# -----------------------------------------------------------------------

if KEYWORD_INDEX.exists():
    try:
        idx_meta   = json.loads(KEYWORD_INDEX.read_text(encoding="utf-8"))
        built_with = idx_meta.get("embedding_model", "")
        if built_with and built_with != HF_MODEL_NAME:
            print(f"[WARN] Embedding model mismatch!")
            print(f"       Built with : {built_with}")
            print(f"       Querying   : {HF_MODEL_NAME}")
            print(f"       Rebuild DB : python sync/embedding_builder_githubv3.py")
    except Exception:
        pass

# -----------------------------------------------------------------------
# Parse --filter
# -----------------------------------------------------------------------

meta_filter = None
if args.filter:
    try:
        key, val = args.filter.split("=", 1)
        meta_filter = {key.strip(): val.strip()}
    except ValueError:
        print(f"[WARN] Invalid --filter '{args.filter}' -- ignored.")

# -----------------------------------------------------------------------
# Load HuggingFace model (must match builder exactly)
# -----------------------------------------------------------------------

try:
    embeddings = HuggingFaceEmbeddings(
        model_name    = HF_MODEL_NAME,
        cache_folder  = str(HF_CACHE_DIR),
        model_kwargs  = {"device": "cpu"},
        encode_kwargs = {"normalize_embeddings": True},
    )
except Exception as e:
    print(f"[SYSTEM ERROR] Model load failed: {e}")
    sys.exit(2)

# -----------------------------------------------------------------------
# Load FAISS index
# -----------------------------------------------------------------------

try:
    vector_db = FAISS.load_local(
        str(VECTORDB_DIR),
        embeddings,
        allow_dangerous_deserialization=True,
    )
except Exception as e:
    print(f"[SYSTEM ERROR] FAISS load failed: {e}")
    sys.exit(2)

# -----------------------------------------------------------------------
# Score normalisation
#
# HuggingFace with normalize_embeddings=True produces unit-length vectors.
# FAISS.from_documents() uses IndexFlatL2 by default.
# For unit vectors: L2_distance = 2 * (1 - cosine_similarity)
# Therefore: cosine_similarity = 1 - L2_distance/2  (range 0-1)
#
# If the index is IndexFlatIP (inner product):
# The "distance" is already the cosine similarity directly.
#
# We detect the index type and apply the correct formula.
# -----------------------------------------------------------------------

def detect_and_norm(dist):
    """
    Apply correct score normalisation based on FAISS index type.
    Returns a 0-1 similarity score.
    """
    try:
        idx_type = type(vector_db.index).__name__
    except Exception:
        idx_type = "unknown"

    if "IP" in idx_type or "InnerProduct" in idx_type:
        # Inner product index: dist IS the similarity (0-1 for normalised)
        score = float(dist)
    else:
        # L2 index (default): convert L2 distance to cosine similarity
        score = 1.0 - (float(dist) / 2.0)

    return max(0.0, min(1.0, score))

# -----------------------------------------------------------------------
# Keyword pre-screen
#
# Three-tier matching logic:
#
# TIER 1: Exact record number match (INC/CHG/PRB + digits in query)
#         -> immediate high-priority hit
#
# TIER 2: short_description token match (1 token sufficient)
#         -> any query token found in sd_tokens = hit
#         Reason: short_description is the most important search field.
#         A query "prometheus alertmanager not sending alerts" must hit
#         a record whose short_description contains "prometheus" even if
#         no other tokens match.
#
# TIER 3: General body keyword match (2 tokens required)
#         -> reduces noise from coincidental single-word matches
#
# TIER 4: Structured field match (state/priority/category/etc)
#         -> handles queries like "P1 critical incidents"
# -----------------------------------------------------------------------

keyword_hits = []

if KEYWORD_INDEX.exists():
    try:
        index_data   = json.loads(KEYWORD_INDEX.read_text(encoding="utf-8"))
        query_lower  = args.query.lower()
        query_tokens = set(re.split(r'\W+', query_lower) if True else [])

        # Use regex split for cleaner tokenisation
        import re as _re
        query_tokens = set(
            t for t in _re.split(r'[\s\-_/]+', query_lower)
            if len(t) >= 3
        )

        entries = index_data.get("entries", [])
        if meta_filter and "table" in meta_filter:
            entries = [e for e in entries
                       if e.get("table") == meta_filter["table"]]

        for entry in entries:
            rid = (entry.get("record_id") or "").lower()

            # TIER 1: exact record number
            if rid and rid in query_lower:
                keyword_hits.insert(0, entry)
                continue

            # TIER 2: short_description single-token match
            sd_tokens = set(
                t.lower() for t in entry.get("short_desc_tokens", [])
            )
            # Also tokenise short_description string directly as fallback
            sd_str = (entry.get("short_description") or "").lower()
            sd_words = set(_re.split(r'[\s\-_/]+', sd_str))
            sd_all = sd_tokens | sd_words
            sd_match = query_tokens & sd_all
            if sd_match:
                keyword_hits.append(entry)
                continue

            # TIER 3: body keyword 2+ token match
            kws     = set(k.lower() for k in entry.get("keywords", []))
            overlap = query_tokens & kws
            if len(overlap) >= 2:
                keyword_hits.append(entry)
                continue

            # TIER 4: structured field token match
            flds = " ".join([
                entry.get("state",             ""),
                entry.get("priority",          ""),
                entry.get("category",          ""),
                entry.get("subcategory",       ""),
                entry.get("severity",          ""),
                entry.get("urgency",           ""),
                entry.get("cmdb_ci",           ""),
                entry.get("assignment_group",  ""),
            ]).lower()
            long_tokens = [t for t in query_tokens if len(t) > 3]
            if any(t in flds for t in long_tokens):
                keyword_hits.append(entry)

    except Exception as e:
        print(f"[WARN] Keyword index error: {e}")

# -----------------------------------------------------------------------
# Vector similarity search
# -----------------------------------------------------------------------

fetch_k = args.top_k * 3 if args.section else args.top_k

try:
    if meta_filter:
        raw_results = vector_db.similarity_search_with_score(
            args.query, k=fetch_k, filter=meta_filter
        )
    else:
        raw_results = vector_db.similarity_search_with_score(
            args.query, k=fetch_k
        )
except Exception as e:
    print(f"[SYSTEM ERROR] Vector search failed: {e}")
    sys.exit(2)

if args.debug and raw_results:
    try:
        idx_type = type(vector_db.index).__name__
    except Exception:
        idx_type = "unknown"
    print(f"[DEBUG] Index type: {idx_type}")
    print(f"[DEBUG] Raw distances (first 5):")
    for doc, dist in raw_results[:5]:
        print(f"  dist={dist:.4f}  "
              f"record={doc.metadata.get('record_id', '?')}  "
              f"section={doc.metadata.get('section', '?')}")

# -----------------------------------------------------------------------
# Build result list
# -----------------------------------------------------------------------

def matches_section(section_meta, requested):
    if not requested:
        return True
    aliases = SECTION_ALIASES.get(requested.lower(), [requested.lower()])
    s = (section_meta or "").lower()
    return any(alias in s for alias in aliases)


results = []
seen    = set()

for doc, dist in raw_results:
    score   = detect_and_norm(dist)
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
results = results[:args.top_k]

# -----------------------------------------------------------------------
# Confidence label
# -----------------------------------------------------------------------

def conf_label(score):
    if score >= 0.85: return "[OK] HIGH (95%)"
    if score >= 0.70: return "[OK] GOOD (80%)"
    if score >= 0.55: return "[!]  MODERATE (65%)"
    if score >= 0.40: return "[!]  WEAK (50%)"
    if score >= 0.25: return "[X]  VERY WEAK (30%)"
    return "[X]  BELOW THRESHOLD"

# -----------------------------------------------------------------------
# No results
# -----------------------------------------------------------------------

if not results and not keyword_hits:
    if not args.json:
        print(f"[NO RESULTS] '{args.query}'")
        if meta_filter:
            print(f"             filter  : {meta_filter}")
        print(f"             min_score: {args.min_score}")
        print("             Suggestions:")
        print("               1. Lower --min_score to 0.20")
        print("               2. Remove --filter to search all tables")
        print("               3. Use fewer, more specific keywords")
        print("               4. Check DB was rebuilt after last sync:")
        print("                  python sync/embedding_builder_githubv3.py")
    else:
        print(json.dumps({
            "query": args.query, "result_count": 0,
            "results": [], "keyword_hits": [],
        }, indent=2))
    sys.exit(1)

# -----------------------------------------------------------------------
# JSON output
# -----------------------------------------------------------------------

if args.json:
    output = {
        "query":             args.query,
        "filter":            meta_filter,
        "section":           args.section,
        "embedding_model":   HF_MODEL_NAME,
        "result_count":      len(results),
        "keyword_hit_count": len(keyword_hits),
        "results": [
            {
                "rank":              i + 1,
                "score":             round(s, 4),
                "confidence":        conf_label(s),
                "record_id":         d.metadata.get("record_id",         ""),
                "sys_id":            d.metadata.get("sys_id",            ""),
                "table":             d.metadata.get("table",             ""),
                "section":           d.metadata.get("section",           ""),
                "short_description": d.metadata.get("short_description", ""),
                "state":             d.metadata.get("state",             ""),
                "priority":          d.metadata.get("priority",          ""),
                "category":          d.metadata.get("category",          ""),
                "subcategory":       d.metadata.get("subcategory",       ""),
                "severity":          d.metadata.get("severity",          ""),
                "urgency":           d.metadata.get("urgency",           ""),
                "impact":            d.metadata.get("impact",            ""),
                "cmdb_ci":           d.metadata.get("cmdb_ci",           ""),
                "assignment_group":  d.metadata.get("assignment_group",  ""),
                "opened_at":         d.metadata.get("opened_at",         ""),
                "updated_at":        d.metadata.get("updated_at",        ""),
                "file":              d.metadata.get("file",              ""),
                "change_type":       d.metadata.get("change_type",       ""),
                "phase":             d.metadata.get("phase",             ""),
                "risk":              d.metadata.get("risk",              ""),
                "content":           d.page_content[:800],
            }
            for i, (d, s) in enumerate(results)
        ],
        "keyword_candidates": [
            {
                "record_id":         e.get("record_id"),
                "sys_id":            e.get("sys_id"),
                "table":             e.get("table"),
                "short_description": e.get("short_description"),
                "state":             e.get("state"),
                "priority":          e.get("priority"),
                "file":              e.get("file"),
                "excerpt":           e.get("excerpt", "")[:300],
            }
            for e in keyword_hits[:5]
        ],
    }
    print(json.dumps(output, indent=2))
    sys.exit(0)

# -----------------------------------------------------------------------
# Human-readable output
# -----------------------------------------------------------------------

W = 68
import re

print()
print("=" * W)
print("  INTERNAL DB SEARCH RESULTS")
print(f"  Query        : {args.query}")
print(f"  Model        : {HF_MODEL_NAME} (local)")
if meta_filter:
    print(f"  Filter       : {meta_filter}")
if args.section:
    print(f"  Section      : {args.section}")
print(f"  Results      : {len(results)} vector match(es)  |  "
      f"keyword hits: {len(keyword_hits)}")
print("=" * W)

for rank, (doc, score) in enumerate(results, 1):
    m = doc.metadata

    print(f"\n  -- RESULT {rank} --- {conf_label(score)}  (score {score:.3f})")
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
    is_res    = any(k in sec_lower for k in
                    ["resolution", "close", "root cause"])
    preview   = len(content) if is_res else 700

    for line in content[:preview].splitlines():
        print(f"  {line}")
    if len(content) > preview:
        print(f"  ... [{len(content) - preview} more chars in "
              f"{m.get('file')}]")
    print()
    print("  " + "-" * (W - 2))

# Keyword-only hits
if keyword_hits and not results:
    print("\n  KEYWORD PRE-SCREEN CANDIDATES")
    print("  (keyword match only -- no vector score above threshold)")
    print("  " + "-" * (W - 2))
    for e in keyword_hits[:5]:
        print(f"  Record       : {e.get('record_id')}  "
              f"[{e.get('table')}]  {e.get('state')}")
        sd = e.get('short_description', '')
        if sd:
            print(f"  Short Desc   : {sd[:80]}")
        print(f"  Priority     : {e.get('priority')}  "
              f"Category: {e.get('category')}")
        print(f"  Excerpt      : {e.get('excerpt', '')[:200]}")
        print(f"  File         : {e.get('file')}")
        print()

print("=" * W)
print(f"  DB: {VECTORDB_DIR}/  model: {HF_MODEL_NAME}  "
      f"min_score: {args.min_score}")
print("=" * W)
sys.exit(0)