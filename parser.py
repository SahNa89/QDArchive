import sqlite3

def insert_project(data):
    conn = sqlite3.connect("23273412-seeding.db")
    c = conn.cursor()

    c.execute("""
    INSERT INTO PROJECTS (
        query_string, repository_id, repository_url,
        project_url,version, title, description,language ,doi ,upload_date ,download_date , 
        download_repository_folder , download_project_folder, download_version_folder, download_method
    )
    VALUES (?, ?, ?, ?, ?, ?,?, ?, ?, ?, ?, ?,?, ?, ?)
    """, (
        data["query"],
        data["repo_id"],
        data["repo_url"],
        data["project_url"],
        data["version"],
        data.get("title"),
        data.get("description"),
        data.get("language"),
        data.get("doi"),
        data.get("upload_date"), data.get("download_date") ,
        data.get("download_repository_folder"),
        data.get("download_project_folder"), data.get("download_version_folder") ,data.get("download_method")

    ))

    pid = c.lastrowid
    conn.commit()
    conn.close()

    return pid


def insert_file(project_id, file_name, file_type ,status):
    conn = sqlite3.connect("23273412-seeding.db")
    c = conn.cursor()

    c.execute("""
    INSERT INTO FILES (project_id, file_name, file_type, status)
    VALUES (?, ?, ?, ?)
    """, (project_id, file_name, file_type,status ))

    conn.commit()
    conn.close()

def insert_KEYWORDS(project_id, keyword):
        conn = sqlite3.connect("23273412-seeding.db")
        c = conn.cursor()

        c.execute("""
        INSERT INTO KEYWORDS (project_id, keyword)
        VALUES (?, ?)
        """, (project_id, keyword))

        conn.commit()
        conn.close()


def insert_PERSON_ROLE(project_id, name , role):
    conn = sqlite3.connect("23273412-seeding.db")
    c = conn.cursor()

    c.execute("""
        INSERT INTO PERSON_ROLE  (project_id, name , role)
        VALUES (?, ? ,? )
        """, (project_id, name , role))

    conn.commit()
    conn.close()
def insert_LICENSES(project_id, license):
        conn = sqlite3.connect("23273412-seeding.db")
        c = conn.cursor()

        c.execute("""
        INSERT INTO LICENSES (project_id, license)
        VALUES (?, ?)
        """, (project_id, license))

        conn.commit()
        conn.close()