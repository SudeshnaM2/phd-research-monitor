"""Weekly PubMed digest with persistent cross-run PMID history and extractive abstracts."""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / 'seen_papers.json'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'PhDResearchMonitor/2.0 (academic literature monitoring)'})
BASE = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'


def get(url, params=None):
    for attempt in range(3):
        try:
            response = SESSION.get(url, params=params, timeout=35)
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def plain(text):
    return ' '.join(BeautifulSoup(html.unescape(str(text)), 'html.parser').get_text(' ', strip=True).split())


def md(text):
    return plain(text).replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]').replace('|', '\\|')


def pubmed_ids(query, days, today, page_size=200):
    start = today - dt.timedelta(days=days)
    params = {'db': 'pubmed', 'term': query, 'retmode': 'json', 'retmax': 0,
              'datetype': 'edat', 'mindate': start.strftime('%Y/%m/%d'),
              'maxdate': today.strftime('%Y/%m/%d'), 'sort': 'pub date',
              'tool': 'phd_research_monitor'}
    if os.getenv('NCBI_EMAIL'):
        params['email'] = os.environ['NCBI_EMAIL']
    result = get(BASE + 'esearch.fcgi', params).json()['esearchresult']
    count = int(result['count'])
    ids = []
    for offset in range(0, count, page_size):
        params.update({'retstart': offset, 'retmax': page_size})
        page = get(BASE + 'esearch.fcgi', params).json()['esearchresult']['idlist']
        ids.extend(page)
        time.sleep(0.38)
    return ids, count


def fetch_papers(ids):
    papers = {}
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        xml = get(BASE + 'efetch.fcgi', {'db': 'pubmed', 'id': ','.join(chunk), 'retmode': 'xml'}).content
        root = ET.fromstring(xml)
        for article in root.findall('.//PubmedArticle'):
            pmid = article.findtext('./MedlineCitation/PMID')
            if not pmid:
                continue
            title_el = article.find('./MedlineCitation/Article/ArticleTitle')
            title = plain(''.join(title_el.itertext())) if title_el is not None else 'Untitled'
            abstract = article.find('./MedlineCitation/Article/Abstract')
            sections = []
            if abstract is not None:
                for section in abstract.findall('AbstractText'):
                    label = section.attrib.get('Label') or section.attrib.get('NlmCategory', '')
                    text = plain(''.join(section.itertext()))
                    if text:
                        sections.append((label.upper(), text))
            doi = ''
            for identifier in article.findall('./PubmedData/ArticleIdList/ArticleId'):
                if identifier.attrib.get('IdType') == 'doi' and identifier.text:
                    doi = identifier.text.strip()
                    break
            journal = article.findtext('./MedlineCitation/Article/Journal/Title', '')
            pubdate = article.findtext('./MedlineCitation/Article/Journal/JournalIssue/PubDate/Year', '')
            if not pubdate:
                pubdate = article.findtext('./MedlineCitation/Article/Journal/JournalIssue/PubDate/MedlineDate', 'Date unavailable')
            papers[pmid] = {'pmid': pmid, 'title': title, 'journal': journal,
                            'date': pubdate, 'doi': doi, 'sections': sections,
                            'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/', 'topics': []}
        time.sleep(0.38)
    return papers


def excerpt(text, limit=340):
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(' ', 1)[0]
    return clipped + '…'


def abstract_summary(paper):
    sections = paper['sections']
    if not sections:
        return ['**Abstract:** Not available in PubMed; open the paper for details.']
    buckets = {
        'Objective': {'OBJECTIVE', 'OBJECTIVES', 'BACKGROUND', 'AIM', 'AIMS', 'PURPOSE'},
        'Methods': {'METHODS', 'METHOD', 'DESIGN', 'MATERIALS AND METHODS', 'PATIENTS AND METHODS'},
        'Findings': {'RESULTS', 'RESULT', 'FINDINGS'},
        'Conclusion': {'CONCLUSION', 'CONCLUSIONS', 'INTERPRETATION'},
    }
    selected = []
    used = set()
    for name, labels in buckets.items():
        for i, (label, text) in enumerate(sections):
            if label in labels and i not in used:
                selected.append(f'**{name} (abstract):** {md(excerpt(text))}')
                used.add(i)
                break
    if not selected:
        joined = ' '.join(text for _, text in sections)
        return [f'**Abstract excerpt:** {md(excerpt(joined, 560))}']
    return selected


