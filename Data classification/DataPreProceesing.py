"""
DataPreProcessing.py
=====================

Pipeline:
  1. Data-integrity pass on the classification SQLite DB (unchanged from your
     original script): find/remove duplicates, find/repair missing relations,
     add classification columns.
  2. For every project row (repository_id 15 = ICPSR/openICPSR/CFDA,
     repository_id 3 = UK Data Service), build the "documentation" page URL,
     scrape it for downloadable file links, download anything missing into
     a per-project folder, and record each downloaded file in FILES.
  3. Remove any folders left empty at the end.

WHERE TO PUT YOUR CREDENTIALS


IMPORTANT CAVEATS (please read)
--------------------------------
- The ICPSR "Object-Export API" (dir.api.it.umich.edu) is a *metadata* search
  /export API (titles, DOIs, subjects, etc.) - it does not serve the actual
  data/documentation files. So it is NOT used to download files. Public
  "datadocumentation" pages are scraped directly with `requests`; only if a
  study turns out to be access-restricted does the script fall back to
  driving your real, already-logged-in Chrome profile with Selenium.
  `ICPSR_CLIENT_ID`/`ICPSR_CLIENT_SECRET` are still wired in in case you want
  to add metadata lookups later.
- UK Data Service's login is handled by a separate identity provider
  (Jisc/UKDS single sign-on), not a plain HTML form. The `get_ukds_session()`
  login attempt below is best-effort: if it doesn't work, open your browser's
  Network tab while logging in manually and adjust `LOGIN_URL` and the
  submitted field names to match what you see.
- Selenium + a Chrome driver must be installed on the machine that actually
  runs this (`pip install selenium`, plus chromedriver matching your Chrome
  version) - only needed for the repository-15 login fallback.
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from license_normalizer import normalize_license

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # pragma: no cover - runtime dependency guard
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = exc
else:
    PLAYWRIGHT_IMPORT_ERROR = None


# ============================================================
# CONFIG
# ============================================================

DATA_CLASSIFICATION_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(DATA_CLASSIFICATION_DIR)
DB_NAME = "23273412-sq26-classification.db"
DB_PATH = os.path.join(PROJECT_ROOT, DB_NAME)
DOWNLOAD_ROOT = os.path.join(PROJECT_ROOT, "My downloads")
REPOSITORY_DOWNLOAD_FOLDERS = {
    15: os.path.join(DOWNLOAD_ROOT, "icpsr"),
    3: os.path.join(DOWNLOAD_ROOT, "ukdataservice"),
}

# A bare "Mozilla/5.0" user agent (no OS/browser details) is a strong bot
# signal - Cloudflare keeps re-challenging a session presenting one, which
# is why page.goto(..., wait_until="networkidle") was timing out (network
# activity never actually settled). This is the same full, realistic UA
# string icpsr_file_lister.py already uses successfully against these
# Cloudflare-protected pages.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
)


print(DB_NAME, "DB_NAME", DB_PATH, "DB_PATH", PROJECT_ROOT, "PROJECT_ROOT")


def connect():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"Database not found at {DB_PATH}. Make sure the classification "
            f"database exists before running this script."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


# ============================================================
# DATA INTEGRITY: DUPLICATE CHECKS
# ============================================================

def check_duplicates(conn):
    print("\n================ DUPLICATE CHECKS ================\n")

    queries = {
        "PROJECTS": """
            SELECT query_string, repository_id, repository_url, project_url,
                   version, title, description, language, doi,
                   upload_date, download_date,
                   download_repository_folder,
                   download_project_folder,
                   download_version_folder,
                   download_method,
                   COUNT(*) as cnt
            FROM PROJECTS
            GROUP BY query_string, repository_id, repository_url, project_url,
                     version, title, description, language, doi,
                     upload_date, download_date,
                     download_repository_folder,
                     download_project_folder,
                     download_version_folder,
                     download_method
            HAVING cnt > 1
        """,
        "FILES": """
            SELECT project_id, file_name, file_type, status, COUNT(*) as cnt
            FROM FILES
            GROUP BY project_id, file_name, file_type, status
            HAVING cnt > 1
        """,
        "KEYWORDS": """
            SELECT project_id, keyword, COUNT(*) as cnt
            FROM KEYWORDS
            GROUP BY project_id, keyword
            HAVING cnt > 1
        """,
        "PERSON_ROLE": """
            SELECT project_id, name, role, COUNT(*) as cnt
            FROM PERSON_ROLE
            GROUP BY project_id, name, role
            HAVING cnt > 1
        """,
        "LICENSES": """
            SELECT project_id, license, COUNT(*) as cnt
            FROM LICENSES
            GROUP BY project_id, license
            HAVING cnt > 1
        """,
    }

    for table, query in queries.items():
        rows = conn.execute(query).fetchall()
        print(f"\n--- {table} duplicates ---")
        if not rows:
            print("No duplicates found")
        else:
            for r in rows:
                print(r)


def check_missing_relations(conn):
    print("\n================ MISSING RELATIONS CHECK ================\n")

    project_ids = [row[0] for row in conn.execute("SELECT id FROM PROJECTS").fetchall()]

    missing = {"FILES": [], "KEYWORDS": [], "PERSON_ROLE": [], "LICENSES": []}

    for pid in project_ids:
        if conn.execute("SELECT 1 FROM FILES WHERE project_id=? LIMIT 1", (pid,)).fetchone() is None:
            missing["FILES"].append(pid)
        if conn.execute("SELECT 1 FROM KEYWORDS WHERE project_id=? LIMIT 1", (pid,)).fetchone() is None:
            missing["KEYWORDS"].append(pid)
        if conn.execute("SELECT 1 FROM PERSON_ROLE WHERE project_id=? LIMIT 1", (pid,)).fetchone() is None:
            missing["PERSON_ROLE"].append(pid)
        if conn.execute("SELECT 1 FROM LICENSES WHERE project_id=? LIMIT 1", (pid,)).fetchone() is None:
            missing["LICENSES"].append(pid)

    for table, ids in missing.items():
        print(f"\n--- Projects missing in {table} ---")
        if not ids:
            print("All projects have at least one record")
        else:
            print(ids)


def remove_duplicates(conn):
    print("\n================ REMOVING DUPLICATES ================\n")
    cur = conn.cursor()

    project_groups = cur.execute("""
        SELECT MIN(id) as keep_id, GROUP_CONCAT(id) as ids
        FROM PROJECTS
        GROUP BY query_string, repository_id, repository_url, project_url,
                 version, title, description, language, doi,
                 upload_date, download_date,
                 download_repository_folder,
                 download_project_folder,
                 download_version_folder,
                 download_method
        HAVING COUNT(*) > 1
    """).fetchall()

    for keep_id, ids in project_groups:
        id_list = [int(x) for x in ids.split(',') if int(x) != keep_id]
        for dup_id in id_list:
            cur.execute("UPDATE FILES SET project_id = ? WHERE project_id = ?", (keep_id, dup_id))
            cur.execute("UPDATE KEYWORDS SET project_id = ? WHERE project_id = ?", (keep_id, dup_id))
            cur.execute("UPDATE PERSON_ROLE SET project_id = ? WHERE project_id = ?", (keep_id, dup_id))
            cur.execute("UPDATE LICENSES SET project_id = ? WHERE project_id = ?", (keep_id, dup_id))
            cur.execute("DELETE FROM PROJECTS WHERE id = ?", (dup_id,))
        conn.commit()

    other_tables = {
        "FILES": ["project_id", "file_name", "file_type", "status"],
        "KEYWORDS": ["project_id", "keyword"],
        "PERSON_ROLE": ["project_id", "name", "role"],
    }

    for table, cols in other_tables.items():
        cols_sql = ", ".join(cols)
        del_sql = f"""
            DELETE FROM {table}
            WHERE id NOT IN (
                SELECT MIN(id) FROM {table} GROUP BY {cols_sql}
            )
        """
        cur.execute(del_sql)
        conn.commit()

    license_rows = cur.execute("""
        SELECT project_id, license, id
        FROM LICENSES
        ORDER BY project_id, license, id
    """).fetchall()

    keep_license_ids = []
    seen_projects = set()
    for project_id, license_text, row_id in license_rows:
        if project_id in seen_projects:
            continue
        seen_projects.add(project_id)
        keep_license_ids.append(row_id)

    if keep_license_ids:
        cur.execute(
            f"DELETE FROM LICENSES WHERE id NOT IN ({','.join('?' for _ in keep_license_ids)})",
            keep_license_ids,
        )
        conn.commit()

    print("[DONE] Removed duplicate rows where applicable")


# ============================================================
# DATABASE HELPERS
# ============================================================

def project_exists(conn, repository_id, project_url):
    cur = conn.execute(
        "SELECT id FROM PROJECTS WHERE repository_id = ? AND project_url = ?",
        (repository_id, project_url),
    )
    return cur.fetchone()


def keyword_exists(conn, project_id, keyword):
    cur = conn.execute(
        "SELECT 1 FROM KEYWORDS WHERE project_id = ? AND keyword = ?",
        (project_id, keyword),
    )
    return cur.fetchone()


def license_exists(conn, project_id, license_text):
    cur = conn.execute(
        "SELECT 1 FROM LICENSES WHERE project_id = ? AND license = ?",
        (project_id, license_text),
    )
    return cur.fetchone()


def file_exists(conn, project_id, file_name):
    cur = conn.execute(
        "SELECT 1 FROM FILES WHERE project_id = ? AND file_name = ?",
        (project_id, file_name),
    )
    return cur.fetchone()


def _normalize_folder_path(value):
    return (value or "").replace("\\", "/").strip()


def _normalize_status(status):
    allowed_statuses = {
        "SUCCEEDED",
        "FAILED_SERVER_UNRESPONSIVE",
        "FAILED_LOGIN_REQUIRED",
        "FAILED_TOO_LARGE",
    }
    return status if status in allowed_statuses else "SUCCEEDED"


def _insert_file_if_missing(conn, project_id, file_name, status="SUCCEEDED"):
    file_name = _normalize_folder_path(file_name)
    if not file_name:
        return False
    if file_exists(conn, project_id, file_name):
        return False
    ext = os.path.splitext(file_name)[1].lstrip(".").lower() or "unknown"
    status = _normalize_status(status)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO FILES (project_id, file_name, file_type, status) VALUES (?, ?, ?, ?)",
        (project_id, file_name, ext, status),
    )
    conn.commit()
    return True


def _clean_download_filename(value):
    if not value:
        return None
    value = (value or "").strip().strip('"\'')
    value = unquote(value)
    value = value.split(";")[0].split("?")[0].split("#")[0]
    value = value.replace("\\", "/")
    value = os.path.basename(value)
    if not value or value in {"", ".", "..", "download", "file", "downloaded_file", "index", "view", "details"}:
        return None
    return value


def _extract_filename_from_url(url, link_text=None):
    parsed = urlparse(url or "")
    query_values = parse_qs(parsed.query)

    for key in ("filename", "file", "name", "download", "path"):
        values = query_values.get(key, [])
        for value in values:
            candidate = _clean_download_filename(value)
            if candidate:
                return candidate

    if parsed.path:
        candidate = _clean_download_filename(parsed.path)
        if candidate:
            return candidate

    if link_text:
        candidate = _clean_download_filename(link_text)
        if candidate:
            return candidate

    return None


def _looks_like_download_link(url, link_text=""):
    if not url:
        return False
    lower_url = url.lower()
    lower_text = (link_text or "").lower()

    if lower_url.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False

    if any(token in lower_url for token in ("/download", "/file", "/attachment", "/files", "/content", "/resource", "/agreement", "/access", "/login")):
        return True
    if any(token in lower_text for token in ("download", "file", "pdf", "zip", "csv", "xlsx", "xls", "sav", "dta", "sas", "json", "xml", "txt", "agreement", "access", "login")):
        return True

    parsed = urlparse(url)
    if not parsed.path or parsed.path in {"/", ""}:
        return False

    name = os.path.basename(parsed.path)
    if not name or name.lower() in {"download", "file", "view", "details", "documentation", "index", "home", "page", "study"}:
        return False

    return True


def _download_file_with_browser(page, url, folder_path, filename):
    """Download `url` through the active browser session into folder_path."""
    os.makedirs(folder_path, exist_ok=True)

    if not filename:
        filename = "downloaded_file"

    destination = os.path.join(folder_path, filename)
    if os.path.exists(destination) and os.path.getsize(destination) > 0:
        return "SUCCEEDED"

    try:
        with page.expect_download(timeout=120000) as download_info:
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
        download = download_info.value
        download.save_as(destination)
        if not os.path.exists(destination) or os.path.getsize(destination) == 0:
            if os.path.exists(destination):
                os.remove(destination)
            return "FAILED_SERVER_UNRESPONSIVE"
        return "SUCCEEDED"
    except Exception as exc:
        print(f"[ERROR] Download failed for {url}: {exc}")
        return "FAILED_SERVER_UNRESPONSIVE"


# ============================================================
# URL / FOLDER RULES
# ============================================================

def sanitize_title_for_folder(title):
    """First 200 chars of the title, made safe as a Windows folder name."""
    if not title:
        title = "untitled_project"
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned[:200].rstrip(" .")
    return cleaned or "untitled_project"


def get_project_folder(repository_id, title):
    base = REPOSITORY_DOWNLOAD_FOLDERS.get(repository_id)
    if base is None:
        raise ValueError(f"Unknown repository_id {repository_id}")
    return os.path.join(base, sanitize_title_for_folder(title))


def build_documentation_url(repository_id, project_url):
    """Turn a raw project URL into the 'documentation' page URL to scrape."""
    if not project_url:
        return None
    url = project_url.strip()
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if repository_id == 15:
        if "icpsr.umich.edu" in host or "childandfamilydataarchive.org" in host:
            path = re.sub(r"/+", "/", parsed.path).rstrip("/")
            if not path.endswith("/datadocumentation"):
                path = path + "/datadocumentation"
            return f"{parsed.scheme}://{parsed.netloc}{path}"
        print(f"[WARN] Unrecognized repository-15 host for url: {url}")
        return url

    if repository_id == 3:
        if "datacatalogue.ukdataservice.ac.uk" in host:
            base = url.split("#")[0].rstrip("/")
            return f"{base}#documentation"
        print(f"[WARN] Unrecognized repository-3 host for url: {url}")
        return url

    return url


# ============================================================
# SESSIONS / AUTH
# ============================================================

def get_icpsr_session():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    return session



def fetch_with_chrome_profile(url):
    """Fallback for restricted ICPSR pages using Selenium Chrome.

    This uses a headless, sandbox-safe setup and a temporary profile so it
    avoids common startup crashes such as the DevToolsActivePort error.
    """
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options

    def build_options(profile_dir=None):
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--remote-debugging-port=0")
        options.add_argument("--disable-extensions")
        if profile_dir:
            options.add_argument(f"user-data-dir={profile_dir}")
        return options


# ============================================================
# SYNC FILES FROM DISK (My downloads/icpsr, My downloads/ukdataservice)
# ============================================================

def _remove_illegal_chars_for_folder(title):
    """Crawler.py's make_folder() scheme: strips filesystem-illegal
    characters out entirely (no "_" replacement). Some project folders on
    disk were created with this scheme rather than sanitize_title_for_folder's,
    so both are tried when matching a title back to its folder."""
    return "".join(c for c in (title or "")[:200].strip() if c not in r'\/:*?"<>|').strip()


def _fuzzy_folder_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _match_project_folder(title, disk_dir_names, fuzzy_index):
    for candidate in (_remove_illegal_chars_for_folder(title), sanitize_title_for_folder(title)):
        if candidate in disk_dir_names:
            return candidate
    return fuzzy_index.get(_fuzzy_folder_key(title))


def _resolve_stored_project_folder(download_project_folder):
    """PROJECTS.download_project_folder as literally stored, resolved
    against PROJECT_ROOT - used as-is when it exists (~53% of repo-3/15
    projects, checked directly against disk). Returns None if it doesn't
    exist, so the caller can fall back to name-matching."""
    if not download_project_folder:
        return None
    path = os.path.join(PROJECT_ROOT, _normalize_folder_path(download_project_folder))
    return path if os.path.isdir(path) else None


def sync_files_from_disk(conn, repository_id=None):
    """Walk My downloads/icpsr and My downloads/ukdataservice and make sure
    every file actually sitting on disk has a matching FILES row - checked
    file by file (not skipped just because the project already has some
    FILES rows, unlike the old repair-missing-relations pass).

    Folder lookup: PROJECTS.download_project_folder is tried first, as-is
    (no changes made to it or to anything on disk). It stores the raw
    project title though, which often contains characters Windows won't
    allow in a folder name (":" etc, e.g. "ETA 9161: Self Employment..."),
    so it doesn't exist literally for ~47% of repo-3/15 projects (whatever
    process actually created the folder sanitized those characters first).
    Only for those is a sanitized/fuzzy match against the title used to
    locate the real folder - this is purely a lookup fallback to find where
    to scan; nothing is renamed or deleted either way.
    """
    print("\n================ SYNCING FILES FROM DISK ================\n")

    where = "repository_id IN (15, 3)"
    params = []
    if repository_id is not None:
        where = "repository_id = ?"
        params.append(repository_id)
    projects = conn.execute(
        f"SELECT id, repository_id, title, download_project_folder FROM PROJECTS WHERE {where}", params
    ).fetchall()

    repo_disk_index = {}
    for rid in {row[1] for row in projects}:
        repo_folder = REPOSITORY_DOWNLOAD_FOLDERS.get(rid)
        names = os.listdir(repo_folder) if repo_folder and os.path.isdir(repo_folder) else []
        dir_names = [n for n in names if os.path.isdir(os.path.join(repo_folder, n))]
        repo_disk_index[rid] = {
            "path": repo_folder,
            "dirs": set(dir_names),
            "fuzzy": {_fuzzy_folder_key(n): n for n in dir_names},
        }

    matched_projects = 0
    matched_via_stored_path = 0
    unmatched_projects = []
    inserted_files = 0
    scanned_files = 0

    for project_id, rid, title, download_project_folder in projects:
        project_folder = _resolve_stored_project_folder(download_project_folder)
        if project_folder:
            matched_via_stored_path += 1
        else:
            index = repo_disk_index.get(rid)
            if not index or not index["dirs"]:
                unmatched_projects.append((project_id, rid, title))
                continue
            folder_name = _match_project_folder(title, index["dirs"], index["fuzzy"])
            if not folder_name:
                unmatched_projects.append((project_id, rid, title))
                continue
            project_folder = os.path.join(index["path"], folder_name)

        matched_projects += 1

        for dirpath, _dirnames, filenames in os.walk(project_folder):
            for name in filenames:
                full_path = os.path.join(dirpath, name)
                if not os.path.isfile(full_path) or os.path.getsize(full_path) == 0:
                    continue
                scanned_files += 1
                if _insert_file_if_missing(conn, project_id, name, status="SUCCEEDED"):
                    inserted_files += 1

    print(f"[DONE] {matched_projects} project folder(s) matched ({matched_via_stored_path} via stored "
          f"download_project_folder, {matched_projects - matched_via_stored_path} via name matching), "
          f"{len(unmatched_projects)} unmatched, {scanned_files} file(s) on disk scanned, "
          f"{inserted_files} new FILES row(s) inserted")
    if unmatched_projects:
        print("[INFO] Sample unmatched projects (up to 10):")
        for project_id, rid, title in unmatched_projects[:10]:
            print(f"    id={project_id} repo={rid} title={title[:100]!r}")
    return inserted_files


# ============================================================
# LICENSE REPAIR (Notes on access: / Copyright: / conditionsOfAccess)
# ============================================================

# Signals that a stored LICENSES.license value is not actually a license,
# but a full project description, a leaked JSON/HTML fragment, or a bare
# archive-name string UK Data Service itself puts in its own "license"
# field (e.g. "Copyright National Social Policy and Social Change Archive").
_SUSPICIOUS_LICENSE_MARKERS = ('","', '"@type"', 'includedindatacatalog', '<p>', '<a href')


def is_suspicious_license(text):
    if not text or not text.strip():
        return True
    t = text.strip()
    if len(t) > 350:
        return True
    lower = t.lower()
    if any(marker in lower for marker in _SUSPICIOUS_LICENSE_MARKERS):
        return True
    if lower.startswith("copyright ") and not any(
        kw in lower for kw in ("agreement", "licence", "license", "creative commons", "terms of", "©", "crown copyright")
    ):
        return True
    if "currently embargoed" in lower:
        return True
    return False


def extract_access_fields_from_eprints_html(html_text):
    """reshare.ukdataservice.ac.uk (EPrints) study pages render an "Access
    and Administration" table of <th>Label:</th><td>Value</td> rows. Reads
    "Notes on access:" and "Copyright:" specifically - deliberately NOT
    "Citation:", which holds the bibliographic citation string, not
    license/access terms."""
    soup = BeautifulSoup(html_text, "html.parser")
    found = {}
    for th in soup.find_all("th"):
        label = th.get_text(" ", strip=True)
        if label not in ("Notes on access:", "Copyright:"):
            continue
        td = th.find_next_sibling("td")
        if not td:
            continue
        value = re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip()
        if value:
            found[label] = value
    return found


def extract_conditions_of_access_from_jsonld(html_text):
    """icpsr.umich.edu / discover.ukdataservice.ac.uk / openicpsr.org embed a
    schema.org Dataset JSON-LD block. Its "conditionsOfAccess" field holds
    real prose about access/licensing terms; "license" is sometimes just a
    bare terms-page URL, or on UK Data Service, literally the depositing
    archive's name - so conditionsOfAccess is preferred here, and "license"
    is only ever used as a last-resort fallback by the caller."""
    soup = BeautifulSoup(html_text, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        conditions = data.get("conditionsOfAccess")
        license_field = data.get("license")
        conditions_text = re.sub(r"<[^>]+>", " ", conditions).strip() if isinstance(conditions, str) else ""
        conditions_text = re.sub(r"\s+", " ", conditions_text).strip()
        license_text = re.sub(r"\s+", " ", license_field).strip() if isinstance(license_field, str) else ""
        if conditions_text or license_text:
            return conditions_text, license_text
    return "", ""


def find_license_repair_candidates(conn, repository_id=None):
    """Projects with no LICENSES row at all, or whose existing row(s) look
    suspicious (see is_suspicious_license)."""
    where = "repository_id IN (15, 3)"
    params = []
    if repository_id is not None:
        where = "repository_id = ?"
        params.append(repository_id)
    projects = conn.execute(f"SELECT id, repository_id, project_url, title FROM PROJECTS WHERE {where}", params).fetchall()

    candidates = []
    for project_id, rid, project_url, title in projects:
        license_rows = conn.execute("SELECT id, license FROM LICENSES WHERE project_id = ?", (project_id,)).fetchall()
        if not license_rows:
            candidates.append((project_id, rid, project_url, title, []))
            continue
        bad_ids = [row_id for row_id, text in license_rows if is_suspicious_license(text)]
        if bad_ids:
            candidates.append((project_id, rid, project_url, title, bad_ids))
    return candidates


def wait_past_cloudflare(page, max_wait_seconds=20):
    """icpsr.umich.edu (and other ICPSR/UKDS-family sites) show a Cloudflare
    "Verifying you are human" interstitial that auto-resolves in a real
    browser after a few seconds - but it resolves *after* the page already
    reports network-idle, so a short fixed sleep isn't reliable. Poll the
    page title for the challenge marker to clear instead of guessing a
    fixed delay."""
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        title = (page.title() or "").lower()
        if "just a moment" not in title and "verifying you are human" not in title:
            return True
        time.sleep(1)
    return False


def repair_licenses(conn, repository_id=None):
    print("\n================ REPAIRING LICENSES ================\n")
    if sync_playwright is None:
        print(f"[WARN] Playwright not available ({PLAYWRIGHT_IMPORT_ERROR}); skipping license repair")
        return 0

    candidates = find_license_repair_candidates(conn, repository_id=repository_id)
    print(f"[INFO] {len(candidates)} project(s) need a license lookup")
    if not candidates:
        return 0

    fixed = 0
    unresolved = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        try:
            for project_id, rid, project_url, title, bad_license_ids in candidates:
                doc_url = build_documentation_url(rid, project_url)
                if not doc_url:
                    continue

                page = context.new_page()
                try:
                    page.goto(doc_url, wait_until="networkidle", timeout=60000)
                    if not wait_past_cloudflare(page):
                        print(f"[WARN] Cloudflare check never cleared for {doc_url} (project {project_id})")
                    html = page.content()
                except Exception as exc:
                    print(f"[WARN] Could not load {doc_url} for project {project_id}: {exc}")
                    page.close()
                    continue
                page.close()

                eprints_fields = extract_access_fields_from_eprints_html(html)
                conditions_text, license_field_text = extract_conditions_of_access_from_jsonld(html)

                found_text = (
                    eprints_fields.get("Notes on access:")
                    or eprints_fields.get("Copyright:")
                    or conditions_text
                    or license_field_text
                )

                if not found_text or is_suspicious_license(found_text):
                    print(f"[UNRESOLVED] project {project_id} ({project_url}): nothing usable found")
                    unresolved += 1
                    continue

                normalized = normalize_license(found_text)
                license_text = normalized if normalized is not None else found_text

                for row_id in bad_license_ids:
                    conn.execute("DELETE FROM LICENSES WHERE id = ?", (row_id,))
                if not license_exists(conn, project_id, license_text):
                    conn.execute("INSERT INTO LICENSES (project_id, license) VALUES (?, ?)", (project_id, license_text))
                conn.commit()
                print(f"[FIXED] project {project_id}: {license_text[:120]!r}")
                fixed += 1
        finally:
            context.close()
            browser.close()

    print(f"\n[DONE] {fixed} project license(s) fixed, {unresolved} still unresolved")
    return fixed


# ============================================================
# DOWNLOAD ORCHESTRATION

def main():
    conn = connect()

    try:
        conn.execute(
            "ALTER TABLE PROJECTS ADD COLUMN type TEXT NOT NULL DEFAULT 'OTHER_PROJECT' "
            "CHECK (type IN ('QDA_PROJECT', 'QD_PROJECT', 'OTHER_PROJECT', 'NOT_A_PROJECT'))"
        )
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE PROJECTS ADD COLUMN class TEXT NOT NULL DEFAULT 'UNKNOWN'")
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE FILES ADD COLUMN class TEXT NOT NULL DEFAULT 'UNKNOWN'")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()
    print("\n[DONE] Schema check")

    # Data integrity check: clean database and duplicated rows
    conn = connect()
    check_duplicates(conn)
    remove_duplicates(conn)
    check_missing_relations(conn)

    sync_files_from_disk(conn)
    check_missing_relations(conn)

    repair_licenses(conn)

    conn.commit()
    conn.close()
    print("\n[DONE]")


if __name__ == "__main__":
    main()