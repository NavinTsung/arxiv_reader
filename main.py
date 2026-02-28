from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import datetime as dt
import re
import requests
import xml.etree.ElementTree as ET

APP_TITLE = "arXiv Astro-Ph Digest API"
APP_VERSION = "1.0.0"

# arXiv provides RSS/Atom feeds for all subject areas, updated daily.  :contentReference[oaicite:1]{index=1}
# The newer RSS infrastructure uses rss.arxiv.org as the base (not strictly required to know "why",
# but useful for stability). :contentReference[oaicite:2]{index=2}
ARXIV_RSS_ASTROPH = "https://rss.arxiv.org/rss/astro-ph"

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

@app.get("/health", summary="Health check")
def health():
    return {"ok": True}

@app.get("/astro-ph/new", summary="Get today's astro-ph new papers (from arXiv RSS)")
def get_new_astroph(
    max_results: int = Query(default=200, ge=1, le=500),
    include_abstracts: bool = Query(default=True),
):
    # Fetch the RSS
    r = requests.get(ARXIV_RSS_ASTROPH, timeout=30)
    r.raise_for_status()

    papers = _parse_rss(r.text)[:max_results]

    if not include_abstracts:
        for p in papers:
            p["abstract"] = ""

    # "date" here is your server's current date; arXiv updates daily (midnight ET per docs). :contentReference[oaicite:3]{index=3}
    return {
        "date": dt.date.today().isoformat(),
        "source": ARXIV_RSS_ASTROPH,
        "papers": papers,
    }