"""
icpsr_file_lister.py
======================

Repository 15 (ICPSR / openICPSR / NACJD / Child & Family Data Archive /
DataLumos) file listing + download.

Replaces icpsr_object_export_client.py, which called a guessed U-M
"Object-Export API" endpoint that was never confirmed to exist and never
worked. This script gets the file list the simple, reliable way instead:
it opens each repository-15 project page in a real browser (Playwright) -
required because these sites serve a Cloudflare bot challenge to plain
HTTP clients (confirmed: a plain `requests.get` on either an openicpsr.org
or icpsr.umich.edu project page returns a 403 "Just a moment..." challenge
page) - and reads the downloadable-file links straight off the rendered
page.

Two link sources are used, verified against real pages (study 37631 and
6035 on icpsr.umich.edu, project 247056 on openicpsr.org):

  1. icpsr.umich.edu / childandfamilydataarchive.org study pages embed a
     schema.org JSON-LD block (<script type="application/ld+json">) whose
     "distribution" array lists real, direct download URLs for every
     format bundle (Stata/SPSS/SAS/ASCII/R/Delimited/Other). This is read
     straight off the page - no guessed endpoint.
  2. Plain <a href="..."> links anywhere on the page that resolve to a
     name ending in a known data/documentation extension (see
     Data acquisition/QDAFileExtensions.csv) - this covers openICPSR /
     DataLumos and any supplemental direct-download links.
  (Most of the per-dataset "Codebook"/"Questionnaire" buttons on
  icpsr.umich.edu render as `href="#"` React components whose real URL is
  only resolved client-side on click; those are not currently captured.)

For every file link found, it then tries the download itself in a real
browser page (not a raw HTTP client - a plain request to icpsr.umich.edu's
download endpoints returns the Cloudflare challenge page, not the file,
and confirmed separately: navigating there without being logged into
ICPSR redirects to login.icpsr.umich.edu, "Sign in to icpsr"):
  - If a real download happens, it's saved to PROJECTS.download_project_folder
    and the FILES row is inserted with status SUCCEEDED.
  - Otherwise (login redirect, timeout, any other failure) nothing is
    written to disk and the FILES row is inserted with status
    FAILED_LOGIN_REQUIRED.

Note: ICPSR requires a logged-in account for every download, even
non-restricted ones, so most icpsr.umich.edu files will legitimately come
back FAILED_LOGIN_REQUIRED - this script does not attempt to log in.

Usage:
    python icpsr_file_lister.py                   # all repo-15 projects
    python icpsr_file_lister.py --project-id 42    # just one project
    python icpsr_file_lister.py --all              # re-process even projects that already have FILES rows
    python icpsr_file_lister.py --dry-run          # scrape + print only, no DB writes and no downloads
"""

import argparse
import csv
import json
import logging
import os
import re
import sqlite3
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except Exception as exc:  # pragma: no cover - runtime dependency guard
    sync_playwright = None
    PlaywrightTimeoutError = Exception
    PLAYWRIGHT_IMPORT_ERROR = exc
else:
    PLAYWRIGHT_IMPORT_ERROR = None

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("icpsr_file_lister")


# ============================================================
# CONFIG
# ============================================================

DATA_CLASSIFICATION_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(DATA_CLASSIFICATION_DIR)
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "23273412-sq26-classification.db")

REPOSITORY_ID = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
)

# Hosts that expose the schema.org JSON-LD "distribution" block (see
# extract_distribution_files below). Other repository-15 hosts
# (openicpsr.org, datalumos.org) fall back to plain link scraping only.
JSON_LD_HOSTS = ("icpsr.umich.edu", "childandfamilydataarchive.org")

DATA_ACQUISITION_DIR = os.path.join(PROJECT_ROOT, "Data acquisition")
QDA_EXTENSIONS_CSV = os.path.join(DATA_ACQUISITION_DIR, "QDAFileExtensions.csv")


def load_known_extensions(csv_path=QDA_EXTENSIONS_CSV):
    """Same extension list Crawler.py uses (QDAFileExtensions.csv's
    'extensions' + 'AttFormat' columns). A plain <a href> link only counts
    as a file if it resolves to a name ending in one of these - not just
    because its URL or link text contains a word like "download", which
    also matches UI buttons like "Download Detailed Metrics" or stray
    links like "Terms"."""
    exts = set()
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            for col in ("extensions", "AttFormat"):
                val = (row.get(col) or "").strip().lower()
                if val.startswith("."):
                    exts.add(val)
    return exts


