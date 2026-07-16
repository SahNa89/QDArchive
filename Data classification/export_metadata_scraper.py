import argparse
import json
import logging
import re
import sqlite3
import sys
import time ,os
from urllib.parse import urljoin, urlparse

from xml.etree import ElementTree as ET

from license_normalizer import normalize_license

try:
    from playwright.sync_api import sync_playwright
except Exception as e:
    sys.exit("Playwright is required. Install with: pip install playwright\nThen run: playwright install")


DATA_ACQUISITION_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(DATA_ACQUISITION_DIR)
DB_NAME = "23273412-sq26-classification.db"
DB_PATH = os.path.join(PROJECT_ROOT, DB_NAME)
REPOSITORY_ID = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")


def fetch_projects(conn, repository_id=REPOSITORY_ID, project_id=None):
    cur = conn.cursor()
    if project_id is None:
        cur.execute("SELECT id, project_url FROM PROJECTS WHERE repository_id = ?", (repository_id,))
    else:
        cur.execute("SELECT id, project_url FROM PROJECTS WHERE repository_id = ? AND id = ?", (repository_id, project_id))
    return cur.fetchall()


def upsert_license(conn, project_id, license_text):
    license_text = license_text.strip()
    if not license_text:
        return
    normalized = normalize_license(license_text)
    license_text = normalized if normalized is not None else license_text
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM LICENSES WHERE project_id = ? AND license = ?", (project_id, license_text))
    if cur.fetchone():
        return
    cur.execute("INSERT INTO LICENSES (project_id, license) VALUES (?, ?)", (project_id, license_text))
    conn.commit()


def upsert_keywords_row(conn, project_id, keywords):
    normalized_keywords = [normalize_text(k) for k in keywords if k and normalize_text(k)]
    if not normalized_keywords:
        return
    keyword_text = ", ".join(normalized_keywords)
    cur = conn.cursor()
    cur.execute("DELETE FROM KEYWORDS WHERE project_id = ?", (project_id,))
    cur.execute("INSERT INTO KEYWORDS (project_id, keyword) VALUES (?, ?)", (project_id, keyword_text))
    conn.commit()


def upsert_person_role(conn, project_id, name, role="UNKNOWN"):
    name = name.strip()
    role = role.strip() or "UNKNOWN"
    if not name:
        return
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM PERSON_ROLE WHERE project_id = ? AND name = ? AND role = ?", (project_id, name, role))
    if cur.fetchone():
        return
    cur.execute("INSERT INTO PERSON_ROLE (project_id, name, role) VALUES (?, ?, ?)", (project_id, name, role))
    conn.commit()


def extract_jsonld(text):
    results = []
    try:
        obj = json.loads(text)
    except Exception:
        return results
    # JSON-LD may be an object or list
    def walk(o):
        if isinstance(o, dict):
            results.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(obj)
    return results


def parse_ddi_xml(xml_text):
    # parse prodStmt/copyright text
    licenses = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return licenses
    for prod in root.findall('.//{*}prodStmt'):
        for cr in prod.findall('.//{*}copyright'):
            if cr.text and cr.text.strip():
                licenses.append(cr.text.strip())
    return licenses


def parse_oai_dc_subjects(xml_text):
    """Real Dublin Core subject/keyword terms from an OAI-DC record.

    Deliberately reads <dc:subject>, not <dc:identifier>: UK Data Service's
    ReShare OAI-DC feed puts the OAI record id, raw data/documentation file
    URLs, AND the full bibliographic citation string all under
    <dc:identifier> - none of those are keywords, and inserting them as
    such is what produced KEYWORDS rows like "info:oai:...;https://...zip;
    Taylor, Rebecca ... (2025). ... 10.5255/UKDA-SN-857869 ...".
    """
    subjects = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return subjects
    for subject in root.findall('.//{*}subject'):
        if subject.text and subject.text.strip():
            subjects.append(subject.text.strip())
    return subjects


def normalize_text(t):
    return re.sub(r"\s+", " ", t).strip()


