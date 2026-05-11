#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
query_structured.py  —  SQL-only structured query tool for the agent.
======================================================================

Use this script when you need STRUCTURED queries that don't need semantic
understanding — counts, lists by state/priority/date, specific record lookups.

This queries servicenow.db (SQLite) directly. No ML model needed.
Starts in <1 ms and returns deterministic, exact results.

TYPICAL AGENT USAGE
-------------------
  # All open P1 incidents
  python sync/query_structured.py --table incident --priority 1 --state open

  # Incidents from last 7 days
  python sync/query_structured.py --table incident --days 7

  # All changes for a specific CI
  python sync/query_structured.py --table change_request --ci "prod-db-01"

  # Count by priority for a table
  python sync/query_structured.py --table incident --aggregate priority

  # Count by state
  python sync/query_structured.py --table incident --aggregate state

  # Count by assignment group
  python sync/query_structured.py --table incident --aggregate assignment_group

  # All records for an assignment group
  python sync/query_structured.py --table incident --group "DevOps Team"

  # Records opened in a date range
  python sync/query_structured.py --table incident --from 2024-01-01 --to 2024-03-31

  # Summary stats for a table
  python sync/query_structured.py --table incident --stats

OPTIONS
-------
  --table TABLE          incident | change_request | problem | kb_knowledge |
                         sc_req_item | sc_task
  --priority N           Filter by priority (1=Critical, 2=High, 3=Medium, 4=Low)
  --state STATE          Filter by state (open | closed | resolved | in_progress ...)
  --category CAT         Filter by category
  --group GROUP          Filter by assignment_group (partial match)
  --ci CI                Filter by cmdb_ci (partial match)
  --assigned PERSON      Filter by assigned_to (partial match)
  --days N               Records opened in last N days
  --from YYYY-MM-DD      Records opened on or after this date
  --to   YYYY-MM-DD      Records opened on or before this date
  --aggregate FIELD      Count records grouped by FIELD
  --stats                Show summary statistics for the table
  --top_k N              Max rows to return (default: 20)
  --json                 Machine-readable JSON output
  --debug                Show SQL being executed

