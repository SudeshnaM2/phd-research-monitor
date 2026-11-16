"""One-time import of PubMed IDs from existing GitHub digest Issues."""
import json
import os
import re
import subprocess
from pathlib import Path

path = Path(__file__).resolve().parent / 'seen_papers.json'
existing = json.loads(path.read_text()) if path.exists() else {'pmids': []}
if existing.get('pmids'):
    print('History already exists; skipping import.')
else:
    result = subprocess.run(['gh', 'issue', 'list', '--state', 'all', '--limit', '100',
                             '--json', 'title,body'], capture_output=True, text=True, check=True,
                            env=os.environ.copy())
    issues = json.loads(result.stdout)
    ids = set()
    for issue in issues:
        if issue['title'].startswith('Weekly PhD Research Digest'):
            ids.update(re.findall(r'pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/?', issue.get('body') or ''))
    existing['pmids'] = sorted(ids, key=int)
    path.write_text(json.dumps(existing, indent=2) + '\n')
    print(f'Imported {len(ids)} previously reported PMIDs from digest Issues.')