def extract_repository3_license_from_html(html_text):
    results = []
    if not html_text:
        return results

    def strip_tags(text):
        return re.sub(r'<[^>]+>', ' ', text)

    for m in re.finditer(
        r'<p\b[^>]*class=["\'][^"\']*MuiTypography-root[^"\']*MuiTypography-body2[^"\']*css-acw7dy[^"\']*["\'][^>]*>(.*?)</p>',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        candidate = normalize_text(strip_tags(m.group(1)))
        if candidate:
            results.append(candidate)
            break

    if not results:
        for m in re.finditer(r'<p\b[^>]*>(.*?)</p>', html_text, re.IGNORECASE | re.DOTALL):
            candidate = normalize_text(strip_tags(m.group(1)))
            if candidate and ('the data collection' in candidate.lower() or 'end user' in candidate.lower()):
                results.append(candidate)
                break

    if not results:
        for m in re.finditer(
            r'<a[^>]+href=["\']https?://creativecommons\.org/licenses/[^"\']+["\'][^>]*>(.*?)</a>',
            html_text,
            re.IGNORECASE | re.DOTALL,
        ):
            candidate = normalize_text(strip_tags(m.group(1)))
            if candidate and is_license_text(candidate):
                results.append(candidate)

    return list(dict.fromkeys(results))


def extract_repository3_keywords_from_html(html_text):
    results = []
    if not html_text:
        return results

    def strip_tags(text):
        return re.sub(r'<[^>]+>', '', text)

    for m in re.finditer(r'<a\b[^>]*>(.*?)</a>', html_text, re.IGNORECASE | re.DOTALL):
        snippet = html_text[max(0, m.start() - 500):min(len(html_text), m.end() + 500)]
        if re.search(r'thesaurus(?:\s+search)?\s+keywords', snippet, re.IGNORECASE):
            candidate = normalize_text(strip_tags(m.group(1)))
            if candidate:
                results.append(candidate)

    return list(dict.fromkeys(results))


def extract_license_from_html(html_text):
    results = []
    if not html_text:
        return results

    def strip_tags(text):
        return re.sub(r'<[^>]+>', '', text)

    def extract_last_anchor_text(text):
        anchors = re.findall(r'<a[^>]*>(.*?)</a>', text, re.IGNORECASE | re.DOTALL)
        if anchors:
            return anchors[-1]
        return text

    def add(candidate):
        if '<a' in candidate.lower() and '</a>' in candidate.lower():
            candidate = extract_last_anchor_text(candidate)
        candidate = normalize_text(strip_tags(candidate))
        if candidate and is_license_text(candidate):
            results.append(candidate)

    for m in re.finditer(r'<a[^>]+rel=["\']license["\'][^>]*>(.*?)</a>', html_text, re.IGNORECASE | re.DOTALL):
        text = re.sub(r'<[^>]+>', '', m.group(1))
        add(text)

    for m in re.finditer(r'<meta[^>]+name=["\']license["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.IGNORECASE):
        add(m.group(1))

    for m in re.finditer(
        r'((?:licensed under|license:|licenses? under|creative commons).*?license[^\n<]*)',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        add(m.group(1))

    return list(dict.fromkeys(results))


def is_license_text(t: str) -> bool:
    if not t:
        return False
    s = t.lower()
    indicators = [
        'license', 'licensed', 'creative commons', 'terms of use', 'terms-of-use',
        'icpsr terms', 'cc by', 'cc-by', 'creativecommons'
    ]
    for ind in indicators:
        if ind in s:
            return True
    # accept obvious license URLs
    if s.startswith('http://') or s.startswith('https://'):
        if 'license' in s or 'creativecommons' in s or 'cc/' in s or 'licen' in s:
            return True
    return False


def infer_person_role(name: str, role_hint: str | None = None) -> str:
    if not name:
        return 'UNKNOWN'
    role = 'UNKNOWN'
    if role_hint:
        role_hint = role_hint.lower()
        if 'creator' in role_hint or 'author' in role_hint:
            role = 'AUTHOR'
        elif 'uploader' in role_hint or 'submitted by' in role_hint or 'submitted' in role_hint:
            role = 'UPLOADER'
        elif 'owner' in role_hint or 'responsible' in role_hint or 'maintainer' in role_hint:
            role = 'OWNER'
        elif 'contact' in role_hint or 'producer' in role_hint:
            role = 'OTHER'
    # fallback heuristics from name labels
    if role == 'UNKNOWN':
        lower_name = name.lower()
        if 'uploader' in lower_name or 'submitted' in lower_name:
            role = 'UPLOADER'
        elif 'author' in lower_name or 'creator' in lower_name:
            role = 'AUTHOR'
        elif 'owner' in lower_name or 'responsible' in lower_name:
            role = 'OWNER'
    return role


def extract_from_page(page, request_ctx, base_url, repository_id=REPOSITORY_ID):
    # returns dict
    data = {
        "licenses": [],
        "keywords": [],
        "person_roles": [],
        "checked_files": [],
    }

    # 1) JSON-LD scripts
    scripts = page.query_selector_all('script[type="application/ld+json"]')
    for s in scripts:
        try:
            txt = s.inner_text()
        except Exception:
            continue
        for obj in extract_jsonld(txt):
            # license entries
            atype = obj.get('@type') or obj.get('type')
            name = obj.get('name') or obj.get('license') or obj.get('url')
            if isinstance(atype, str) and 'license' in atype.lower():
                if name:
                    data['licenses'].append(normalize_text(str(name)))
            elif name and is_license_text(str(name)):
                data['licenses'].append(normalize_text(str(name)))
            # keywords
            if 'keywords' in obj:
                kw = obj['keywords']
                if isinstance(kw, list):
                    for k in kw:
                        data['keywords'].append(normalize_text(str(k)))
                else:
                    data['keywords'].append(normalize_text(str(kw)))
            # creators / persons
            for person_key in ('creator', 'author'):
                if person_key in obj:
                    creators = obj[person_key]
                    if isinstance(creators, list):
                        itercre = creators
                    else:
                        itercre = [creators]
                    for c in itercre:
                        if isinstance(c, dict):
                            name = c.get('name') or c.get('@id')
                            role_hint = c.get('role') or person_key
                            if name:
                                role_value = infer_person_role(str(name), str(role_hint))
                                data['person_roles'].append((normalize_text(str(name)), normalize_text(role_value)))
                        elif isinstance(c, str):
                            name = c
                            role_value = infer_person_role(name, person_key)
                            data['person_roles'].append((normalize_text(str(name)), normalize_text(role_value)))

    # 2) <a rel="license"> anchors
    for a in page.query_selector_all('a[rel="license"]'):
        txt = a.inner_text() or ''
        if txt.strip() and is_license_text(txt):
            data['licenses'].append(normalize_text(txt))

    # 3) project-page HTML text license search
    try:
        page_html = page.content()
        if repository_id == 3:
            for lic in extract_repository3_license_from_html(page_html):
                data['licenses'].append(normalize_text(lic))
            for kw in extract_repository3_keywords_from_html(page_html):
                data['keywords'].append(normalize_text(kw))
        else:
            for lic in extract_license_from_html(page_html):
                data['licenses'].append(normalize_text(lic))
    except Exception:
        pass

    try:
        page_text = page.inner_text()
        for m in re.finditer(r'(?:licensed under|license:|licenses? under|creative commons).*?license', page_text, re.IGNORECASE):
            candidate = normalize_text(m.group(0))
            if is_license_text(candidate):
                data['licenses'].append(candidate)
    except Exception:
        pass

    # 4) meta keywords
    try:
        meta_kw = page.query_selector('meta[name="keywords"]')
        if meta_kw:
            cnt = meta_kw.get_attribute('content')
            if cnt:
                for k in re.split(r"[,;]\s*", cnt):
                    data['keywords'].append(normalize_text(k))
    except Exception:
        pass

    # 4) collect candidate export-metadata links (limit 4)
    anchors = page.query_selector_all('a')
    candidates = []
    for a in anchors:
        href = a.get_attribute('href')
        if not href:
            continue
        href = href.strip()
        if href.startswith('#'):
            continue
        href_lower = href.lower()
        if any(k in href_lower for k in ('pcms.icpsr', 'oai', 'metadata', 'ddi', 'dublin', 'oai_dc', 'oai_ddi25', 'export', 'json', 'xml', 'fcr:versions')):
            abs_url = urljoin(base_url, href)
            if abs_url not in candidates:
                candidates.append(abs_url)
        txt = (a.inner_text() or '').lower()
        if any(k in txt for k in ('export', 'metadata', 'ddi', 'dublin', 'json', 'oai', 'xml')):
            abs_url = urljoin(base_url, href)
            if abs_url not in candidates:
                candidates.append(abs_url)
        if len(candidates) >= 4:
            break
    for link in page.query_selector_all('link[rel="alternate"], link[rel="license"]'):
        href = link.get_attribute('href')
        if not href:
            continue
        href = href.strip()
        abs_url = urljoin(base_url, href)
        if abs_url not in candidates and len(candidates) < 4:
            candidates.append(abs_url)

    # 5) fetch and parse each candidate
    for u in candidates[:4]:
        data['checked_files'].append(u)
        logging.info(f"Fetching metadata file: {u}")
        try:
            resp = request_ctx.get(u, timeout=60000)
            status = resp.status
            if status and status >= 400:
                logging.warning(f"Failed to fetch {u}: HTTP {status}")
                continue
            text = resp.text()
        except Exception as e:
            logging.warning(f"Request failed for {u}: {e}")
            continue

        # try JSON
        try:
            parsed = json.loads(text)
            # search for license entries
            def walk_json(o):
                if isinstance(o, dict):
                    t = o.get('@type') or o.get('type')
                    name = o.get('name') or o.get('license') or o.get('url')
                    if isinstance(t, str) and 'license' in t.lower():
                        if name:
                            data['licenses'].append(normalize_text(str(name)))
                    elif name and is_license_text(str(name)):
                        data['licenses'].append(normalize_text(str(name)))
                    if 'keywords' in o:
                        kw = o['keywords']
                        if isinstance(kw, list):
                            for k in kw:
                                data['keywords'].append(normalize_text(str(k)))
                        else:
                            data['keywords'].append(normalize_text(str(kw)))
                    for v in o.values():
                        walk_json(v)
                elif isinstance(o, list):
                    for it in o:
                        walk_json(it)

            walk_json(parsed)
            continue
        except Exception:
            pass

        # try XML (DDI or OAI-DC)
        if 'oai_ddi25' in u or 'ddi' in u or text.strip().startswith('<'):
            licenses = parse_ddi_xml(text)
            for lic in licenses:
                if is_license_text(lic):
                    data['licenses'].append(normalize_text(lic))
            # also check for copyright strings in raw text
            m = re.search(r'licensed under (.*?license\.?)(<|$)', text, re.IGNORECASE)
            if m:
                candidate = normalize_text(m.group(1))
                if is_license_text(candidate):
                    data['licenses'].append(candidate)

        if 'oai_dc' in u or 'oai_dc' in text.lower() or 'oai_dc' in u.lower() or 'dublin' in u.lower():
            for subject in parse_oai_dc_subjects(text):
                data['keywords'].append(subject)

        # fallback: search for rel="license" or link text
        for m in re.finditer(r'rel\s*=\s*"license"[^>]*>([^<]+)<', text, re.IGNORECASE):
            candidate = normalize_text(m.group(1))
            if is_license_text(candidate):
                data['licenses'].append(candidate)

    # deduplicate
    data['licenses'] = list(dict.fromkeys([normalize_text(x) for x in data['licenses'] if x]))
    data['keywords'] = list(dict.fromkeys([normalize_text(x) for x in data['keywords'] if x]))
    data['person_roles'] = list(dict.fromkeys(data['person_roles']))

    return data


def run(repository_id=REPOSITORY_ID, project_id=None):
    conn = sqlite3.connect(DB_PATH)

    projects = fetch_projects(conn, repository_id=repository_id, project_id=project_id)
    if not projects:
        logging.info(f"No projects found with repository_id={REPOSITORY_ID}")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"]) 
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
        )
        # create a request context for fetching metadata files with same headers
        request_ctx = p.request.new_context(extra_http_headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })

        for project_id, project_url in projects:
            logging.info(f"Processing project_id={project_id} url={project_url}")
            try:
                page = context.new_page()
                page.set_default_navigation_timeout(60000)
                page.goto(project_url, wait_until="networkidle")
                time.sleep(0.5)
            except Exception as e:
                logging.error(f"Failed to open {project_url}: {e}")
                try:
                    page.close()
                except Exception:
                    pass
                continue

            try:
                data = extract_from_page(page, request_ctx, project_url, repository_id=repository_id)
            except Exception as e:
                logging.error(f"Error extracting metadata for {project_url}: {e}")
                data = {"licenses": [], "keywords": [], "person_roles": [], "checked_files": []}

            logging.info(f"Found licenses: {data['licenses']}")
            logging.info(f"Found keywords: {data['keywords']}")
            logging.info(f"Found person roles: {data['person_roles']}")
            logging.info(f"Checked files: {data['checked_files']}")

            # upsert into DB
            for lic in data['licenses']:
                upsert_license(conn, project_id, lic)
            upsert_keywords_row(conn, project_id, data['keywords'])
            for name, role in data['person_roles']:
                upsert_person_role(conn, project_id, name, role)

            try:
                page.close()
            except Exception:
                pass

        request_ctx.dispose()
        context.close()
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate metadata for projects from their project URLs")
    parser.add_argument("--repository-id", type=int, default=REPOSITORY_ID)
    parser.add_argument("--project-id", type=int, default=None)
    args = parser.parse_args()
    run(repository_id=args.repository_id, project_id=args.project_id)
