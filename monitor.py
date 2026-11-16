"""Weekly research monitor: PubMed + official conference-site checks.

Uses no LLM: citations are fetched from PubMed; conference snippets are
unverified excerpts with source links, never inferred deadlines.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
USER_AGENT = "PhDResearchMonitor/1.0 (academic literature monitoring)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def get(url, params=None):
    response = SESSION.get(url, params=params, timeout=25)
    response.raise_for_status()
    return response


def pubmed(topic, query, days, limit, today):
    start = today - dt.timedelta(days=days)
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    params = {
        "db": "pubmed", "term": query, "retmode": "json", "retmax": limit,
        "sort": "pub date", "datetype": "edat",
        "mindate": start.strftime("%Y/%m/%d"),
        "maxdate": today.strftime("%Y/%m/%d"),
        "tool": "phd_research_monitor", "email": os.getenv("NCBI_EMAIL", "")
    }
    params = {k: v for k, v in params.items() if v != ""}
    result = get(base + "esearch.fcgi", params).json()["esearchresult"]
    ids = result.get("idlist", [])
    if not ids:
        return []
    time.sleep(0.4)  # Respect NCBI's unauthenticated request rate limit.
    details = get(base + "esummary.fcgi", {
        "db": "pubmed", "id": ",".join(ids), "retmode": "json"
    }).json()["result"]
    papers = []
    for pmid in ids:
        record = details.get(pmid, {})
        doi = next((x.get("value", "") for x in record.get("articleids", []) if x.get("idtype") == "doi"), "")
        title = BeautifulSoup(html.unescape(record.get("title", "Untitled")), "html.parser").get_text(" ", strip=True)
        papers.append({
            "pmid": pmid, "doi": doi, "title": title,
            "date": record.get("pubdate", "Unknown"),
            "journal": record.get("fulljournalname", ""),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "topics": [topic]
        })
    return papers


def conference_check(conf):
    url = conf["url"]
    try:
        response = get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        lines = [" ".join(x.split()) for x in soup.get_text("\n").splitlines()]
        lines = [x for x in lines if 20 <= len(x) <= 280]
        pattern = re.compile(r"abstract|deadline|submission|call for papers|conference|symposium|registration", re.I)
        excerpts = list(dict.fromkeys(x for x in lines if pattern.search(x)))[:5]
        return {"name": conf["name"], "url": url, "status": "Fetched", "excerpts": excerpts}
    except requests.RequestException as exc:
        return {"name": conf["name"], "url": url, "status": f"Could not fetch ({type(exc).__name__})", "excerpts": []}


def escape_md(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Override today's date (YYYY-MM-DD), for testing")
    args = parser.parse_args()
    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.timezone.utc).date()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    all_papers = {}
    failures = []
    for topic, query in cfg["topics"].items():
        try:
            for p in pubmed(topic, query, cfg["lookback_days"], cfg["max_results_per_topic"], today):
                if p["pmid"] in all_papers:
                    all_papers[p["pmid"]]["topics"].append(topic)
                else:
                    all_papers[p["pmid"]] = p
            print(f"Searched: {topic}")
        except (requests.RequestException, ValueError, KeyError) as exc:
            failures.append(f"{topic}: {type(exc).__name__}: {exc}")
        time.sleep(0.4)
    conferences = [conference_check(c) for c in cfg["conferences"]]
    lines = [f"# Weekly PhD research monitor — {today.isoformat()}", "",
             f"Search window: past {cfg['lookback_days']} days by PubMed entry date (not necessarily publication date).",
             "", "## A. New PubMed records", "",
             f"{len(all_papers)} unique records found across {len(cfg['topics'])} searches. Results are capped per topic and **not AI-screened**.", ""]
    for p in sorted(all_papers.values(), key=lambda x: x["title"].lower()):
        doi_link = f" · [DOI](https://doi.org/{quote(p['doi'], safe='/')})" if p["doi"] else ""
        lines.extend([f"- **[{escape_md(p['title'])}]({p['url']})** ({escape_md(p['date'])}) — {escape_md(p['journal'])}{doi_link}",
                      f"  - Topics: {', '.join(p['topics'])}"])
    if not all_papers:
        lines.append("No records returned (or searches failed; see errors below).")
    lines.extend(["", "## B. Conference website watch", "",
                  "These are **live excerpts, not verified upcoming dates or deadlines**. Open the official link to confirm details.", ""])
    for c in conferences:
        lines.extend([f"### [{c['name']}]({c['url']})", f"Fetch status: {c['status']}", ""])
        lines.extend(f"- {escape_md(s)}" for s in c["excerpts"])
        if not c["excerpts"]:
            lines.append("- No matching text extracted; inspect the website manually.")
        lines.append("")
    if failures:
        lines.extend(["## Search errors", ""] + [f"- {escape_md(e)}" for e in failures])
    output = ROOT / "reports" / f"digest-{today.isoformat()}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved {output}; {len(all_papers)} unique papers; {len(failures)} search errors")
    # Do not silently publish an incomplete digest when every PubMed query fails.
    if len(failures) == len(cfg["topics"]):
        sys.exit("All PubMed searches failed; inspect the log.")


if __name__ == "__main__":
    main()
