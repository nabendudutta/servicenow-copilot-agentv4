import json
from pathlib import Path

# Load keyword index
keyword_index = json.loads(Path('vectordb/keyword_index.json').read_text(encoding='utf-8'))
entries = keyword_index.get('entries', [])

# Search for Terraform state lock related entries
query = 'terraform state lock azure'
query_tokens = set(query.lower().split())

print(f'Total entries: {len(entries)}')
print()

# Search through keyword index
results = []
for entry in entries:
    rid = entry.get('record_id', '').lower()
    kws = set([kw.lower() for kw in entry.get('keywords', [])])
    desc = entry.get('description', '').lower()
    category = entry.get('category', '').lower()
    
    # Check for Terraform keywords
    if any(term in rid + ' ' + ' '.join(kws) + ' ' + desc + ' ' + category for term in ['terraform', 'state', 'lock', 'azure']):
        results.append(entry)

print(f'Matching records: {len(results)}')
for r in results[:10]:
    print(f"  - {r.get('record_id')}: {r.get('description', '')[:80]}")
