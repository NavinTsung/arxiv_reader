from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import datetime as dt
import re
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from urllib.parse import urlencode

APP_TITLE = "arXiv Astro-Ph Digest API"
APP_VERSION = "1.0.0"

# arXiv provides RSS/Atom feeds for all subject areas, updated daily.  :contentReference[oaicite:1]{index=1}
# The newer RSS infrastructure uses rss.arxiv.org as the base (not strictly required to know "why",
# but useful for stability). :contentReference[oaicite:2]{index=2}
ARXIV_RSS_ASTROPH = "https://rss.arxiv.org/rss/astro-ph"
ARXIV_RECENT_ASTROPH = "https://arxiv.org/list/astro-ph/recent"
ARXIV_API = "https://export.arxiv.org/api/query"  # arXiv API base :contentReference[oaicite:2]{index=2}

app = FastAPI(title=APP_TITLE, version=APP_VERSION)

# Optional but handy if you call this API from other places
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

def _strip_html(s: str) -> str:
    # RSS description often contains HTML tags
    return re.sub(r"<[^>]+>", "", s or "").strip()

def _parse_rss(xml_text: str):
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []

    items = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = _strip_html(item.findtext("description") or "")
        # Common pattern: link points to /abs/<id>
        arxiv_id = ""
        m = re.search(r"arxiv\.org/abs/([^/?#]+)", link)
        if m:
            arxiv_id = m.group(1)

        # Try to extract authors if present in description
        # (Different RSS formats vary; we keep this best-effort.)
        authors = []
        # Some feeds include "Authors:" line (not guaranteed)
        m2 = re.search(r"Authors?:\s*(.+)", desc, re.IGNORECASE)
        if m2:
            authors = [a.strip() for a in m2.group(1).split(",") if a.strip()]

        items.append(
            {
                "id": arxiv_id,
                "title": title,
                "authors": authors,
                "abstract": desc,
                "link": link,
            }
        )
    return items

def _parse_recent_latest_date_block(html_text: str):
    """
    Returns (date_str, ids) for the most recent date section on /recent.
    date_str example: "Fri, 27 Feb 2026"
    """
    soup = BeautifulSoup(html_text, "html.parser")

    # The first date section header is an <h3> like: "Fri, 27 Feb 2026 (showing ...)" :contentReference[oaicite:3]{index=3}
    h3 = soup.find("h3")
    if not h3 or not h3.get_text(strip=True):
        return None, []

    header = h3.get_text(" ", strip=True)
    date_str = header.split("(")[0].strip()  # keep "Fri, 27 Feb 2026"

    # Collect arXiv IDs until the next <h3> (next date section)
    ids = []
    node = h3
    while True:
        node = node.find_next_sibling()
        if node is None:
            break
        if node.name == "h3":
            break

        # arXiv list entries include links like /abs/2602.23364 or /abs/astro-ph/...
        for a in node.select('a[href^="/abs/"]'):
            href = a.get("href", "")
            m = re.search(r"^/abs/([^/?#]+)", href)
            if m:
                ids.append(m.group(1))

    # De-duplicate while preserving order
    seen = set()
    ids_unique = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            ids_unique.append(x)

    return date_str, ids_unique

def _fetch_arxiv_api_metadata(ids, max_results=500):
    """
    Batch fetch titles/authors/abstracts via arXiv API using id_list (best-effort).
    """
    if not ids:
        return []

    # arXiv API supports id_list; chunk to be safe
    results = []
    chunk_size = 50
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i+chunk_size]
        params = {
            "id_list": ",".join(chunk),
            "max_results": len(chunk),
        }
        url = f"{ARXIV_API}?{urlencode(params)}"
        r = requests.get(url, timeout=30)
        r.raise_for_status()

        # API returns Atom; parse minimally with ElementTree
        root = ET.fromstring(r.text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns):
            # id like "http://arxiv.org/abs/2602.23364v1"
            eid = entry.findtext("a:id", default="", namespaces=ns)
            m = re.search(r"arxiv\.org/abs/([^v]+)", eid)
            arxiv_id = m.group(1) if m else ""

            title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
            abstract = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
            authors = [a.findtext("a:name", default="", namespaces=ns) for a in entry.findall("a:author", ns)]
            link = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""

            results.append({
                "id": arxiv_id,
                "title": re.sub(r"\s+", " ", title),
                "authors": [x for x in authors if x],
                "abstract": re.sub(r"\s+", " ", abstract),
                "link": link,
            })

    return results

def _recent_date_to_iso(date_str: str) -> str | None:
    """
    Converts 'Fri, 27 Feb 2026' -> '2026-02-27'
    Returns None if parsing fails.
    """
    if not date_str:
        return None
    try:
        d = dt.datetime.strptime(date_str, "%a, %d %b %Y").date()
        return d.isoformat()
    except Exception:
        return None

@app.get("/health", summary="Health check")
def health():
    return {"ok": True}

@app.get("/astro-ph/new", summary="Get today's astro-ph new papers; fallback to most recent date if none today")
def get_new_astroph(
    max_results: int = Query(default=200, ge=1, le=500),
    include_abstracts: bool = Query(default=False),
):
    # 1) Try RSS first (updated daily). :contentReference[oaicite:4]{index=4}
    r = requests.get(ARXIV_RSS_ASTROPH, timeout=30)
    r.raise_for_status()
    papers = _parse_rss(r.text)[:max_results]

    # 2) If no papers today, fallback to /recent and grab the most recent date block
    if len(papers) == 0:
        recent_url = f"{ARXIV_RECENT_ASTROPH}?show=2000&skip=0"
        rr = requests.get(recent_url, timeout=30)
        rr.raise_for_status()

        date_str, ids = _parse_recent_latest_date_block(rr.text)
        ids = ids[:max_results]

        # IMPORTANT CHANGE:
        # Always fetch metadata (at least titles/authors) for fallback IDs using arXiv API,
        # so the GPT can select papers by title without pulling all abstracts.
        papers = _fetch_arxiv_api_metadata(ids, max_results=max_results)

        if not include_abstracts:
            for p in papers:
                p["abstract"] = ""

        return {
            "date": _recent_date_to_iso(date_str) or (date_str or dt.date.today().isoformat()),
            "source": recent_url,
            "papers": papers,
            "note": "No papers found in today's RSS feed; fell back to most recent date on /recent.",
            "is_fallback": True,
            "fallback_date_human": date_str,
        }

    # Normal RSS path
    if not include_abstracts:
        for p in papers:
            p["abstract"] = ""

    return {
        "date": dt.date.today().isoformat(),
        "source": ARXIV_RSS_ASTROPH,
        "papers": papers,
    }

@app.get("/astro-ph/papers")
def get_papers_by_id(ids: str):
    """
    ids = comma-separated arXiv IDs
    """
    id_list = ids.split(",")
    papers = _fetch_arxiv_api_metadata(id_list)

    return {
        "papers": papers
    }

@app.get("/privacy")
def privacy():
    return FileResponse("privacy.html")