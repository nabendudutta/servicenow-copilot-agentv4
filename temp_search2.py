import os
import re
from pathlib import Path

# Search all knowledge files for 'state lock' or 'terraform'
matches = []

knowledge_dir = Path('knowledge')
for root, dirs, files in os.walk(knowledge_dir):
    for file in files:
        if file.endswith('.md'):
            filepath = Path(root) / file
            try:
                content = filepath.read_text(encoding='utf-8', errors='ignore')
                if re.search(r'(terraform|state lock|lock.*releasing)', content, re.IGNORECASE):
                    # Get first 200 chars of summary
                    match = re.search(r'## Summary\s*\n([^\n]+)', content, re.IGNORECASE)
                    summary = match.group(1) if match else 'No summary'
                    matches.append({
                        'file': str(filepath.relative_to('.')),
                        'summary': summary[:120]
                    })
            except Exception as e:
                pass

print(f'Found {len(matches)} potential matches:\n')
for m in matches[:15]:
    print(f\"- {m['file']}\")
    print(f\"  {m['summary']}\n\")
