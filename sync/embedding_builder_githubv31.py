#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embedding_builder_githubv3.py
Builds the FAISS vector database from all Markdown files in knowledge/
and writes a rich keyword + metadata index to vectordb/keyword_index.json.

Embedding: sentence-transformers/all-MiniLM-L6-v2 (local CPU, no API calls)

Key fixes vs previous version
------------------------------
1. short_description stored as dedicated field in every index entry
2. short_desc_tokens stored separately for single-token match logic
3. Keyword limit raised from 60 to 300
4. STOP_WORDS restricted to true English filler ONLY
   (never strips tech tool names like prometheus, alertmanager, terraform)
5. ## Search Keywords section given its own chunk with high signal density
6. Index stores: short_description, subcategory, cmdb_ci, assignment_group
"""

import os
import re
import json
import time
import datetime
import yaml
from pathlib import Path

from langchain.schema import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from dotenv import load_dotenv

load_dotenv()

# -----------------------------------------------------------------------
# Paths & constants
# -----------------------------------------------------------------------

KNOWLEDGE_DIR = Path("knowledge")
VECTORDB_DIR  = Path("vectordb")
HF_CACHE_DIR  = Path(".hf_cache")

HF_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

FALLBACK_CHUNK_SIZE    = 1200
FALLBACK_CHUNK_OVERLAP = 200
EMBED_BATCH_SIZE       = 200   # no rate limit -- memory tuning only

# True English filler ONLY -- NEVER add tech tool names here
# 'prometheus', 'alertmanager', 'terraform', etc must never appear here
STOP_WORDS = {
    "that", "this", "with", "from", "have", "will", "were", "been",
    "your", "they", "when", "what", "which", "also", "more", "than",
    "then", "into", "some", "none", "true", "false", "after", "before",
    "about", "above", "below", "there", "their", "these", "those",
    "would", "could", "should", "shall", "while", "where",
}

# -----------------------------------------------------------------------
# YAML front-matter parser
# -----------------------------------------------------------------------

def extract_frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}

# -----------------------------------------------------------------------
# Section-aware splitter
# -----------------------------------------------------------------------

HEADER_SPLITTER = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "title"), ("##", "section")],
    strip_headers=False,
)

FALLBACK_SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=FALLBACK_CHUNK_SIZE,
    chunk_overlap=FALLBACK_CHUNK_OVERLAP,
    separators=["\n\n", "\n", " ", ""],
)


def split_document(path, text, fm):
    """
    Split one .md file into LangChain Documents.
    ## Search Keywords becomes a short focused chunk with dense signal.
    Every chunk carries full metadata for agent pre-filtering.
    """
    base_meta = {
        "table":             fm.get("table",             path.parent.name),
        "record_id":         fm.get("record_id",         path.stem),
        "sys_id":            fm.get("sys_id",            ""),
        "state":             fm.get("state",             ""),
        "priority":          fm.get("priority",          ""),
        "category":          fm.get("category",          ""),
        "subcategory":       fm.get("subcategory",       ""),
        "short_description": fm.get("short_description", ""),
        "cmdb_ci":           fm.get("cmdb_ci",           ""),
        "assignment_group":  fm.get("assignment_group",  ""),
        "opened_at":         fm.get("opened_at",         ""),
        "updated_at":        fm.get("updated_at",        ""),
        "file":              str(path),
        "change_type":       fm.get("change_type",       ""),
        "phase":             fm.get("phase",             ""),
        "risk":              fm.get("risk",              ""),
        "severity":          fm.get("severity",          ""),
        "urgency":           fm.get("urgency",           ""),
        "impact":            fm.get("impact",            ""),
    }

    docs = []
    for chunk in HEADER_SPLITTER.split_text(text):
        section    = (chunk.metadata.get("section")
                      or chunk.metadata.get("title") or "body")
        chunk_meta = {**base_meta, "section": section}
        chunk_text = chunk.page_content.strip()
        if not chunk_text:
            continue

        # Raw JSON: single chunk, not split further
        if section.lower() == "raw json":
            docs.append(Document(page_content=chunk_text,
                                 metadata=chunk_meta))
            continue

        # All Fields: single chunk
        if section.lower() == "all fields":
            docs.append(Document(page_content=chunk_text,
                                 metadata=chunk_meta))
            continue

        # All other sections including Search Keywords
        if len(chunk_text) > FALLBACK_CHUNK_SIZE:
            for sub in FALLBACK_SPLITTER.split_text(chunk_text):
                if sub.strip():
                    docs.append(Document(page_content=sub.strip(),
                                         metadata=chunk_meta))
        else:
            docs.append(Document(page_content=chunk_text,
                                 metadata=chunk_meta))
    return docs

# -----------------------------------------------------------------------
# Keyword index entry builder
#
# DATABASE STRUCTURE per entry:
#   record_id         -- unique filename key (INC/CHG/PRB number)
#   sys_id            -- unique ServiceNow global key
#   table             -- incident | change_request | problem | etc
#   short_description -- verbatim, dedicated field for single-token match
#   short_desc_tokens -- tokenised list for O(1) set intersection
#   keywords          -- up to 300 unique tech terms from full body
#   state/priority/category/subcategory/severity/urgency/impact/cmdb_ci
#   assignment_group  -- team name
#   opened_at/updated_at -- dates
#   excerpt           -- first meaningful line for display
#   embedding_model   -- model name for consistency check
# -----------------------------------------------------------------------

def build_keyword_entry(path, text, fm):
    body = re.sub(r'^---.*?---\s*', '', text, flags=re.DOTALL)

    # Extract keywords from full body -- limit 300, no tech term filtering
    words = re.findall(r'\b[A-Za-z][A-Za-z0-9_\-\.]{2,}\b', body.lower())
    kws   = list(dict.fromkeys(
        w for w in words if w not in STOP_WORDS
    ))[:300]

    # short_description from front-matter (most reliable source)
    short_desc = fm.get("short_description", "")

    # Tokenise short_description separately for single-token match
    sd_tokens = []
    if short_desc:
        sd_tokens = re.findall(
            r'\b[A-Za-z][A-Za-z0-9_\-\.]{2,}\b',
            short_desc.lower()
        )
        # Remove only true filler from short_desc tokens
        sd_tokens = [t for t in sd_tokens if t not in STOP_WORDS]

    # Excerpt: prefer short_description, else first body line
    excerpt = short_desc[:300] if short_desc else ""
    if not excerpt:
        for line in body.splitlines():
            line = line.strip().lstrip("#").strip()
            if len(line) > 20:
                excerpt = line[:300]
                break

    return {
        # -- Unique keys --
        "record_id":         fm.get("record_id",         path.stem),
        "sys_id":            fm.get("sys_id",            ""),
        # -- Location --
        "file":              str(path),
        "table":             fm.get("table",             path.parent.name),
        # -- Primary search: short_description --
        "short_description": short_desc,
        "short_desc_tokens": sd_tokens,
        # -- Structured ITSM fields --
        "state":             fm.get("state",             ""),
        "priority":          fm.get("priority",          ""),
        "category":          fm.get("category",          ""),
        "subcategory":       fm.get("subcategory",       ""),
        "severity":          fm.get("severity",          ""),
        "urgency":           fm.get("urgency",           ""),
        "impact":            fm.get("impact",            ""),
        "change_type":       fm.get("change_type",       ""),
        "phase":             fm.get("phase",             ""),
        "risk":              fm.get("risk",              ""),
        "cmdb_ci":           fm.get("cmdb_ci",           ""),
        "assignment_group":  fm.get("assignment_group",  ""),
        "opened_at":         fm.get("opened_at",         ""),
        "updated_at":        fm.get("updated_at",        ""),
        # -- Keyword search --
        "keywords":          kws,
        "size_chars":        len(text),
        "excerpt":           excerpt,
        # -- Provenance --
        "embedding_model":   HF_MODEL_NAME,
    }

# -----------------------------------------------------------------------
# Batch helper
# -----------------------------------------------------------------------

def batch_list(lst, size):
    for i in range(0, len(lst), size):
        yield i, lst[i:i + size]

# -----------------------------------------------------------------------
# Embedding loop
# -----------------------------------------------------------------------

def embed_all(chunks, embeddings):
    vector_db     = None
    total_batches = -(-len(chunks) // EMBED_BATCH_SIZE)
    t_start       = time.time()

    print(f"[INFO] {len(chunks)} chunks | {total_batches} batches | "
          f"batch_size={EMBED_BATCH_SIZE} | local CPU")

    for batch_idx, batch in batch_list(chunks, EMBED_BATCH_SIZE):
        pct = int((batch_idx / max(len(chunks), 1)) * 100)
        print(f"  [EMBED] batch {batch_idx // EMBED_BATCH_SIZE + 1}/"
              f"{total_batches} ({len(batch)} chunks) {pct}% ...",
              end=" ", flush=True)
        try:
            if vector_db is None:
                vector_db = FAISS.from_documents(batch, embeddings)
            else:
                vector_db.add_documents(batch)
            print("[OK]")
        except Exception as exc:
            print(f"[FAIL] {exc}")

    elapsed = time.time() - t_start
    rate    = len(chunks) / elapsed if elapsed > 0 else 0
    print(f"[INFO] Done: {elapsed:.1f}s ({rate:.0f} chunks/sec)")
    return vector_db

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    KNOWLEDGE_DIR.mkdir(exist_ok=True)
    HF_CACHE_DIR.mkdir(exist_ok=True)

    print(f"[INFO] Loading model: {HF_MODEL_NAME}")
    embeddings = HuggingFaceEmbeddings(
        model_name    = HF_MODEL_NAME,
        cache_folder  = str(HF_CACHE_DIR),
        model_kwargs  = {"device": "cpu"},
        encode_kwargs = {"normalize_embeddings": True},
    )
    print("[OK] Model loaded")

    all_chunks    = []
    keyword_index = []
    skipped       = []

    md_files = sorted(KNOWLEDGE_DIR.rglob("*.md"))
    print(f"\n[INFO] {len(md_files)} Markdown files in {KNOWLEDGE_DIR}/")

    for path in md_files:
        if "_meta" in path.parts:
            continue
        try:
            text   = path.read_text(encoding="utf-8", errors="ignore")
            fm     = extract_frontmatter(text)
            chunks = split_document(path, text, fm)
            all_chunks.extend(chunks)
            keyword_index.append(build_keyword_entry(path, text, fm))
            print(f"  [OK] {path}  ({len(chunks)} chunks)")
        except Exception as exc:
            print(f"  [FAIL] {path}: {exc}")
            skipped.append(str(path))

    if not all_chunks:
        raise ValueError("No chunks produced. Check knowledge/ directory.")

    print(f"\n[INFO] Documents : {len(keyword_index)}")
    print(f"[INFO] Chunks    : {len(all_chunks)}")
    print(f"[INFO] Skipped   : {len(skipped)}")

    print()
    vector_db = embed_all(all_chunks, embeddings)
    if vector_db is None:
        raise RuntimeError("[ERROR] No vectors produced.")

    VECTORDB_DIR.mkdir(exist_ok=True)
    vector_db.save_local(str(VECTORDB_DIR))
    print(f"\n[OK] FAISS saved -> {VECTORDB_DIR}/")

    by_table = {}
    for entry in keyword_index:
        by_table.setdefault(entry["table"], []).append(entry)

    payload = {
        "built_at":        datetime.datetime.utcnow().isoformat() + "Z",
        "embedding_model": HF_MODEL_NAME,
        "doc_count":       len(keyword_index),
        "chunk_count":     len(all_chunks),
        "tables":          list(by_table.keys()),
        "by_table":        by_table,
        "entries":         keyword_index,
    }
    idx_path = VECTORDB_DIR / "keyword_index.json"
    idx_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[OK] Keyword index -> {idx_path} ({len(keyword_index)} entries)")

    print("\n" + "=" * 55)
    print(f"Vector DB complete | model: {HF_MODEL_NAME}")
    print("=" * 55)
    for tbl, entries in by_table.items():
        print(f"  {tbl:<22}  {len(entries):>6} records")
    print(f"  Total chunks : {len(all_chunks)}")
    print("=" * 55)


if __name__ == "__main__":
    main()