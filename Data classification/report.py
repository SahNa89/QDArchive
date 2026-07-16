"""
generate_report.py

Builds ONE combined PDF report (ReportOutput/isic_classification_report_by_repository.pdf)
containing every repository's section, in order:

    1. Histogram of primary classes (full ISIC class name as bin label,
       count printed on top of each bar, rendered as vector graphics)
    2. Rank-ordered table of the top 20 classes (class name + count)
    3. Short automated comments on the findings

Everything is written into a single multi-page PDF via matplotlib's
PdfPages — no separate per-repository files.

This extends report.py: same paths/queries, plus the PDF report builder.
Drop this file in the same folder as report.py (or just run it directly —
it re-declares the loader functions so it also works standalone).
"""

from pathlib import Path
import sqlite3
import textwrap

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.backends.backend_pdf import PdfPages


TOP_N = 20  # how many classes to show in the ranked table

# Edit these two to personalize the report
AUTHOR_NAME = "Sahar Nazeri"

# Map repository_id -> a human-readable name. Any id not listed here
# falls back to "Repository {id}".
REPOSITORY_NAMES = {
    3: "UK Data Service",
    15: "ICPSR",
}


def repository_label(repository_id) -> str:
    return REPOSITORY_NAMES.get(repository_id, f"Repository {repository_id}")

# Shared palette
NAVY = "#1F3A5F"
ACCENT = "#2E86AB"
LIGHT_GRID = "#E3E7EC"
ROW_STRIPE = "#F3F6F9"


###############################################################################
# Paths  (same layout as report.py)
###############################################################################
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_database_path() -> Path:
    return repo_root() / "23273412-sq26-classification.db"


def isic_csv_path() -> Path:
    return repo_root() / "ISIC5_Exp_Notes_11Mar2024.xlsx - Divisions.csv"


def output_directory() -> Path:
    out = repo_root() / "ReportOutput"
    out.mkdir(exist_ok=True)
    return out


