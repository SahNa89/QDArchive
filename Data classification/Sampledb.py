from pathlib import Path
import sqlite3
import pandas as pd


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent

def default_database_path() -> Path:
    return repo_root() / "23273412-sq26-classification.db"


def export_project_summary():
    sql = """
    SELECT
        p.repository_id,
        p.type AS project_type,
        p.title AS project_title,
        p.class AS project_class,
        COUNT(f.id) AS no_project_files
    FROM PROJECTS p
    JOIN FILES f
        ON f.project_id = p.id
    WHERE p.type IN ('QDA_PROJECT', 'QD_PROJECT')
    GROUP BY
        p.repository_id,
        p.type,
        p.title,
        p.class
    ORDER BY
        CASE p.type
            WHEN 'QDA_PROJECT' THEN 0
            WHEN 'QD_PROJECT' THEN 1
            ELSE 2
        END,
        no_project_files DESC;
    """

    db_path = default_database_path()

    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(sql, conn)

    output_file = repo_root() / "NazeriSampleDB.xlsx"

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(
            writer,
            sheet_name="Project Summary",
            index=False,
        )

        worksheet = writer.sheets["Project Summary"]

        # Auto-size columns
        for column_cells in worksheet.columns:
            max_length = max(
                len(str(cell.value)) if cell.value is not None else 0
                for cell in column_cells
            )
            worksheet.column_dimensions[
                column_cells[0].column_letter
            ].width = min(max_length + 2, 50)

    print(f"Export complete: {output_file}")


if __name__ == "__main__":
    export_project_summary()