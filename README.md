# QDArchive

> A data acquisition pipeline for archiving Qualitative Data Analysis (QDA) files from public research repositories.

---

## What is QDArchive?

Qualitative Data Analysis (QDA) helps researchers synthesize and structure information from unstructured data. Through qualitative coding — labeling data so that theoretical constructs are represented as codes — researchers build hierarchically structured code systems. QDArchive is a web service designed to **publish and archive qualitative research data, with an emphasis on QDA project files.

This repository implements **Part 1: the Data Acquisition Pipeline, which automatically discovers, downloads, and stores QDA datasets from supported repositories.

---

## Features

- Searches two major qualitative data repositories: **ICPSR and **UK Data Service
- Retrieves dataset metadata (title, DOI, authors, keywords, license, description) via the DataCite API
- Stores structured metadata in a local **SQLite database
- Uses **Playwright (headless Chromium) to scrape project landing pages for downloadable files
- Downloads files and organizes them into a clean folder structure by repository and project title
- Tracks download status per file: `SUCCEEDED`, `SKIPPED`, `FAILED_LOGIN_REQUIRED`, `FAILED_SERVER_UNRESPONSIVE`
- Avoids re-downloading files that already exist locally

---

## Repository Structure

```
QDArchive/
├── scraper.py                      # Phase 1: metadata search and DB insertion
├── Crawler.py                      # Phase 2: file scraping and downloading
├── parser.py                       # DB helper functions (insert project, file, keywords, etc.)
├── SQLseeding.py                   # Seeds the DB with initial repository records
├── QDATest.py                      # Test/validation script
├── QDAFileExtensions.csv           # QDA file extensions and search queries
├── QualitativeDataRepositories.csv # Target repository URLs
├── SQLiteMetaDataDatabaseSchema.csv# Database schema documentation
├── 23273412-seeding.db             # Pre-seeded SQLite database
├── requirements.txt                # Python dependencies
├── __init__.py
└── .gitignore
```

---

## Pipeline Overview

```
Phase 1 — Metadata Scraping
  QDAFileExtensions.csv ──► scraper.py ──► SQLite DB
  QualitativeDataRepositories.csv

Phase 2 — File Acquisition
  SQLite DB ──► Crawler.py ──► Downloaded files (local folder)
                    │
                    └──► Status written back to DB
```

**Phase 1 (`scraper.py`): Queries the DataCite API using QDA-related search terms and file extensions. Retrieves metadata for matching datasets from ICPSR and the UK Data Service, then inserts records into the database.

**Phase 2 (`Crawler.py`): Reads project URLs from the database, navigates to each page using a headless browser, scrapes download links, and fetches files via HTTP. Each file's download outcome is recorded.

---

## Requirements

- Python 3.8+
- See `requirements.txt` for all dependencies

Install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Usage

### Step 1 — Seed the database

```bash
python SQLseeding.py
```

### Step 2 — Run the metadata scraper

```bash
python scraper.py
```

### Step 3 — Run the file crawler

Before running, update the `ROOT` path in `Crawler.py` to your local download directory:

```python
ROOT = r"/your/local/path/QDArchive/downloads"
```

Then run:

```bash
python Crawler.py
```

---

## Database Schema

The SQLite database stores the following entities:

| Table | Description |
|---|---|
| `PROJECTS` | One record per dataset (title, DOI, URL, dates, method) |
| `FILES` | Downloaded files linked to a project |
| `KEYWORDS` | Subject keywords per project |
| `LICENSES` | License information per project |
| `PERSON_ROLE` | Authors and their roles per project |

Refer to `SQLiteMetaDataDatabaseSchema.csv` for full column definitions.

---

## Supported Repositories

| ID | Repository | API Used |
|---|---|---|
| 3 | UK Data Service | DataCite (`bl.ukda`) |
| 15 | ICPSR | DataCite (`gesis.icpsr`) |

---

## Known Issues

The pipeline has 4 open issues that represent active limitations:

| # | Title | Summary |
|---|---|---|
| [#1](https://github.com/SahNa89/QDArchive/issues/1) | Limitation | Databrary requires institutional auth; UK Data Service requires login for most datasets |
| [#2](https://github.com/SahNa89/QDArchive/issues/2) | 401 Unauthorized | Download requests rejected — session cookies not shared between Playwright and `requests` |
| [#3](https://github.com/SahNa89/QDArchive/issues/3) | Max retries exceeded | Connection timeouts when reaching the UK Data Service ReShare server |
| [#4](https://github.com/SahNa89/QDArchive/issues/4) | API Block | Playwright times out on DataLumos pages — likely bot-detection or JS-heavy rendering |

See the [Issues tab](https://github.com/SahNa89/QDArchive/issues) for full details.

---

## License

This project is licensed under the [MIT License](LICENSE).
