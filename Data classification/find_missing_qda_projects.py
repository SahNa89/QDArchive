"""
find_missing_qda_projects.py
================================

Finds projects missing from the classification database by searching
repository 15 (ICPSR) and repository 3 (UK Data Service) directly on their
own search backends - not the DataCite proxy scraper.py used - with every
term in Taggingtables.QDA_SOFTWARE and Taggingtables.QD_KEYWORDS as a query.

Search backends used:
  - Repository 15: https://www.icpsr.umich.edu/sites/icpsr/search/studies?q=
    Same ICPSR search as the /search/top-results page, but with real
    pagination (rows/start) instead of top-results' fixed 5-per-category
    cap - a term with more than 5 hits would otherwise silently lose the
    rest. Server-rendered (Next.js __NEXT_DATA__ JSON), fetched with plain
    HTTP requests, no browser needed for the search step itself.
  - Repository 3: the GraphQL API backing
    https://datacatalogue.ukdataservice.ac.uk/searchresults?search=
    (AWS AppSync, same public x-api-key the site's own frontend bundle
    ships), also fetched with plain HTTP requests.

For every result not already present in PROJECTS - matched by the numeric
study id / FriendlyId embedded in project_url, not the exact URL string,
since the same study can be linked under more than one URL scheme (e.g.
icpsr.umich.edu/web/ICPSR/studies/N vs icpsr.umich.edu/sites/icpsr/view/studies/N)
- this:
  1. Inserts a PROJECTS row. download_date is the real date this script
     ran (never backdated). download_project_folder is built from the
     first 150 characters of the title, same "raw title, sanitized later
     at folder-creation time" convention the rest of this project's
     tooling already uses - just a shorter cut.
  2. Scrapes the project's own page for KEYWORDS / LICENSES / PERSON_ROLE
     (reuses export_metadata_scraper.extract_from_page - not
     reimplemented) and backfills PROJECTS.description from the page's
     JSON-LD/meta description if the search result didn't already carry
     one.
  3. Lists + downloads whatever files are actually reachable, recording
     FILES rows (reuses icpsr_file_lister.py for repository 15 and the
     companion ukds_file_lister.py for repository 3).

Usage:
    python find_missing_qda_projects.py                    # search + insert + metadata + downloads
    python find_missing_qda_projects.py --dry-run           # search only, print candidates, no DB writes
    python find_missing_qda_projects.py --no-download       # search + insert + metadata, skip file downloads
    python find_missing_qda_projects.py --no-metadata       # search + insert only
    python find_missing_qda_projects.py --repo 15           # only one repository
    python find_missing_qda_projects.py --keywords maxqda,nvivo   # only specific search terms
"""

import argparse
import json
import os
import re
import sqlite3
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

import icpsr_file_lister
import ukds_file_lister
from export_metadata_scraper import (
    extract_from_page,
    upsert_keywords_row,
    upsert_license,
    upsert_person_role,
)
from Taggingtables import QDA_SOFTWARE, QD_KEYWORDS

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # pragma: no cover - runtime dependency guard
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = exc
else:
    PLAYWRIGHT_IMPORT_ERROR = None


DATA_CLASSIFICATION_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(DATA_CLASSIFICATION_DIR)
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "23273412-sq26-classification.db")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
)

FOLDER_TITLE_MAX_CHARS = 150


def normalize_text(value):
    """Collapses embedded newlines/repeated whitespace - same rule
    export_metadata_scraper.normalize_text and extract_page_description use
    - so a description built straight from a search result (e.g. ICPSR's
    SUMMARY field, which can contain literal newlines) is stored in the
    same normalized form as one backfilled later from a project page."""
    return re.sub(r"\s+", " ", value or "").strip()


# ============================================================
# REPOSITORY 15 (ICPSR) SEARCH
# ============================================================

ICPSR_SEARCH_URL = "https://www.icpsr.umich.edu/sites/icpsr/search/studies"


def _get_with_retry(session, url, params, max_retries=5):
    """ICPSR rate-limits (429) fairly aggressively under sustained querying.
    Backs off exponentially (2s, 4s, 8s, ...), honoring Retry-After if the
    server sends one, instead of giving up on the first 429."""
    delay = 2
    for attempt in range(max_retries + 1):
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        if attempt == max_retries:
            resp.raise_for_status()
        wait = float(resp.headers.get("Retry-After", delay))
        print(f"    [429] rate-limited, waiting {wait:.0f}s (attempt {attempt + 1}/{max_retries})")
        time.sleep(wait)
        delay *= 2
    return resp


