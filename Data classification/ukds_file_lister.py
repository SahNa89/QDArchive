"""
ukds_file_lister.py
======================

Repository 3 (UK Data Service, datacatalogue.ukdataservice.ac.uk) file
listing + download. Companion to icpsr_file_lister.py - same approach,
applied to UK Data Service's catalogue instead of ICPSR's.

datacatalogue.ukdataservice.ac.uk is a client-rendered React SPA (its
search/study pages return an near-empty HTML shell to a plain HTTP client -
confirmed separately), so this opens each project's page in a real browser
(Playwright) and reads whatever downloadable-file links appear once the
page - and its "#documentation" tab, per DataPreProceesing.py's
build_documentation_url convention for repository_id == 3 - finishes
rendering.

Unlike ICPSR, these pages don't embed a schema.org JSON-LD "distribution"
array of direct file URLs (see DataPreProceesing.py's module notes), so
this only reads plain <a href> links whose target resolves to a name ending
in a known data/documentation extension (Data acquisition/QDAFileExtensions.csv)
- the same rule and the same extension list icpsr_file_lister.py uses for
its own link-scraping fallback (imported from there directly, not
reimplemented).

Most UK Data Service studies require a registered/logged-in session, and
Special Licence / Safeguarded studies additionally require an approved
application - same situation as ICPSR, so most downloads here will
legitimately come back FAILED_LOGIN_REQUIRED. This script does not attempt
to log in.

Usage:
    python ukds_file_lister.py                   # all repo-3 projects
    python ukds_file_lister.py --project-id 42    # just one project
    python ukds_file_lister.py --all              # re-process even projects that already have FILES rows
    python ukds_file_lister.py --dry-run          # scrape + print only, no DB writes and no downloads
"""

import argparse
import logging
import os
import time

from icpsr_file_lister import (
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    connect,
    download_file,
    extract_file_links,
    file_exists,
    insert_file_if_missing,
    sanitize_relative_path,
)

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # pragma: no cover - runtime dependency guard
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = exc
else:
    PLAYWRIGHT_IMPORT_ERROR = None

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("ukds_file_lister")

REPOSITORY_ID = 3
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
)


def build_documentation_url(project_url):
    """Same convention as DataPreProceesing.py's build_documentation_url for
    repository_id == 3: strip any existing fragment and add "#documentation"
    to land on the tab that lists downloadable files."""
    if not project_url:
        return None
    base = project_url.strip().split("#")[0].rstrip("/")
    return f"{base}#documentation"


def fetch_repo3_projects(conn, project_id=None, missing_only=False):
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


def scrape_project_files(browser_context, project_url):
    doc_url = build_documentation_url(project_url)
    page = browser_context.new_page()
    try:
        page.goto(doc_url, wait_until="networkidle", timeout=60000)
        time.sleep(1.5)  # let the client-rendered documentation tab finish painting
        html = page.content()
    except Exception as exc:
        log.warning("[WARN] Failed to load %s: %s", doc_url, exc)
        return []
    finally:
        page.close()

    results = []
    seen = set()
    for url, name in extract_file_links(html, doc_url):
        if name not in seen:
            seen.add(name)
            results.append((url, name))
    return results


def run(project_id=None, dry_run=False, db_path=DEFAULT_DB_PATH, missing_only=True):
    if sync_playwright is None:
        raise RuntimeError(f"Playwright is required to list UK Data Service files: {PLAYWRIGHT_IMPORT_ERROR}")

    conn = connect(db_path)
    rows = fetch_repo3_projects(conn, project_id=project_id, missing_only=missing_only)
    log.info("[INFO] %d repository-3 project(s) to process", len(rows))

    with sync_playwright() as p:
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
    parser = argparse.ArgumentParser(description="List + download repository-3 (UK Data Service) project files by scraping their pages directly (no API, no login)")
    parser.add_argument("--project-id", type=int, default=None, help="Process only this PROJECTS.id (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Scrape + print only, no DB writes")
    parser.add_argument("--all", action="store_true", help="Process all repo-3 projects, not just ones with no FILES rows yet")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Path to the classification SQLite DB")
    args = parser.parse_args()
    run(project_id=args.project_id, dry_run=args.dry_run, db_path=args.db, missing_only=not args.all)


if __name__ == "__main__":
    main()