EXIT CODES: 0=results, 1=no results, 2=system error
"""

import sys
import json
import sqlite3
import argparse
from pathlib import Path

VECTORDB_DIR = Path("vectordb")
DB_PATH      = VECTORDB_DIR / "servicenow.db"

# ─────────────────────────────────────────────────────────────────────────────
# Args
# ─────────────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description="Structured SQL query against the ServiceNow SQLite database"
)
parser.add_argument("--table",     type=str, default=None,
                    choices=["incident","change_request","problem",
                             "kb_knowledge","sc_req_item","sc_task"])
parser.add_argument("--priority",  type=str, default=None)
parser.add_argument("--state",     type=str, default=None)
parser.add_argument("--category",  type=str, default=None)
parser.add_argument("--group",     type=str, default=None,
                    help="Assignment group (partial match)")
parser.add_argument("--ci",        type=str, default=None,
                    help="Configuration item (partial match)")
parser.add_argument("--assigned",  type=str, default=None,
                    help="Assigned to (partial match)")
parser.add_argument("--days",      type=int, default=None)
parser.add_argument("--from",      dest="date_from", type=str, default=None)
parser.add_argument("--to",        dest="date_to",   type=str, default=None)
parser.add_argument("--aggregate", type=str, default=None,
                    choices=["priority","state","category","assignment_group",
                             "subcategory","cmdb_ci","change_type","phase",
                             "risk","table_name"])
parser.add_argument("--stats",     action="store_true")
parser.add_argument("--top_k",     type=int, default=20)
parser.add_argument("--json",      action="store_true")
parser.add_argument("--debug",     action="store_true")
args = parser.parse_args()

# ─────────────────────────────────────────────────────────────────────────────
# DB check
# ─────────────────────────────────────────────────────────────────────────────

if not DB_PATH.exists():
    print(f"[SYSTEM ERROR] SQLite DB not found: {DB_PATH}")
    print("  Run: python sync/servicenow_syncv4.py")
    sys.exit(2)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur  = conn.cursor()

# ─────────────────────────────────────────────────────────────────────────────
# Build WHERE clause
# ─────────────────────────────────────────────────────────────────────────────

where_parts: list[str] = []
bind_vals:   list      = []

if args.table:
    where_parts.append("table_name = ?")
    bind_vals.append(args.table)

if args.priority:
    # Allow "1", "P1", "critical" etc to all match priority field
    pri = args.priority.strip().lstrip("Pp")
    where_parts.append("(priority = ? OR LOWER(priority) LIKE ?)")
    bind_vals.extend([pri, f"%{args.priority.lower()}%"])

if args.state:
    where_parts.append("LOWER(state) LIKE ?")
    bind_vals.append(f"%{args.state.lower()}%")

if args.category:
    where_parts.append("LOWER(category) LIKE ?")
    bind_vals.append(f"%{args.category.lower()}%")

if args.group:
    where_parts.append("LOWER(assignment_group) LIKE ?")
    bind_vals.append(f"%{args.group.lower()}%")

if args.ci:
    where_parts.append("LOWER(cmdb_ci) LIKE ?")
    bind_vals.append(f"%{args.ci.lower()}%")

if args.assigned:
    where_parts.append("LOWER(assigned_to) LIKE ?")
    bind_vals.append(f"%{args.assigned.lower()}%")

if args.days:
    where_parts.append("opened_date >= date('now', ?)")
    bind_vals.append(f"-{args.days} days")

if args.date_from:
    where_parts.append("opened_date >= ?")
    bind_vals.append(args.date_from)

if args.date_to:
    where_parts.append("opened_date <= ?")
    bind_vals.append(args.date_to)

where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

# ─────────────────────────────────────────────────────────────────────────────
# Summary statistics mode
# ─────────────────────────────────────────────────────────────────────────────

if args.stats:
    queries = {
        "total":          f"SELECT COUNT(*) FROM records {where_sql}",
        "by_priority":    f"""
            SELECT priority, COUNT(*) as cnt FROM records {where_sql}
            GROUP BY priority ORDER BY cnt DESC
        """,
        "by_state":       f"""
            SELECT state, COUNT(*) as cnt FROM records {where_sql}
            GROUP BY state ORDER BY cnt DESC
        """,
        "by_category":    f"""
            SELECT category, COUNT(*) as cnt FROM records {where_sql}
            GROUP BY category ORDER BY cnt DESC LIMIT 10
        """,
        "by_assignment":  f"""
            SELECT assignment_group, COUNT(*) as cnt FROM records {where_sql}
            GROUP BY assignment_group ORDER BY cnt DESC LIMIT 10
        """,
        "oldest_open":    f"""
            SELECT record_id, short_description, opened_date
            FROM records {where_sql}
            {'AND' if where_sql else 'WHERE'} LOWER(state) NOT LIKE '%closed%'
            ORDER BY opened_date ASC LIMIT 5
        """,
        "recent":         f"""
            SELECT record_id, short_description, opened_date, state
            FROM records {where_sql}
            ORDER BY opened_date DESC LIMIT 5
        """,
    }

    stats: dict = {}

    # Total
    cur.execute(queries["total"], bind_vals)
    stats["total"] = cur.fetchone()[0]

    # Grouped breakdowns
    for key in ["by_priority", "by_state", "by_category", "by_assignment"]:
        cur.execute(queries[key], bind_vals)
        stats[key] = [dict(r) for r in cur.fetchall()]

    # Oldest open
    cur.execute(queries["oldest_open"], bind_vals * 2)
    stats["oldest_open"] = [dict(r) for r in cur.fetchall()]

    # Recent
    cur.execute(queries["recent"], bind_vals)
    stats["recent"] = [dict(r) for r in cur.fetchall()]

    conn.close()

    if args.json:
        print(json.dumps(stats, indent=2))
        sys.exit(0)

    W = 68
    print()
    print("=" * W)
    tbl_label = args.table or "ALL TABLES"
    print(f"  STATS: {tbl_label.upper()}")
    print("=" * W)
    print(f"  Total records : {stats['total']}")
    print()
    print("  By Priority:")
    for r in stats["by_priority"]:
        print(f"    {r['priority'] or '(none)':<20}  {r['cnt']:>6}")
    print()
    print("  By State:")
    for r in stats["by_state"]:
        print(f"    {r['state'] or '(none)':<20}  {r['cnt']:>6}")
    print()
    print("  By Category (top 10):")
    for r in stats["by_category"]:
        print(f"    {r['category'] or '(none)':<30}  {r['cnt']:>6}")
    print()
    print("  By Assignment Group (top 10):")
    for r in stats["by_assignment"]:
        print(f"    {r['assignment_group'] or '(none)':<30}  {r['cnt']:>6}")
    print()
    if stats["oldest_open"]:
        print("  Oldest Open:")
        for r in stats["oldest_open"]:
            print(f"    {r['record_id']:<14}  {r['opened_date']}  {r['short_description'][:40]}")
    print()
    print("  Most Recent:")
    for r in stats["recent"]:
        print(f"    {r['record_id']:<14}  {r['opened_date']}  [{r['state']}]  {r['short_description'][:40]}")
    print("=" * W)
    sys.exit(0)

# ─────────────────────────────────────────────────────────────────────────────
# Aggregate mode
# ─────────────────────────────────────────────────────────────────────────────

if args.aggregate:
    col = args.aggregate
    agg_sql = f"""
        SELECT {col}, COUNT(*) as cnt
        FROM records
        {where_sql}
        GROUP BY {col}
        ORDER BY cnt DESC
        LIMIT ?
    """
    if args.debug:
        print(f"[DEBUG SQL] {agg_sql.strip()} | {bind_vals + [args.top_k]}")
    cur.execute(agg_sql, bind_vals + [args.top_k])
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not rows:
        print(f"[NO RESULTS] No records matched filters.")
        sys.exit(1)

    if args.json:
        print(json.dumps({"aggregate_by": col, "rows": rows}, indent=2))
        sys.exit(0)

    W = 68
    tbl_label = args.table or "all tables"
    print(f"\n  Aggregate by '{col}' in {tbl_label}:")
    print(f"  {'Value':<35}  {'Count':>6}")
    print("  " + "-" * 44)
    for r in rows:
        val = r.get(col) or "(none)"
        print(f"  {val:<35}  {r['cnt']:>6}")
    sys.exit(0)

# ─────────────────────────────────────────────────────────────────────────────
# Standard record listing
# ─────────────────────────────────────────────────────────────────────────────

list_sql = f"""
    SELECT * FROM records
    {where_sql}
    ORDER BY opened_date DESC
    LIMIT ?
