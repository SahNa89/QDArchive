import os
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from urllib.parse import urljoin
import requests
import csv
from parser import insert_file
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db_path = os.path.join(BASE_DIR, "PipelineScript", "23273412-seeding.db")

def load_extensions():
    with open("QDAFileExtensions.csv", newline="") as f:
        return [r["extensions"].lower().strip() for r in csv.DictReader(f)]

def load_Attext():
    with open("QDAFileExtensions.csv", newline="") as f:
        return [r["AttFormat"].lower().strip() for r in csv.DictReader(f)]

extensions = load_extensions()
attext = load_Attext()

BASE_URL_UK = "https://datacatalogue.ukdataservice.ac.uk"
ROOT = r"D:\Desktop\FAUUni\Adv\QDArchive\My downloads"

REPO_FOLDER_MAP = {
    "15": "icpsr",
    "3": "ukdataservice",
}

def make_folder(repo_id, title):
    repo_name = REPO_FOLDER_MAP.get(str(repo_id), f"repo_{repo_id}")
    safe_title = "".join(
        c for c in title[:200].strip()
        if c not in r'\/:*?"<>|'
    ).strip()
    path = os.path.join(ROOT, repo_name, safe_title)
    os.makedirs(path, exist_ok=True)
    print(f"Folder created: {path}")
    return path


def download(url, folder, filename):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)

    # ── DUPLICATE CHECK ──────────────────────────────────────
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"    ⟳ Skipped (already exists): {filename}")
        return "SKIPPED"

    try:
        r = requests.get(url, timeout=60, stream=True)
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        print(f"    ✓ Downloaded: {filename}")
        return "SUCCEEDED"
    except Exception as e:
        print(f"    ✗ Failed: {filename} — {e}")
        return 'FAILED_LOGIN_REQUIRED'


def scrape_links(page, project_url):
    """Navigate to a project URL and return all downloadable file links."""
    try:
        page.goto(project_url, wait_until="networkidle", timeout=60000)
        html = page.content()
    except Exception as e:
        print(f"  ✗ Failed to load page {project_url}: {e}")
        return set()

    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE_URL_UK, a["href"])
        if any(ext in url.lower() for ext in attext + extensions):
            links.add(url)
    return links


def process_projects(browser, rows, repo_id):
    """Scrape and download files for all projects in a repository."""
    for id, project_url, title in rows:
        print(f"\n{'='*60}")
        print(f"  Project : {title[:200]}")
        print(f"  URL     : {project_url}")
        print(f"{'='*60}")

        # Fix URL suffix
        if project_url.endswith("#doi"):
            project_url = project_url[:-4] + "#documentation"

        # Build folder
        folder = make_folder(repo_id, title)

        # --- SCRAPING PHASE ---
        page = browser.new_page()
        links = scrape_links(page, project_url)
        page.close()  # Close page as soon as scraping is done

        if not links:
            print(f"  No downloadable files found, skipping.")
            continue

        print(f"  Found {len(links)} file(s) to download.")

        # --- DOWNLOADING PHASE ---
        succeeded = 0
        failed = 0
        skipped = 0
        login_required =0
        for url in links:
            filename = url.split("/")[-1]
            print(f"  → Downloading: {filename}")
            status = download(url, folder, filename)

            if status == "SUCCEEDED":
                succeeded += 1
                insert_file(id, filename, filename.split(".")[-1], status)
            elif status == "SKIPPED":
                skipped += 1
            elif status == "FAILED_LOGIN_REQUIRED":
                login_required += 1
                insert_file(id, filename, filename.split(".")[-1], "FAILED_LOGIN_REQUIRED")
            else:
                failed += 1
                insert_file(id, filename, filename.split(".")[-1], "FAILED_SERVER_UNRESPONSIVE")

        # --- SUMMARY PER PROJECT ---
        print(f"\n  ✔ Done — {succeeded} succeeded, {failed} failed.")


def run():

    REPOSITORY_IDS = ["3", "15"]

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Launch browser ONCE for the entire run
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for repo_id in REPOSITORY_IDS:
            print(f"\n{'#'*60}")
            print(f"  Processing repository: {REPO_FOLDER_MAP.get(repo_id, repo_id)}")
            print(f"{'#'*60}")

            cursor.execute(
                "SELECT id, project_url, title FROM PROJECTS WHERE repository_id = ?",
                (repo_id,)
            )
            rows = cursor.fetchall()
            conn.close()
            if not rows:
                print(f"  No projects found for repository_id={repo_id}")
                continue

            print(f"  {len(rows)} project(s) found.\n")
            process_projects(browser, rows, repo_id)

        browser.close()


    print("\nPipeline completed.")


if __name__ == "__main__":
    run()