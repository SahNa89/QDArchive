import os
import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, date
import time
import sqlite3
from parser import insert_project,insert_KEYWORDS, insert_LICENSES, insert_PERSON_ROLE



BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db_path = os.path.join(BASE_DIR, "PipelineScript", "23273412-seeding.db")


def load_extensions(): #search ext
    with open("QDAFileExtensions.csv", newline="") as f:
        return [r["extensions"].lower().strip() for r in csv.DictReader(f)]

def load_search(): # search query
    with open("QDAFileExtensions.csv", newline="") as f:
        return [r["query"].lower().strip() for r in csv.DictReader(f)]

def load_repositories():
    with open("QualitativeDataRepositories.csv", newline="") as f:
        return [r["Address"].strip() for r in csv.DictReader(f)]

extensions = load_extensions()
searchquery= load_search()
repos = load_repositories()


def ukdsUK_search(query):
    url = "https://api.datacite.org/dois"
    # url = "https://datacatalogue.ukdataservice.ac.uk/searchresults"
    #     DOC_BASE_URL = "http://doc.ukdataservice.ac.uk/doc"

    params = {
        'client-id': "bl.ukda" ,
        'query': query,
        'page[size]': 50,
        'page[number]': 1,
    }
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    s = requests.Session()
    response = s.get(url, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    data = response.json()
    items = data.get('data', [])
    return items

def icpsr_search(query):

    url = "https://api.datacite.org/dois"
    #url =  "https://www.icpsr.umich.edu/web/ICPSR/search/studies"


    params = {
         'client-id': "gesis.icpsr",
         'query': query,
         'page[size]': 50,
         'page[number]': 1,
         'resource-type-id': 'dataset',
    }
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    s=requests.Session()
    response = s.get(url, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    data = response.json()
    items = data.get('data', [])
    return items

def insert_metadata(data):
    for ext in data:
        studiesicpsr = icpsr_search(ext)
        studiesukdsUK = ukdsUK_search(ext)
        time.sleep(0.5)
        if studiesicpsr:
            for s in studiesicpsr:
                print("s1:", s)
                attrs = s.get('attributes', {})
                doi = attrs.get('doi', '')
                landing_url = attrs.get('url', f"https://doi.org/{doi}" if doi else '')
                print("landing_url, doi",landing_url , doi )
                # Titles: list of {'title': '...', 'lang': '...'}
                titles = attrs.get('titles', [])
                title = titles[0].get('title', '') if titles else ''
                print("lan"  , titles[0].get('lang', '') if titles else '')
                # Titles: list of {'title': '...', 'lang': '...'}
                identifiers = attrs.get('identifiers', [])
                identifier = identifiers[0].get('identifier', '') if identifiers else ''

                # Creators: list of {'name': '...', 'givenName': ..., 'familyName': ...}
                creators = attrs.get('creators', [])
                authors = '; '.join(
                    c.get('name', '') or f"{c.get('givenName', '')} {c.get('familyName', '')}".strip()
                    for c in creators
                )

                # Subjects: list of {'subject': '...'}
                subjects = attrs.get('subjects', [])
                keywords = '; '.join(sub.get('subject', '') for sub in subjects if sub.get('subject'))

                # Descriptions
                descriptions = attrs.get('descriptions', [])
                description = descriptions[0].get('description', '') if descriptions else ''

                # Rights / license
                rights_list = attrs.get('rightsList', [])
                license_name = rights_list[0].get('rights', '') if rights_list else ''
                #license_url = rights_list[0].get('rightsUri', '') if rights_list else ''

                pub_year = str(attrs.get('publicationYear', '') or '')


                insert_project({
                    "query": ext,
                    "repo_id": 15,
                    "repo_url": "https://www.icpsr.umich.edu",
                    "project_url": landing_url,
                    "version" : str(attrs.get('version', '') or ''),
                    "title": title,
                    "description": description,
                    "language" : titles[0].get('lang', '') if titles else '',
                    "doi" : f"https://doi.org/{doi}" if doi else '',
                    "upload_date" : pub_year,
                    "download_date": date.today(),
                    "download_repository_folder" : "My downloads/icpsr/" ,
                    "download_project_folder" : f"My downloads/icpsr/{title[:200]}" ,
                    "download_version_folder" : "" ,
                    "download_method": "SCRAPING",
                                   })

                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute(f"SELECT id FROM PROJECTS WHERE doi = '{doi}' ")
                rows = cursor.fetchall()
                # store ids in a list
                ids = [row[0] for row in rows]
                conn.close()
                #insert_file(project_id, r["file_name"], r["file_type"])
                for id in ids:
                    insert_KEYWORDS(id, keywords)
                    insert_LICENSES(id, license_name)
                    insert_PERSON_ROLE(id, authors, 'AUTHOR')

        if studiesukdsUK:
            for s in studiesukdsUK:
                print("s2:", s)
                attrs = s.get('attributes', {})
                doi = attrs.get('doi', '')
                landing_url = attrs.get('url', f"https://doi.org/{doi}" if doi else '')
                print("landing_url", landing_url)
                # Titles: list of {'title': '...', 'lang': '...'}
                titles = attrs.get('titles', [])
                title = titles[0].get('title', '') if titles else ''

                # Titles: list of {'title': '...', 'lang': '...'}
                identifiers = attrs.get('identifiers', [])
                identifier = identifiers[0].get('identifier', '') if identifiers else ''

                # Creators: list of {'name': '...', 'givenName': ..., 'familyName': ...}
                creators = attrs.get('creators', [])
                authors = '; '.join(
                    c.get('name', '') or f"{c.get('givenName', '')} {c.get('familyName', '')}".strip()
                    for c in creators
                )

                # Subjects: list of {'subject': '...'}
                subjects = attrs.get('subjects', [])
                keywords = '; '.join(sub.get('subject', '') for sub in subjects if sub.get('subject'))

                # Descriptions
                descriptions = attrs.get('descriptions', [])
                description = descriptions[0].get('description', '') if descriptions else ''

                # Rights / license
                rights_list = attrs.get('rightsList', [])
                license_name = rights_list[0].get('rights', '') if rights_list else ''
                # license_url = rights_list[0].get('rightsUri', '') if rights_list else ''

                pub_year = str(attrs.get('publicationYear', '') or '')

                insert_project({
                    "query": ext,
                    "repo_id": 3,
                    "repo_url": "https://ukdataservice.ac.uk/learning-hub/qualitative-data/",
                    "project_url": landing_url,
                    "version": str(attrs.get('version', '') or ''),
                    "title": title,
                    "description": description,
                    "language": titles[0].get('lang', '') if titles else '',
                    "doi": f"https://doi.org/{doi}" if doi else '',
                    "upload_date": pub_year,
                    "download_date": date.today(),
                    "download_repository_folder": "My downloads/ukdataservice/",
                    "download_project_folder": f"My downloads/ukdataservice/{title[:200]}",
                    "download_version_folder": "",
                    "download_method": "SCRAPING",
                })

                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute(f"SELECT id FROM PROJECTS WHERE doi = '{doi}' ")
                rows = cursor.fetchall()
                # store ids in a list
                ids = [row[0] for row in rows]
                conn.close()
                # insert_file(project_id, r["file_name"], r["file_type"])
                for id in ids:
                    insert_KEYWORDS(id, keywords)
                    insert_LICENSES(id, license_name)
                    insert_PERSON_ROLE(id, authors, 'AUTHOR')

        print("studies1:",studiesukdsUK)
# PIPELINE

def run():

    extensions = load_extensions()
    repos = load_repositories()
    searchquery = load_search()


    print("Extensions:", extensions)
    print("Repositories:", repos)
    print("Query:", searchquery)


    insert_metadata(extensions)
    insert_metadata(searchquery)


if __name__ == "__main__":
    run()
