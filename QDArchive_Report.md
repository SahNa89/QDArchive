# QDArchive — Project Report
---

## 1. Project Overview

QDArchive is a Python-based data acquisition pipeline designed to serve as a web service for publishing and archiving **Qualitative Data Analysis (QDA) files. Qualitative Data Analysis is a research methodology that helps synthesize and structure information from unstructured data through qualitative coding — labeling data so that theoretical constructs are represented as codes in a hierarchically structured code system.

The project focuses specifically on **Part 1: Data Acquisition Pipeline, with the goal of automatically discovering, downloading, and archiving QDA-related datasets from public research repositories.

---

## 2. Architecture and Components

The repository contains the following core components:

### 2.1 `scraper.py`
The metadata scraper. It queries two repository APIs — **ICPSR (via DataCite with `client-id: gesis.icpsr`) and **UK Data Service (via DataCite with `client-id: bl.ukda`) — using search terms and QDA file extensions loaded from CSV files. For each result, it extracts title, DOI, description, creators, subjects, license, and publication year, then inserts structured records into a local SQLite database.

### 2.2 `Crawler.py`
The file downloader. It reads project records from the SQLite database, navigates to each project's landing page using **Playwright (headless Chromium), scrapes all downloadable file links matching known QDA extensions, and downloads them via `requests`. Each download outcome (SUCCEEDED, SKIPPED, FAILED\_LOGIN\_REQUIRED, FAILED\_SERVER\_UNRESPONSIVE) is recorded back to the database.

### 2.3 `parser.py`
A database helper module that provides insert functions (`insert_project`, `insert_file`, `insert_KEYWORDS`, `insert_LICENSES`, `insert_PERSON_ROLE`) to write structured metadata into the SQLite database.

### 2.4 `SQLseeding.py`
Seeds the SQLite database with initial repository records from `QualitativeDataRepositories.csv`.

### 2.5 `QDATest.py`
A test/validation script for the pipeline.

### 2.6 Supporting Data Files
- `QDAFileExtensions.csv` — defines QDA-specific file extensions, attachment formats, and search queries
- `QualitativeDataRepositories.csv` — lists target repository URLs
- `SQLiteMetaDataDatabaseSchema.csv` — documents the database schema
- `23273412-seeding.db` — the pre-seeded SQLite database (committed to the repo)

---

## 3. Pipeline Flow

```
QDAFileExtensions.csv ──► scraper.py ──► SQLite DB (PROJECTS, FILES, KEYWORDS, LICENSES, PERSON_ROLE)
QualitativeDataRepositories.csv               │
                                              ▼
                                         Crawler.py ──► File Downloads (local folder)
                                              │
                                              ▼
                                    Status recorded back to DB
```

---

## 4. Known Challenges and Open Issues

The repository currently has **4 open issues, all of which represent active technical blockers in the pipeline. These are documented below.

---

### Issue #1 — Repository Access Limitations
**Opened: March 12, 2026 | **Status: Open

This is the foundational access challenge for the entire pipeline. Two of the primary target repositories have structural restrictions that prevent automated data collection:

**Databrary:
- Most datasets require institutional authorization before any access is granted.
- Files are not publicly crawlable, meaning the scraper cannot reach download links without authenticated sessions.

**UK Data Service and ICPSR:
- Many datasets require user registration and login.
- Files are exposed through the catalogue API, but not through the learning hub interface that the crawler targets.

**Impact: A significant portion of the QDA dataset universe is inaccessible to the pipeline in its current form. The pipeline can only retrieve publicly available datasets, which represents a minority of relevant holdings.

**Possible remediation: OAuth or session-based authentication flows, institutional API key agreements, or working through data access portals that provide bulk export for authorized users.

---

### Issue #2 — 401 Client Error: Unauthorized
**Opened: April 16, 2026 | **Status: Open

**Error:
```
✗ Failed: 857712_data.zip — 401 Client Error: Unauthorized for downloading urls
```

This error surfaces when `Crawler.py` attempts to download a file from the UK Data Service's ReShare platform. Even when the scraper successfully identifies a download URL, the server rejects the HTTP `GET` request with a 401 Unauthorized response. The `requests` library in `download()` makes unauthenticated HTTP calls, which fails against endpoints that require a valid session token or API credential.