def conference_check(conf):
    try:
        response = get(conf['url'])
        soup = BeautifulSoup(response.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        lines = [' '.join(line.split()) for line in soup.get_text('\n').splitlines()]
        pattern = re.compile(r'abstract|deadline|submission|call for papers|conference|symposium|registration', re.I)
        snippets = list(dict.fromkeys(x for x in lines if 20 <= len(x) <= 280 and pattern.search(x)))[:4]
        return {'name': conf['name'], 'url': conf['url'], 'status': 'Fetched', 'snippets': snippets}
    except requests.RequestException as exc:
        return {'name': conf['name'], 'url': conf['url'], 'status': type(exc).__name__, 'snippets': []}


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    previous = json.loads(HISTORY.read_text(encoding='utf-8')) if HISTORY.exists() else {'pmids': []}
    seen = set(map(str, previous.get('pmids', [])))
    topics_by_id = {}
    errors = []
    for topic, query in cfg['topics'].items():
        try:
            ids, count = pubmed_ids(query, max(21, cfg.get('lookback_days', 8)), today)
            for pmid in ids:
                topics_by_id.setdefault(pmid, []).append(topic)
            print(f'{topic}: {count} matches, retrieved {len(ids)} IDs')
        except (requests.RequestException, ValueError, KeyError) as exc:
            errors.append(f'{topic}: {type(exc).__name__}: {exc}')
        time.sleep(0.38)
    if errors:
        # Do not advance the seen-history when any topic failed: it could hide missed papers.
        sys.exit('PubMed search failure; history unchanged: ' + '; '.join(errors))
    fresh_ids = [pmid for pmid in topics_by_id if pmid not in seen]
    papers = fetch_papers(fresh_ids)
    if len(papers) != len(fresh_ids):
        sys.exit('Not all new PubMed records could be fetched; history unchanged.')
    for pmid, paper in papers.items():
        paper['topics'] = topics_by_id[pmid]
    ordered = sorted(papers.values(), key=lambda p: (len(p['topics']), p['pmid']), reverse=True)
    max_featured = 15
    featured = ordered[:max_featured]
    lines = [f'# Weekly PhD research digest — {today.isoformat()}', '',
             f'**{len(papers)} newly discovered papers** · {len(seen)} previously reported PMIDs excluded · {len(cfg["topics"])} topics searched', '',
             'Search uses a 21-day PubMed entry-date window; only PMIDs absent from the saved history are reported.',
             'Summaries below are **extracts from PubMed abstracts**, not AI-generated interpretations. Verify methods and findings in the full paper.', '',
             f'## Key reading: {len(featured)} featured papers', '',
             'Featured papers are ordered by number of matching search topics (a simple relevance proxy, not a quality assessment).', '']
    for idx, p in enumerate(featured, 1):
        doi = f' · [DOI](https://doi.org/{quote(p["doi"], safe="/")})' if p['doi'] else ''
        lines.extend([f'### {idx}. [{md(p["title"])}]({p["url"]})',
                      f'{md(p["journal"])} · {md(p["date"])} · [PubMed]({p["url"]}){doi}',
                      f'**Topics:** {", ".join(map(md, p["topics"]))}', ''])
        lines.extend(abstract_summary(p))
        lines.append('')
    if not featured:
        lines.extend(['No new papers found in this run.', ''])
    lines.extend(['## All newly discovered papers', '',
  'This index includes every newly found record, including papers not featured above.', ''])
    for p in sorted(papers.values(), key=lambda x: x['title'].lower()):
        doi = f' · [DOI](https://doi.org/{quote(p["doi"], safe="/")})' if p['doi'] else ''
        lines.append(f'- [{md(p["title"])}]({p["url"]}) — {md(p["journal"])} ({md(p["date"])}){doi}')
    lines.extend(['', '## Conference website watch', '',
                  'These are website excerpts, **not verified new announcements or deadlines**. Check the official links.', ''])
    for conf in cfg['conferences']:
        result = conference_check(conf)
        lines.extend([f'### [{md(result["name"])}]({result["url"]})', f'Status: {result["status"]}', ''])
        lines.extend(f'- {md(s)}' for s in result['snippets'])
        if not result['snippets']:
            lines.append('- No matching excerpts found.')
        lines.append('')
    report = ROOT / 'reports' / f'digest-{today.isoformat()}.md'
    report.parent.mkdir(exist_ok=True)
    report.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    # Staged history is promoted to seen_papers.json ONLY after the GitHub Issue is published.
    staged = {'pmids': sorted(seen | set(papers.keys()), key=int), 'last_successful_search': today.isoformat()}
    (ROOT / 'pending_seen_papers.json').write_text(json.dumps(staged, indent=2) + '\n', encoding='utf-8')
    print(f'Report: {report}; new={len(papers)}; previously seen={len(seen)}')


if __name__ == '__main__':
    main()