def search_icpsr(term, session, rows=50, max_pages=20):
    """Paginates https://www.icpsr.umich.edu/sites/icpsr/search/studies?q=...
    (the same search /search/top-results uses, minus the 5-per-category
    cap) and returns every "study" doc found for `term`."""
    results = []
    start = 0
    for _ in range(max_pages):
        resp = _get_with_retry(session, ICPSR_SEARCH_URL, {"q": term, "start": start, "rows": rows})
        soup = BeautifulSoup(resp.text, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            break
        data = json.loads(tag.string)
        query_response = data.get("props", {}).get("pageProps", {}).get("queryResponse", {})
        response = query_response.get("response", {})
        docs = response.get("docs", [])
        results.extend(d for d in docs if d.get("OBJECT_TYPE") == "study")

        start += rows
        if not docs or start >= response.get("numFound", 0):
            break
        time.sleep(1.5)
    return results


def extract_icpsr_study_id(url):
    match = re.search(r"/studies/(\d+)", url or "")
    return match.group(1) if match else None


def known_icpsr_study_ids(conn):
    """Numeric study id extracted from every repo-15 project_url already in
    the DB, whichever of the known URL schemes it's stored under
    (icpsr.umich.edu/.../studies/N or openicpsr.org|datalumos.org/.../project/N/) -
    self-published studies commonly share the same numeric id across both."""
    ids = set()
    for (url,) in conn.execute("SELECT project_url FROM PROJECTS WHERE repository_id = 15"):
        m = re.search(r"/studies/(\d+)", url or "")
        if m:
            ids.add(m.group(1))
        m = re.search(r"/project/(\d+)/", url or "")
        if m:
            ids.add(m.group(1))
    return ids


def build_icpsr_record(term, doc):
    title = normalize_text(doc.get("TITLE")) or "Untitled"
    summary = normalize_text(" ".join(s for s in (doc.get("SUMMARY") or []) if s))
    today = date.today().isoformat()
    return {
        "query_string": term,
        "repository_id": 15,
        "repository_url": "https://icpsr.umich.edu",
        "project_url": doc.get("URL") or "",
        "version": str(doc.get("VERSION_LABEL") or ""),
        "title": title,
        "description": summary,
        "language": "en",
        "doi": "",
        "upload_date": (doc.get("VERSION_DATE") or "")[:10],
        "download_date": today,
        "download_repository_folder": "My downloads/icpsr/",
        "download_project_folder": f"My downloads/icpsr/{title[:FOLDER_TITLE_MAX_CHARS]}",
        "download_version_folder": "",
        "download_method": "SCRAPING",
    }


# ============================================================
# REPOSITORY 3 (UK DATA SERVICE) SEARCH
# ============================================================

UKDS_GRAPHQL_URL = "https://ohlhy6cg7nhwtpuer664aeok2i.appsync-api.eu-west-2.amazonaws.com/graphql"
# Public API key shipped in the site's own frontend JS bundle (same one
# https://datacatalogue.ukdataservice.ac.uk itself uses client-side) -
# not a secret, scoped to this AppSync API's public search operations.
UKDS_API_KEY = "da2-dbqlla2y3jf2vaqev4lcrpiq4a"

UKDS_GET_STUDY_LIST_QUERY = """
query GetStudyList($DateFrom: Int, $DateTo: Int, $FacetParams: String, $QueryPath: String, $QueryString: String, $Rows: Int, $Sort: Int, $Start: Int, $Phrase: Boolean) {
  getStudyList(
    DateFrom: $DateFrom
    DateTo: $DateTo
    FacetParams: $FacetParams
    QueryPath: $QueryPath
    QueryString: $QueryString
    Rows: $Rows
    Sort: $Sort
    Start: $Start
    Phrase: $Phrase
  ) {
    Count
    Results {
      FriendlyId
      Id
      Title
      KindOfData
      LatestEditionReleaseDate
      Embargoed
      __typename
    }
    __typename
  }
}
"""


def search_ukds(term, session, rows=50, max_pages=20):
    """Paginates the GraphQL query backing
    https://datacatalogue.ukdataservice.ac.uk/searchresults?search=... and
    returns every study result found for `term`."""
    headers = {
        "content-type": "application/json; charset=UTF-8",
        "x-api-key": UKDS_API_KEY,
        "User-Agent": USER_AGENT,
    }
    results = []
    start = 0
    for _ in range(max_pages):
        payload = {
            "query": UKDS_GET_STUDY_LIST_QUERY,
            "variables": {
                "QueryString": term,
                "Start": start,
                "Rows": rows,
                "Sort": 2,
                "Phrase": False,
                "DateFrom": "440",
                "DateTo": str(date.today().year),
                "FacetParams": "",
            },
        }
        resp = session.post(UKDS_GRAPHQL_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            print(f"  [WARN] UKDS GraphQL error for {term!r}: {data['errors']}")
            break
        study_list = data.get("data", {}).get("getStudyList") or {}
        docs = study_list.get("Results") or []
        results.extend(docs)

        start += rows
        if not docs or start >= (study_list.get("Count") or 0):
            break
        time.sleep(0.3)
    return results


def known_ukds_study_ids(conn):
    ids = set()
    for (url,) in conn.execute("SELECT project_url FROM PROJECTS WHERE repository_id = 3"):
        m = re.search(r"/studies/study/(\d+)", url or "")
        if m:
            ids.add(m.group(1))
    return ids


def build_ukds_record(term, doc):
    title = normalize_text(doc.get("Title")) or "Untitled"
    friendly_id = doc.get("FriendlyId")
    today = date.today().isoformat()
    return {
        "query_string": term,
        "repository_id": 3,
        "repository_url": "https://ukdataservice.ac.uk/learning-hub/qualitative-data/",
        # "#details" (not "#doi") - the tab that actually renders Abstract/Main
        # topics content in the DOM, which scrape_metadata's UKDS JSON-LD +
        # accordion extraction below reads.
        "project_url": f"https://datacatalogue.ukdataservice.ac.uk/studies/study/{friendly_id}#details",
        "version": "",
        "title": title,
        "description": title,
        "language": "en",
        "doi": "",
        "upload_date": (doc.get("LatestEditionReleaseDate") or "")[:10],
        "download_date": today,
        "download_repository_folder": "My downloads/ukdataservice/",
        "download_project_folder": f"My downloads/ukdataservice/{title[:FOLDER_TITLE_MAX_CHARS]}",
        "download_version_folder": "",
        "download_method": "SCRAPING",
    }


# ============================================================
# DB INSERT
# ============================================================

def insert_project(conn, data):
    cur = conn.execute(
        """
        INSERT INTO PROJECTS (
            query_string, repository_id, repository_url, project_url, version,
            title, description, language, doi, upload_date, download_date,
            download_repository_folder, download_project_folder, download_version_folder,
            download_method
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["query_string"], data["repository_id"], data["repository_url"], data["project_url"],
            data["version"], data["title"], data["description"], data["language"], data["doi"],
            data["upload_date"], data["download_date"], data["download_repository_folder"],
            data["download_project_folder"], data["download_version_folder"], data["download_method"],
        ),
    )
    conn.commit()
    return cur.lastrowid


# ============================================================
# METADATA (KEYWORDS / LICENSES / PERSON_ROLE) + DESCRIPTION BACKFILL
# ============================================================

def extract_page_description(html):
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if isinstance(obj, dict):
                desc = obj.get("description")
                if isinstance(desc, str) and desc.strip():
                    return re.sub(r"\s+", " ", desc).strip()
    meta = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return re.sub(r"\s+", " ", meta["content"]).strip()
    return None


def extract_icpsr_doi(html):
    """icpsr.umich.edu study pages embed a schema.org Dataset JSON-LD block
    whose "identifier" is a PropertyValue with a clean DOI URL (verified
    against study 36684: identifier.url == "https://doi.org/10.3886/ICPSR36684.v5")."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        identifier = data.get("identifier")
        if isinstance(identifier, dict):
            doi_url = identifier.get("url") or identifier.get("@id")
            if isinstance(doi_url, str) and "doi.org" in doi_url:
                return doi_url
    return None


def extract_ukds_dataset_jsonld(html):
    """The single schema.org Dataset JSON-LD block every
    datacatalogue.ukdataservice.ac.uk study page embeds - verified against
    study 8132 to carry a clean description, DOI ("@id"), the full keyword
    list, license text and creator names. Far more reliable than parsing
    Material-UI's auto-hashed CSS classes (which is what
    export_metadata_scraper.py's repository-3 branch does, tuned for a
    different/older page layout)."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("@type") == "Dataset":
            return data
    return None


def extract_ukds_accordion_text(html, heading_text):
    """Reads a Material-UI accordion's body text by its visible heading -
    used for "Main topics", which (unlike Abstract) isn't in the JSON-LD
    block. Matches on the MuiAccordionSummary/MuiAccordion-root/
    MuiAccordionDetails structure directly (verified against study 8132),
    not on the auto-hashed per-build CSS class names."""
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(r"^\s*" + re.escape(heading_text) + r"\s*$", re.IGNORECASE)
    for tag in soup.find_all(string=pattern):
        summary = tag.find_parent(class_=re.compile("MuiAccordionSummary"))
        if not summary:
            continue
        accordion = summary.find_parent(class_=re.compile("MuiAccordion-root"))
        if not accordion:
            continue
        details = accordion.find(class_=re.compile("MuiAccordionDetails"))
        if details:
            text = normalize_text(details.get_text(" "))
            if text:
                return text
    return None


def apply_ukds_metadata(conn, pid, html):
    """UK Data Service metadata pass: description (Abstract + Main topics),
    doi, language, KEYWORDS, LICENSES and PERSON_ROLE - all sourced from the
    page's own schema.org JSON-LD block (+ the Main-topics accordion, the
    one field the JSON-LD doesn't carry), not the generic/legacy
    export_metadata_scraper.extract_from_page repository-3 path."""
    jsonld = extract_ukds_dataset_jsonld(html)
    if jsonld is None:
        return

    abstract_html = jsonld.get("description") or ""
    abstract = normalize_text(BeautifulSoup(abstract_html, "html.parser").get_text(" "))
    main_topics = extract_ukds_accordion_text(html, "Main topics")
    description_parts = [p for p in (abstract, f"Main topics: {main_topics}" if main_topics else "") if p]
    description = " ".join(description_parts)

    doi_id = jsonld.get("@id") or ""
    doi = doi_id if doi_id.startswith("http") else (f"https://doi.org/{doi_id}" if doi_id else "")

    if description:
        conn.execute("UPDATE PROJECTS SET description = ? WHERE id = ?", (description, pid))
    if doi:
        conn.execute("UPDATE PROJECTS SET doi = ? WHERE id = ?", (doi, pid))
    conn.execute("UPDATE PROJECTS SET language = 'en' WHERE id = ? AND (language IS NULL OR language = '')", (pid,))
    conn.commit()

    keywords = jsonld.get("keywords")
    if isinstance(keywords, list) and keywords:
        upsert_keywords_row(conn, pid, [normalize_text(str(k)) for k in keywords if k])

    license_text = jsonld.get("license")
    if isinstance(license_text, str) and license_text.strip():
        upsert_license(conn, pid, license_text.strip())

    for creator in (jsonld.get("creator") or []):
        name = creator.get("name") if isinstance(creator, dict) else creator if isinstance(creator, str) else None
        if name:
            upsert_person_role(conn, pid, normalize_text(str(name)), "AUTHOR")


def scrape_metadata(conn, new_ids_15, new_ids_3, db_path):
    if sync_playwright is None:
        print(f"[WARN] Playwright not available ({PLAYWRIGHT_IMPORT_ERROR}); skipping metadata scraping")
        return

    rows = [(pid, 15) for pid in new_ids_15] + [(pid, 3) for pid in new_ids_3]
    if not rows:
        return

    print("\n================ SCRAPING METADATA (keywords / license / person_role) ================\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        request_ctx = p.request.new_context(extra_http_headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            for pid, repository_id in rows:
                row = conn.execute("SELECT project_url, title, description FROM PROJECTS WHERE id = ?", (pid,)).fetchone()
                if not row:
                    continue
                project_url, title, description = row
                print(f"[INFO] metadata for project {pid} ({project_url})")

                page = context.new_page()
                try:
                    page.set_default_navigation_timeout(60000)
                    page.goto(project_url, wait_until="networkidle", timeout=60000)
                    time.sleep(0.5)
                except Exception as exc:
                    print(f"  [WARN] failed to load {project_url}: {exc}")
                    page.close()
                    continue

                if repository_id == 3:
                    try:
                        html = page.content()
                        apply_ukds_metadata(conn, pid, html)
                    except Exception as exc:
                        print(f"  [WARN] UKDS metadata extraction failed: {exc}")
                    page.close()
                    continue

                try:
                    data = extract_from_page(page, request_ctx, project_url, repository_id=repository_id)
                except Exception as exc:
                    print(f"  [WARN] extraction failed: {exc}")
                    data = {"licenses": [], "keywords": [], "person_roles": []}

                try:
                    html = page.content()
                    better_description = extract_page_description(html)
                    if better_description and description == title:
                        conn.execute("UPDATE PROJECTS SET description = ? WHERE id = ?", (better_description, pid))
                    doi = extract_icpsr_doi(html)
                    if doi:
                        conn.execute("UPDATE PROJECTS SET doi = ? WHERE id = ? AND (doi IS NULL OR doi = '')", (doi, pid))
                    conn.commit()
                except Exception:
                    pass

                for license_text in data["licenses"]:
                    upsert_license(conn, pid, license_text)
                if data["keywords"]:
                    upsert_keywords_row(conn, pid, data["keywords"])
                for name, role in data["person_roles"]:
                    upsert_person_role(conn, pid, name, role)

                page.close()
        finally:
            request_ctx.dispose()
            context.close()
            browser.close()


# ============================================================
# SEARCH TERM CONSTRUCTION
# ============================================================

# A bare QDA_SOFTWARE/QD_KEYWORDS term searched on its own is noisy - e.g.
# "atlas" alone pulls in unrelated datasets (a stroke-lesion atlas, roll-call
# maps, ...) that have nothing to do with ATLAS.ti. Joining each term with a
# research/study/paper/article qualifier narrows results to actual research
# output that also matches the QDA/QD term - confirmed live against both
# backends: "atlas" alone returns 108 ICPSR / 6 UKDS hits, "atlas research"
# returns 15 / 1, "atlas study" 11 / 2 - narrower, not broader (these search
# backends AND tokens together by default, they don't OR them).
SEARCH_QUALIFIERS = ("research", "study", "search", "paper", "article")

# "atlas" is excluded as a *search query* even combined with a qualifier -
# it's still dominated by geographic/medical atlases ("Anatomical Tracings
# of Lesions After Stroke (ATLAS)", roll-call/boundary atlases, ...), not
# ATLAS.ti usage. Left in Taggingtables.QDA_SOFTWARE itself (unchanged) since
# that set also drives classification of projects already found some other
# way, where "atlas" in a description is a legitimate signal - it's only
# excluded here, from what gets typed into the search box.
SEARCH_TERM_EXCLUDE = {"atlas"}


def default_search_terms():
    base_keywords = (QDA_SOFTWARE | QD_KEYWORDS) - SEARCH_TERM_EXCLUDE
    return sorted({f"{keyword} {qualifier}" for keyword in base_keywords for qualifier in SEARCH_QUALIFIERS})


# A "study"/"survey" title like "Crime Survey for England and Wales" or a
# "Dataset"/"Database" title like "HMDA National Mortgage Dataset" is a real
# research output, but not a QDA/qualitative-coding project. "Atlas" in a
# title (as opposed to a description) is virtually always a literal
# geographic/medical atlas, not a project that used ATLAS.ti - a real
# ATLAS.ti project mentions the software in its description/methodology,
# not its title. All four words are a strong signal of a mismatch, so
# candidates matching any of them are filtered out before insert_project()
# is ever called (never inserted, rather than inserted then deleted).
TITLE_EXCLUDE_PATTERN = re.compile(r"\b(dataset|database|survey|atlas)\b", re.IGNORECASE)


def is_excluded_title(title):
    return bool(TITLE_EXCLUDE_PATTERN.search(title or ""))


# ============================================================
# ORCHESTRATION
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Search repository 15 (ICPSR) and repository 3 (UK Data Service) for projects "
                    "matching QDA_SOFTWARE/QD_KEYWORDS that are missing from the database, insert them, "
                    "and (unless disabled) scrape their metadata and download their files."
    )
    parser.add_argument("--dry-run", action="store_true", help="Search only, print candidates, no DB writes")
    parser.add_argument("--no-download", action="store_true", help="Skip file listing/downloading")
    parser.add_argument("--no-metadata", action="store_true", help="Skip KEYWORDS/LICENSES/PERSON_ROLE scraping")
    parser.add_argument("--repo", choices=["15", "3", "both"], default="both")
    parser.add_argument("--keywords", type=str, default=None,
                         help="Comma-separated exact search terms to use as-is, bypassing the default "
                              "'<QDA_SOFTWARE|QD_KEYWORDS term> <research|study|search|paper|article>' "
                              "combination (e.g. for a quick single-term test)")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Path to the classification SQLite DB")
    args = parser.parse_args()

    if args.keywords:
        terms = sorted({t.strip() for t in args.keywords.split(",") if t.strip()})
    else:
        terms = default_search_terms()

    print(f"[INFO] {len(terms)} search term(s): {terms}")

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")

    known_15 = known_icpsr_study_ids(conn)
    known_3 = known_ukds_study_ids(conn)
    seen_15_this_run = set()
    seen_3_this_run = set()

    new_ids_15 = []
    new_ids_3 = []

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    for term in terms:
        print(f"\n=== searching {term!r} ===")

        if args.repo in ("15", "both"):
            try:
                docs = search_icpsr(term, session)
            except Exception as exc:
                print(f"  [WARN] ICPSR search failed for {term!r}: {exc}")
                docs = []
            for doc in docs:
                study_id = extract_icpsr_study_id(doc.get("URL", ""))
                if not study_id or study_id in known_15 or study_id in seen_15_this_run:
                    continue
                seen_15_this_run.add(study_id)
                title = doc.get("TITLE") or ""
                if is_excluded_title(title):
                    print(f"  [SKIP-15] {study_id}: {title[:80]!r} (dataset/database/survey in title)")
                    continue
                print(f"  [NEW-15] {study_id}: {title[:80]!r}")
                if args.dry_run:
                    continue
                pid = insert_project(conn, build_icpsr_record(term, doc))
                new_ids_15.append(pid)
            time.sleep(1.5)

        if args.repo in ("3", "both"):
            try:
                docs3 = search_ukds(term, session)
            except Exception as exc:
                print(f"  [WARN] UKDS search failed for {term!r}: {exc}")
                docs3 = []
            for doc in docs3:
                friendly_id = str(doc.get("FriendlyId") or "")
                if not friendly_id or friendly_id in known_3 or friendly_id in seen_3_this_run:
                    continue
                seen_3_this_run.add(friendly_id)
                title = doc.get("Title") or ""
                if is_excluded_title(title):
                    print(f"  [SKIP-3] {friendly_id}: {title[:80]!r} (dataset/database/survey in title)")
                    continue
                print(f"  [NEW-3] {friendly_id}: {title[:80]!r}")
                if args.dry_run:
                    continue
                pid = insert_project(conn, build_ukds_record(term, doc))
                new_ids_3.append(pid)
            time.sleep(0.3)

    print(f"\n[DONE searching] {len(new_ids_15)} new repository-15 project(s), "
          f"{len(new_ids_3)} new repository-3 project(s) inserted "
          f"(download_date = {date.today().isoformat()}).")

    if args.dry_run:
        conn.close()
        return

    if not args.no_metadata:
        scrape_metadata(conn, new_ids_15, new_ids_3, args.db)

    conn.close()

    if not args.no_download:
        print("\n================ DOWNLOADING FILES ================\n")
        # One run() call per repository (missing_only=True), not one per
        # project id: each call launches its own browser, and every project
        # just inserted above has no FILES row yet, so a single batched
        # call already covers all of them without one browser launch per
        # project (which would be extremely slow for more than a handful).
        if new_ids_15:
            try:
                icpsr_file_lister.run(db_path=args.db, missing_only=True)
            except Exception as exc:
                print(f"[WARN] icpsr_file_lister failed: {exc}")
        if new_ids_3:
            try:
                ukds_file_lister.run(db_path=args.db, missing_only=True)
            except Exception as exc:
                print(f"[WARN] ukds_file_lister failed: {exc}")

    print("\n[DONE]")


if __name__ == "__main__":
    main()