"""
if args.debug:
    print(f"[DEBUG SQL] {list_sql.strip()} | {bind_vals + [args.top_k]}")

cur.execute(list_sql, bind_vals + [args.top_k])
rows = [dict(r) for r in cur.fetchall()]
conn.close()

if not rows:
    if args.json:
        print(json.dumps({"result_count": 0, "results": []}, indent=2))
    else:
        print("[NO RESULTS] No records matched your filters.")
        print("  Try: --stats to see what is available")
    sys.exit(1)

if args.json:
    output = {
        "result_count": len(rows),
        "filters": {k: getattr(args, k, None) for k in [
            "table","priority","state","category","group","ci",
            "assigned","days","date_from","date_to"
        ] if getattr(args, k, None)},
        "results": rows,
    }
    print(json.dumps(output, indent=2))
    sys.exit(0)

# Human-readable
W = 68
print()
print("=" * W)
tbl_label = args.table or "ALL TABLES"
print(f"  STRUCTURED QUERY: {tbl_label.upper()}")
print(f"  Results: {len(rows)}")
print("=" * W)

for rank, row in enumerate(rows, 1):
    print(f"\n  [{rank}] {row.get('record_id','N/A')}  "
          f"[{row.get('table_name','N/A')}]  "
          f"state={row.get('state','N/A')}  "
          f"priority={row.get('priority','N/A')}")
    sd = row.get("short_description", "")
    if sd:
        print(f"       {sd[:80]}")
    opened = row.get("opened_at", "") or row.get("opened_date", "")
    ag = row.get("assignment_group", "")
    ci = row.get("cmdb_ci", "")
    if opened: print(f"       Opened: {opened}")
    if ag:     print(f"       Group : {ag}")
    if ci:     print(f"       CI    : {ci}")

print()
print("=" * W)
sys.exit(0)
