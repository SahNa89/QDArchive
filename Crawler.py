

import requests
from bs4 import BeautifulSoup
import pandas as pd
import re
import time
from urllib.parse import urljoin

SITES = [

]

QDA_EXTENSIONS = [
    ".qdpx",
    ".qda",
    ".qdp",
    ".mx22",
    ".mx",
    ".atlas.ti",
    ".nvpx"
]

queries = [
"qdpx",
"qda project",
"nvivo coding csv",
"atlas.ti export csv",
"maxqda mx22",
"qualitative coding csv",
"interview transcript dataset",
"coded qualitative dataset",
"refi-qda xml",
"open ended responses csv"
]

CSV_PATTERN = re.compile(r"\.csv$", re.IGNORECASE)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (QDA File Research Bot)"
}

MAX_PAGES_PER_SITE = 10   # adjust to crawl deeper
DELAY = 1                 # wait seconds between requests

def contains_qda_extension(url):
    for ext in QDA_EXTENSIONS:
        if url.lower().endswith(ext):
            return True
    return False


def is_potential_qda_csv(url):
    return bool(CSV_PATTERN.search(url))


def crawl_site(base_url):
    found_links = []

    for page in range(1, MAX_PAGES_PER_SITE + 1):
        try:
            search_url = f"{base_url}/search?q=qda&page={page}"
            print(f"Searching: {search_url}")

            response = requests.get(search_url, headers=HEADERS, timeout=15)
            if response.status_code != 200:
                continue

            soup = BeautifulSoup(response.text, "lxml")

            for link in soup.find_all("a", href=True):
                href = link["href"]
                full_url = urljoin(base_url, href)

                if contains_qda_extension(full_url):
                    found_links.append({
                        "site": base_url,
                        "type": "QDA_NATIVE",
                        "url": full_url
                    })

                elif is_potential_qda_csv(full_url):
                    found_links.append({
                        "site": base_url,
                        "type": "CSV_POSSIBLE_QDA",
                        "url": full_url
                    })

            time.sleep(DELAY)

        except Exception as e:
            print(f"Error crawling {base_url}: {e}")

    return found_links


all_results = []

for site in SITES:
    results = crawl_site(site)
    all_results.extend(results)

# Remove duplicates
df = pd.DataFrame(all_results).drop_duplicates()

# Save results
df.to_csv("qda_file_links.csv", index=False)

print("\nFinished.")
print(f"Total links found: {len(df)}")
print("Saved to qda_file_links.csv")


