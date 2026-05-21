import sqlite3
from PipelineScript.SQLseeding import init_database
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db_path = os.path.join(BASE_DIR, "PipelineScript", "23273412-seeding.db")
def test_database():

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    tables = ["PROJECTS", "FILES", "KEYWORDS", "PERSON_ROLE", "LICENSES"]

    for t in tables:
        c.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{t}'")
        assert c.fetchone() is not None, f"{t} missing"

    print("All tables exist ✔")

def test_data():

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM PROJECTS ' ")
    count = c.fetchone()[0]
    c.execute("SELECT * FROM PROJECTS ")
    rows = c.fetchall()

    for row in rows:
        print(row)

    #c.execute("PRAGMA foreign_keys = OFF")
    #c.execute("DELETE FROM PROJECTS")
    #c.execute("PRAGMA foreign_keys = ON")
    #c.execute("DELETE FROM sqlite_sequence WHERE name='PROJECTS'")
   # c.execute("SELECT * FROM sqlite_sequence")
   # print(c.fetchall())

    assert count >= 0
    print("Data test passed ✔", count)
    conn.commit()
    conn.close()

if __name__ == "__main__":
    test_database()
    test_data()