**Root cause: The download function does not carry any authentication headers or cookies. The Playwright browser navigates the page (potentially accepting cookies), but when `requests` makes a separate download call, it starts a new, unauthenticated session.

**Possible remediation: Share browser cookies between Playwright and the `requests.Session`, or replace the `requests` download with a Playwright-driven download using `page.expect_download()`.

---

### Issue #3 — Max Retries Exceeded / Connection Timeout
**Opened: April 16, 2026 | **Status: Open

**Error:
```
Failed: UKDA-SN-857755 — HTTPSConnectionPool(host='reshare.ukdataservice.ac.uk', port=443):
Max retries exceeded with url: /id/eprint/857755
(Caused by ConnectTimeoutError: Connection to reshare.ukdataservice.ac.uk timed out. connect timeout=60)
```

The pipeline's HTTP requests are timing out when attempting to connect to the UK Data Service ReShare server. This may be caused by the server rate-limiting or blocking automated requests, network instability, or the server being temporarily unavailable.

**Root cause: The scraper and crawler make no provision for adaptive backoff, user-agent rotation, or request throttling beyond a basic 0.5-second sleep in `scraper.py`. A single `timeout=60` is set, but no retry strategy is implemented for connection-level failures.

**Possible remediation: Implement exponential backoff with `urllib3.util.retry.Retry`, add jitter between requests, rotate User-Agent strings, and add per-host rate limiting to avoid triggering server-side blocks.

---

### Issue #4 — API Block / Page Load Timeout (Playwright)
**Opened: April 16, 2026 | **Status: Open

**Error:
```
Failed to load page https://www.datalumos.org/datalumos/project/240510/version/V1/view:
Page.goto: Timeout 60000ms exceeded.
Call log: navigating to URL, waiting until "networkidle"
No downloadable files found, skipping.
```

When `Crawler.py` uses Playwright to navigate to a DataLumos project page, the browser times out waiting for the page to reach a `networkidle` state. This is likely because the DataLumos platform uses JavaScript-heavy rendering or actively detects and blocks headless browser traffic.

**Root cause: The `wait_until="networkidle"` strategy waits until there are no more than 2 network connections for 500ms, which can fail indefinitely on pages with background polling, analytics scripts, or anti-bot measures. DataLumos may also be blocking Playwright's default Chromium fingerprint.

**Possible remediation: Switch `wait_until` to `"domcontentloaded"` or `"load"` for faster page resolution; apply stealth Playwright plugins (e.g., `playwright-stealth`) to disguise the headless browser; or implement per-domain crawl strategies with a fallback to direct API calls where available.

---

## 5. Additional Technical Observations

Beyond the tracked issues, the following concerns were identified through code review:

**Hardcoded local path: `Crawler.py` contains a hardcoded Windows path `ROOT = r"D:\Desktop\FAUUni\Adv\QDArchive\My downloads"`, which makes the script non-portable. This should be replaced with a configurable path, environment variable, or argument.

**Database file committed to repo: The binary SQLite database `23273412-seeding.db` is version-controlled. Binary database files generally should not be committed; a schema migration script or seed script should be used instead, and the `.gitignore` updated accordingly.

**SQL injection risk: In `scraper.py`, raw DOI values are interpolated directly into SQL strings (e.g., `f"SELECT id FROM PROJECTS WHERE doi = '{doi}' "`). This should use parameterized queries (`cursor.execute("... WHERE doi = ?", (doi,))`) to prevent injection.

**Database connection closed prematurely: In `Crawler.py`'s `run()` function, `conn.close()` is called inside the loop before all repository iterations are complete, which would cause a failure on the second loop iteration.

**No logging framework: The pipeline uses `print()` throughout. Replacing this with Python's `logging` module would allow configurable log levels and persistent log files for audit trails.

---

## 6. Summary

QDArchive implements a well-structured two-phase pipeline (metadata scraping → file crawling) with a clean separation of concerns across its modules. The SQLite-backed metadata store and per-project folder organization are sound design choices for an archival system.

However, the pipeline is currently blocked on fundamental access control issues (Issues #1, #2) affecting the UK Data Service, and on robustness issues (Issues #3, #4) affecting network reliability and headless browser detection. Resolving the authentication gap and improving the HTTP/Playwright request strategies are the highest-priority next steps for making the pipeline operationally viable.