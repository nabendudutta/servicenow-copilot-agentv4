#!/usr/bin/env python3
"""
internet_search.py — Internet fallback search for ServiceNow Copilot.

This script is ONLY called by the agent after ALL 3 internal DB attempts
have returned zero results. It uses DuckDuckGo (no API key needed).

Usage:
    python sync/internet_search.py "terraform state lock error fix" --max_results 5
"""

import sys
import json
import argparse
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument("query",        type=str,  help="Search query")
parser.add_argument("--max_results",type=int,  default=5)
args = parser.parse_args()

print("=" * 60)
print("🌐 INTERNET FALLBACK SEARCH")
print(f"   ⚠️  Internal DB returned 0 results after 3 attempts.")
print(f"   Query : {args.query}")
print("=" * 60)

try:
    from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(args.query, max_results=args.max_results):
            results.append({
                "title":   r.get("title", ""),
                "url":     r.get("href",  ""),
                "snippet": r.get("body",  "")[:300],
            })

    if not results:
        print("\n❌ No internet results found either.")
        sys.exit(0)

    print(f"\nFound {len(results)} internet result(s):\n")
    for i, r in enumerate(results, 1):
        print(f"[{i}] {r['title']}")
        print(f"     URL     : {r['url']}")
        print(f"     Snippet : {r['snippet'][:200]}")
        print()

    print("=" * 60)
    print(f"Searched: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("Internet confidence: ~50-60% (unverified external source)")
    print("=" * 60)

    # Also output JSON for agent parsing
    output = {
        "status":      "internet_results",
        "query":       args.query,
        "count":       len(results),
        "searched_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "results":     results,
    }
    print("\nJSON_OUTPUT:" + json.dumps(output))

except ImportError:
    print("❌ duckduckgo-search not installed. Run: pip install duckduckgo-search")
    sys.exit(1)
except Exception as e:
    print(f"❌ Internet search failed: {e}")
    sys.exit(1)