KNOWN_EXTENSIONS = load_known_extensions()
_FILENAME_RE = re.compile(
    r"[A-Za-z0-9][\w\-.%]*(?:" + "|".join(re.escape(e) for e in sorted(KNOWN_EXTENSIONS, key=len, reverse=True)) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


# ============================================================
# DB HELPERS
# ============================================================

def connect(db_path=DEFAULT_DB_PATH):
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found at {db_path}. Run the acquisition pipeline first.")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def fetch_repo15_projects(conn, project_id=None, missing_only=False):
    where = "repository_id = ?"
    params = [REPOSITORY_ID]
    if project_id is not None:
        where += " AND id = ?"
        params.append(project_id)
    if missing_only:
        where += " AND NOT EXISTS (SELECT 1 FROM FILES f WHERE f.project_id = PROJECTS.id)"
    cur = conn.execute(
        f"SELECT id, project_url, title, download_project_folder FROM PROJECTS WHERE {where}", params
    )
    return cur.fetchall()


def file_exists(conn, project_id, file_name):
    return conn.execute(
        "SELECT 1 FROM FILES WHERE project_id = ? AND file_name = ?", (project_id, file_name)
    ).fetchone()


def insert_file_if_missing(conn, project_id, file_name, status="SUCCEEDED"):
    file_name = (file_name or "").strip()
    if not file_name or file_exists(conn, project_id, file_name):
        return False
    ext = os.path.splitext(file_name)[1].lstrip(".").lower() or "unknown"
    conn.execute(
        "INSERT INTO FILES (project_id, file_name, file_type, status) VALUES (?, ?, ?, ?)",
        (project_id, file_name, ext, status),
    )
    conn.commit()
    return True


# ============================================================
# URL / SCRAPING RULES
# ============================================================

def build_documentation_url(project_url):
    """icpsr.umich.edu hosts a dedicated documentation page for each study;
    openICPSR/DataLumos list files right on the project page itself."""
    if not project_url:
        return None
    parsed = urlparse(project_url.strip())
    host = parsed.netloc.lower()
    if any(h in host for h in JSON_LD_HOSTS):
        path = re.sub(r"/+", "/", parsed.path).rstrip("/")
        if not path.endswith("/datadocumentation"):
            path += "/datadocumentation"
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    return project_url.strip()


def extract_study_id(project_url):
    match = re.search(r"/studies/(\d+)", project_url or "")
    return match.group(1) if match else None


def extract_distribution_files(html, project_url):
    """icpsr.umich.edu / childandfamilydataarchive.org study pages embed a
    schema.org JSON-LD block with a "distribution" array of real, direct
    download URLs (verified against studies 37631 and 6035) - one ZIP
    bundle per format (Stata/SPSS/SAS/ASCII/R/Delimited/Other). Returns
    [(url, file_name), ...]; file_name is synthesized (studyId-version-
    format.ext) since the real server-assigned name is only known once the
    download actually succeeds."""
    soup = BeautifulSoup(html, "html.parser")
    study_id = extract_study_id(project_url) or "study"
    results = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        version = data.get("version") or ""
        for dist in data.get("distribution") or []:
            if not isinstance(dist, dict):
                continue
            url = dist.get("contentURL")
            if not url:
                continue
            fmt = re.sub(r"[^A-Za-z0-9]+", "", str(dist.get("fileFormat") or "file")).lower()
            encoding = str(dist.get("encodingFormat") or "")
            ext = "zip" if "zip" in encoding.lower() else "dat"
            name_parts = [p for p in (study_id, version, fmt) if p]
            file_name = "-".join(name_parts) + f".{ext}"
            results.append((url, file_name))
    return results


def extract_filename_from_link(abs_url, link_text=""):
    """Return the real file name (e.g. "37853-0001-Data.dta") behind a link,
    or None if nothing on it matches a known data/documentation extension."""
    if not abs_url or abs_url.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
        return None

    parsed = urlparse(abs_url)
    candidates = [unquote(os.path.basename(parsed.path))]
    for values in parse_qs(parsed.query).values():
        candidates.extend(unquote(v) for v in values)
    candidates.append(link_text or "")

    for candidate in candidates:
        match = _FILENAME_RE.search(candidate)
        if match:
            return match.group(0)
    return None


def extract_file_links(html, base_url):
    """Returns [(absolute_url, file_name), ...], deduplicated by file_name."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        abs_url = urljoin(base_url, href)
        link_text = a.get_text(" ", strip=True)
        filename = extract_filename_from_link(abs_url, link_text)
        if not filename or filename in seen:
            continue
        seen.add(filename)
        results.append((abs_url, filename))
    return results


def scrape_project_files(browser_context, project_url):
    doc_url = build_documentation_url(project_url)
    page = browser_context.new_page()
    try:
        page.goto(doc_url, wait_until="networkidle", timeout=60000)
        time.sleep(1.5)  # let client-rendered file lists finish painting
        html = page.content()
    except Exception as exc:
        log.warning("[WARN] Failed to load %s: %s", doc_url, exc)
        return []
    finally:
        page.close()

    results = []
    seen = set()
    parsed_host = urlparse(doc_url).netloc.lower()
    if any(h in parsed_host for h in JSON_LD_HOSTS):
        for url, name in extract_distribution_files(html, project_url):
            if name not in seen:
                seen.add(name)
                results.append((url, name))
    for url, name in extract_file_links(html, doc_url):
        if name not in seen:
            seen.add(name)
            results.append((url, name))
    return results


# ============================================================
# DOWNLOAD
# ============================================================

DOWNLOAD_TIMEOUT_MS = 60000


def sanitize_relative_path(rel_path):
    """PROJECTS.download_project_folder stores the raw project title, which
    can contain characters Windows won't allow in a folder name (":" etc).
    Sanitize each path segment while preserving the "/" structure."""
    rel_path = (rel_path or "").strip().replace("\\", "/")
    parts = [p for p in rel_path.split("/") if p not in ("", ".")]
    cleaned_parts = []
    for part in parts:
        cleaned = re.sub(r'[<>:"|?*]', "_", part).strip().rstrip(" .")
        cleaned_parts.append(cleaned or "_")
    return os.path.join(*cleaned_parts) if cleaned_parts else ""


def download_file(browser_context, url, destination):
    """Try to download `url` in a real browser page (needed both to clear
    the Cloudflare bot check these sites use, and because ICPSR requires an
    authenticated session for actual file downloads - a raw HTTP client
    would either get the Cloudflare challenge page or, if that's cleared,
    a redirect to login.icpsr.umich.edu). Returns True only on a real,
    non-empty file download. If `destination` already exists on disk from
    a previous run, it's left alone and this returns True without
    re-downloading."""
    if os.path.exists(destination) and os.path.getsize(destination) > 0:
        return True

    page = browser_context.new_page()
    try:
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=DOWNLOAD_TIMEOUT_MS)
                except Exception:
                    pass  # navigation is expected to abort in favor of the download
            download = dl_info.value
        except PlaywrightTimeoutError:
            return False

        os.makedirs(os.path.dirname(destination), exist_ok=True)
        download.save_as(destination)
        return os.path.exists(destination) and os.path.getsize(destination) > 0
    finally:
        page.close()


# ============================================================
# ORCHESTRATION
# ============================================================

def run(project_id=None, dry_run=False, db_path=DEFAULT_DB_PATH, missing_only=True):
    if sync_playwright is None:
        raise RuntimeError(f"Playwright is required to list ICPSR files: {PLAYWRIGHT_IMPORT_ERROR}")

    conn = connect(db_path)
    rows = fetch_repo15_projects(conn, project_id=project_id, missing_only=missing_only)
    log.info("[INFO] %d repository-15 project(s) to process", len(rows))

    with sync_playwright() as p:
        # headless=False + AutomationControlled disabled: needed to pass the
        # Cloudflare challenge these sites serve to plain/headless clients.
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US", accept_downloads=True)
        try:
            for pid, project_url, title, download_project_folder in rows:
                if not project_url:
                    continue
                log.info("[INFO] Project %s: %s", pid, project_url)
                try:
                    file_links = scrape_project_files(context, project_url)
                except Exception as exc:
                    log.error("[ERROR] Project %s failed: %s", pid, exc)
                    continue

                log.info("[FOUND] project %s: %d file(s)", pid, len(file_links))
                if dry_run:
                    for _url, name in file_links:
                        log.info("    - %s", name)
                    continue

                folder = os.path.join(PROJECT_ROOT, sanitize_relative_path(download_project_folder))
                for url, name in file_links:
                    if file_exists(conn, pid, name):
                        continue
                    destination = os.path.join(folder, name)
                    try:
                        downloaded = download_file(context, url, destination)
                    except Exception as exc:
                        log.warning("[WARN] Download failed for %s: %s", url, exc)
                        downloaded = False
                    status = "SUCCEEDED" if downloaded else "FAILED_LOGIN_REQUIRED"
                    insert_file_if_missing(conn, pid, name, status=status)
                    log.info("[%s] %s", status, name)
        finally:
            context.close()
            browser.close()

    conn.close()
    log.info("[DONE]")


def main():
    parser = argparse.ArgumentParser(description="List + download repository-15 (ICPSR) project files by scraping their pages directly (no API, no login)")
    parser.add_argument("--project-id", type=int, default=None, help="Process only this PROJECTS.id (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Scrape + print only, no DB writes")
    parser.add_argument("--all", action="store_true", help="Process all repo-15 projects, not just ones with no FILES rows yet")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Path to the classification SQLite DB")
    args = parser.parse_args()
    run(project_id=args.project_id, dry_run=args.dry_run, db_path=args.db, missing_only=not args.all)


if __name__ == "__main__":
    main()
