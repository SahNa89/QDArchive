import sqlite3

DB_NAME = "23273412-seeding.db"

def init_database():
    with sqlite3.connect(DB_NAME) as conn:

        conn.execute("PRAGMA foreign_keys = ON;")

        # PROJECTS TABLE
        conn.execute("""
        CREATE TABLE IF NOT EXISTS PROJECTS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_string TEXT,
            repository_id INTEGER NOT NULL,
            repository_url TEXT NOT NULL,
            project_url TEXT NOT NULL,
            version TEXT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            language TEXT,
            doi TEXT,
            upload_date TIMESTAMP,
            download_date TIMESTAMP NOT NULL,
            download_repository_folder TEXT NOT NULL,
            download_project_folder TEXT NOT NULL,
            download_version_folder TEXT,
            download_method TEXT NOT NULL CHECK(download_method IN ('SCRAPING','API-CALL'))
        );
        """)


        # FILES TABLE

        conn.execute("""
        CREATE TABLE IF NOT EXISTS FILES (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            file_type TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'SUCCEEDED',
                'FAILED_SERVER_UNRESPONSIVE',
                'FAILED_LOGIN_REQUIRED',
                'FAILED_TOO_LARGE'
            )),
            FOREIGN KEY(project_id) REFERENCES PROJECTS(id) ON DELETE CASCADE
        );
        """)

        # KEYWORDS TABLE

        conn.execute("""
        CREATE TABLE IF NOT EXISTS KEYWORDS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES PROJECTS(id) ON DELETE CASCADE
        );
        """)

        # PERSON_ROLE TABLE

        conn.execute("""
        CREATE TABLE IF NOT EXISTS PERSON_ROLE (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN (
                'UPLOADER',
                'AUTHOR',
                'OWNER',
                'OTHER',
                'UNKNOWN'
            )),
            FOREIGN KEY(project_id) REFERENCES PROJECTS(id) ON DELETE CASCADE
        );
        """)

        # LICENSES TABLE

        conn.execute("""
        CREATE TABLE IF NOT EXISTS LICENSES (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            license TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES PROJECTS(id) ON DELETE CASCADE
        );
        """)

    print(f"Database '{DB_NAME}' created successfully.")


if __name__ == "__main__":
    init_database()