###############################################################################
# Read classification data
###############################################################################
def load_project_classes():
    conn = sqlite3.connect(default_database_path())

    query = """
    SELECT
        repository_id,
        class as project_class
    FROM projects
    WHERE class <> 'UNKNOWN'
      AND TRIM(class) <> ''
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    return df


###############################################################################
# Read ISIC names
###############################################################################
def load_isic():
    isic = pd.read_csv(isic_csv_path(), encoding="latin1")

    isic = isic.rename(columns={
        "ISIC5 lable class": "project_class",
        "title": "class_name",
    })

    return isic[["project_class", "class_name"]]


###############################################################################
# Histogram page (vector graphics via matplotlib's PDF backend)
###############################################################################
def add_histogram_page(pdf, repository, counts):
    # Horizontal bars: every class name is printed in full, top-to-bottom,
    # largest class at the top. No rotation, no truncation, no overlap —
    # the chart simply grows taller as more classes are involved.
    n = len(counts)
    ordered = counts.sort_values("Count", ascending=True).reset_index(drop=True)

    fig_height = max(4.5, 0.32 * n + 1.5)
    fig, ax = plt.subplots(figsize=(13, fig_height))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    # Colour each bar by its rank (darker = larger) using a smooth gradient
    norm = mcolors.Normalize(vmin=0, vmax=max(n - 1, 1))
    cmap = matplotlib.colormaps["Blues"]
    colors = [cmap(0.35 + 0.55 * norm(i)) for i in range(n)]

    bars = ax.barh(
        ordered["class_name"],
        ordered["Count"],
        color=colors,
        edgecolor="white",
        linewidth=0.6,
        height=0.72,
        zorder=3,
    )

    max_count = ordered["Count"].max()
    for bar, count in zip(bars, ordered["Count"]):
        ax.text(
            bar.get_width() + max_count * 0.012,
            bar.get_y() + bar.get_height() / 2,
            f"{int(count):,}",
            va="center",
            ha="left",
            fontsize=8.5,
            color=NAVY,
            fontweight="bold",
        )

    ax.set_xlim(0, max_count * 1.12)
    ax.set_title(f"{repository}", fontsize=16, fontweight="bold",
                 color=NAVY, loc="left", pad=14)
    ax.text(0, 1.01, "Primary ISIC Classes — Distribution of Classified Projects",
            transform=ax.transAxes, fontsize=10.5, color="dimgray")

    ax.set_xlabel("Number of Projects", fontsize=10, color="dimgray")
    ax.tick_params(axis="y", labelsize=8.5, length=0)
    ax.tick_params(axis="x", labelsize=8.5, colors="dimgray")

    ax.xaxis.grid(True, color=LIGHT_GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for spine in ["top", "right", "left"]:
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#cccccc")

    plt.tight_layout()
    pdf.savefig(fig)  # vector graphics, embedded directly in the combined report
    plt.close(fig)


###############################################################################
# Top-N table + comments page
###############################################################################
def add_table_page(pdf, repository, counts, total_projects):
    top = counts.head(TOP_N).reset_index(drop=True)
    n_classes = len(counts)
    largest_pct = (top.iloc[0]["Count"] / total_projects * 100) if total_projects else 0
    topn_pct = (top["Count"].sum() / total_projects * 100) if total_projects else 0

    fig, ax = plt.subplots(figsize=(11, 14))
    fig.patch.set_facecolor("white")
    ax.axis("off")

    ax.text(0, 1.0, str(repository), fontsize=16, fontweight="bold",
            va="top", color=NAVY, transform=ax.transAxes)
    ax.text(0, 0.975,
            f"Top {min(TOP_N, n_classes)} of {n_classes} classified ISIC classes "
            f"— ranked by project count",
            fontsize=10.5, va="top", transform=ax.transAxes, color="dimgray")
    ax.plot([0, 1], [0.955, 0.955], transform=ax.transAxes,
            color=LIGHT_GRID, linewidth=1.5)

    table_data = [["Rank", "ISIC Class", "Count"]]
    for i, row in top.iterrows():
        table_data.append([str(i + 1), row["class_name"], f"{int(row['Count']):,}"])

    tbl = ax.table(
        cellText=table_data,
        colWidths=[0.08, 0.72, 0.20],
        loc="upper left",
        bbox=[0, 0.35, 1, 0.58],
        cellLoc="left",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9.5)
    tbl.scale(1, 1.55)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor(LIGHT_GRID)
        cell.set_linewidth(0.8)
        if r == 0:
            cell.set_facecolor(NAVY)
            cell.set_text_props(color="white", fontweight="bold")
        elif r % 2 == 0:
            cell.set_facecolor(ROW_STRIPE)
        else:
            cell.set_facecolor("white")
        if c == 0:
            cell.set_text_props(ha="center", color="dimgray" if r else "white")
        if c == 2:
            cell.set_text_props(ha="right", fontweight="bold" if r else "bold")

    # Comment cards
    card_y = 0.285
    ax.plot([0, 1], [card_y + 0.02, card_y + 0.02], transform=ax.transAxes,
            color=LIGHT_GRID, linewidth=1.5)
    ax.text(0, card_y, "Comments", fontsize=12, fontweight="bold",
            color=NAVY, va="top", transform=ax.transAxes)

    stats = [
        ("Total classified projects", f"{total_projects:,}"),
        ("Distinct classes found", f"{n_classes}"),
    ]
    if n_classes:
        stats.append(("Largest class", f"{top.iloc[0]['class_name']} ({largest_pct:.1f}%)"))
        stats.append((f"Top {min(TOP_N, n_classes)} classes cover",
                       f"{topn_pct:.1f}% of all classified projects"))

    y = card_y - 0.045
    for label, value in stats:
        ax.text(0.02, y, "\u2022", fontsize=11, color=ACCENT, transform=ax.transAxes)
        ax.text(0.045, y, f"{label}:", fontsize=10, color="dimgray",
                va="top", transform=ax.transAxes)
        ax.text(0.045, y - 0.025, value, fontsize=11, va="top",
                fontweight="bold", color=NAVY, transform=ax.transAxes)
        y -= 0.065

    plt.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)

    # Console output too, mirroring report.py's original print summary
    print()
    print("=" * 80)
    print(repository)
    print("=" * 80)
    print()
    print("Comments")
    print("-------------------------")
    print(f"Projects : {total_projects}")
    print(f"Classes  : {n_classes}")
    if n_classes:
        print(f"Largest class contains {largest_pct:.1f}% of all classified projects.")


###############################################################################
# Conclusion page
###############################################################################
CONCLUSION_TEXT = (
    "However, some projects are QDA (Qualitative Data Analysis) projects "
    "whose authors did not share the original QDA files. In these cases, "
    "the projects were instead placed under QD (Qualitative Data) project. "
    "Most of the QDA files are PDFs, converted into codebook PDF files.\n\n"
    "Some of the URLs returned by my searches were not actual projects, "
    "but rather standards or guidance on how to write a survey. To "
    "exclude these, I filtered out entries using keywords such as "
    "\u201cdatabase\u201d in the title, removing non-project entries from "
    "the repository.\n\n"
    "To retrieve more data, I found that these repositories are updated "
    "periodically, which improves search results over time; I therefore "
    "re-ran searches to capture additional data as it became available.\n\n"
    "Overall, I believe the majority class within each repository "
    "reflects the most important project area for the institution "
    "providing that repository."
)


def add_conclusion_page(pdf, text=CONCLUSION_TEXT):
    fig, ax = plt.subplots(figsize=(11, 8.5))
    fig.patch.set_facecolor("white")
    ax.axis("off")

    ax.add_patch(plt.Rectangle((0, 0.90), 1, 0.012, transform=ax.transAxes,
                                color=ACCENT, clip_on=False))
    ax.text(0, 0.84, "Conclusion", fontsize=20, fontweight="bold",
            color=NAVY, transform=ax.transAxes)

    wrapped = "\n".join(
        "\n".join(textwrap.wrap(paragraph, 95)) if paragraph else ""
        for paragraph in text.split("\n\n")
    )
    # rebuild with blank line between paragraphs
    paragraphs = ["\n".join(textwrap.wrap(p, 95)) for p in text.split("\n\n")]
    wrapped = "\n\n".join(paragraphs)

    ax.text(0, 0.74, wrapped, fontsize=11.5, va="top", ha="left",
            color="#333333", linespacing=1.6, transform=ax.transAxes)

    plt.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


###############################################################################
# Process one repository
###############################################################################
def process_repository(df, repository, pdf):
    repo_df = df[df["repository_id"] == repository]

    counts = (
        repo_df
        .groupby("class_name")
        .size()
        .reset_index(name="Count")
        .sort_values("Count", ascending=False)
        .reset_index(drop=True)
    )

    total_projects = len(repo_df)
    label = repository_label(repository)

    add_histogram_page(pdf, label, counts)
    add_table_page(pdf, label, counts, total_projects)


###############################################################################
# Main
###############################################################################
def main():
    projects = load_project_classes()
    isic = load_isic()

    projects = projects.merge(isic, on="project_class", how="left")
    projects["class_name"] = projects["class_name"].fillna(projects["project_class"])

    repositories = sorted(projects["repository_id"].dropna().unique())

    report_path = output_directory() / "isic_classification_report_by_repository.pdf"

    with PdfPages(report_path) as pdf:
        # Cover page
        fig, ax = plt.subplots(figsize=(11, 8.5))
        fig.patch.set_facecolor("white")
        ax.axis("off")

        ax.add_patch(plt.Rectangle((0, 0.78), 1, 0.02, transform=ax.transAxes,
                                    color=ACCENT, clip_on=False))
        ax.text(0.5, 0.62, "ISIC Classification Report", ha="center",
                fontsize=26, fontweight="bold", color=NAVY)
        ax.text(0.5, 0.565, "by Repository", ha="center",
                fontsize=18, color=ACCENT)
        ax.text(0.5, 0.47,
                "Primary ISIC classification of projects, grouped by\n"
                "source data repository.",
                ha="center", fontsize=12, color="dimgray")
        ax.text(0.5, 0.40, f"Repositories covered: {len(repositories)}",
                ha="center", fontsize=11.5, color=NAVY, fontweight="bold")
        ax.text(0.5, 0.10, AUTHOR_NAME, ha="center", fontsize=12,
                color=NAVY, fontweight="bold")
        ax.text(0.5, 0.075, pd.Timestamp.today().strftime("%B %d, %Y"),
                ha="center", fontsize=9.5, color="dimgray")
        ax.add_patch(plt.Rectangle((0, 0.0), 1, 0.01, transform=ax.transAxes,
                                    color=ACCENT, clip_on=False))
        pdf.savefig(fig)
        plt.close(fig)

        for repo in repositories:
            process_repository(projects, repo, pdf)

        add_conclusion_page(pdf)

    print()
    print(f"Report written to: {report_path}")


if __name__ == "__main__":
    main()