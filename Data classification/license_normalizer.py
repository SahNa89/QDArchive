"""
license_normalizer.py
======================

Normalizes free-text LICENSES.license values in the classification database
onto the fixed enum from SQLiteMetaDataDatabaseSchema.csv:

    CC BY, CC BY-SA, CC BY-NC, CC BY-ND, CC BY-NC-ND, CC0,
    ODbL, ODC-By, PDDL

Each of those may carry a trailing version identifier (e.g. "CC BY 4.0",
"ODbL-1.0"). If a license value cannot be confidently mapped onto one of
these, it is left exactly as-is - this script never invents a license or
guesses at ambiguous/descriptive text (e.g. UK Data Service's End User
Licence paragraphs are not in the enum, so they are untouched).

Usage:
    python license_normalizer.py                 # normalize in place
    python license_normalizer.py --dry-run        # show what would change
    python license_normalizer.py --db path\to.db  # override DB path
"""

import argparse
import os
import re
import sqlite3

DATA_CLASSIFICATION_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(DATA_CLASSIFICATION_DIR)
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "23273412-sq26-classification.db")


# ============================================================
# NORMALIZATION LOGIC
# ============================================================

# Base license family -> canonical label, and whether the version suffix is
# joined with a space ("CC BY 4.0") or a hyphen ("ODbL-1.0").
_CANONICAL = {
    "by-nc-nd": ("CC BY-NC-ND", " "),
    "by-nc": ("CC BY-NC", " "),
    "by-nd": ("CC BY-ND", " "),
    "by-sa": ("CC BY-SA", " "),
    "by": ("CC BY", " "),
    "cc0": ("CC0", " "),
    "odbl": ("ODbL", "-"),
    "odc-by": ("ODC-By", "-"),
    "pddl": ("PDDL", "-"),
}

_VERSION_RE = re.compile(r"\d+\.\d+")


def _tokenize(raw: str) -> str:
    """Collapse a free-text license string down to a hyphen-joined token
    string so 'Creative Commons Attribution 4.0 International' and
    'CC-BY-4.0' and 'creativecommons.org/licenses/by/4.0' all normalize to
    the same shape for matching."""
    s = raw.lower()

    # Longest/most-specific phrases first, so e.g. PDDL's full name isn't
    # partially swallowed by the shorter CC0 phrase.
    s = re.sub(r"public\s*domain\s*dedication\s*and\s*license", "pddl", s)
    s = re.sub(r"open\s*data(?:base)?\s*commons?\s*attribution(?:\s*license)?", "odc-by", s)
    s = re.sub(r"open\s*database\s*license", "odbl", s)
    s = re.sub(r"creative\s*commons\s*zero", "cc0", s)
    s = re.sub(r"public\s*domain\s*dedication", "cc0", s)
    # Handle the creativecommons.org / opendatacommons.org URL forms before
    # the generic phrase substitution, so the ".org" doesn't stay glued to
    # the token (which would stop it from being recognized as isolated "cc").
    # opendatacommons.org's own "/licenses/by/..." path means ODC-By, not
    # CC BY - fold it straight into "odc-by" rather than leaving a bare "by"
    # that would later require an (absent) "cc" token to match.
    s = re.sub(r"opendatacommons\.org[a-z0-9\-/]*?by\b", "odc-by", s)
    s = re.sub(r"creativecommons\.org", "cc", s)
    s = re.sub(r"creative\s*commons", "cc", s)
    s = re.sub(r"attribution", "by", s)
    s = re.sub(r"non[\s\-]*commercial", "nc", s)
    s = re.sub(r"no[\s\-]*deriv\w*", "nd", s)
    s = re.sub(r"share[\s\-]*alike", "sa", s)
    s = re.sub(r"\bzero\b", "0", s)

    s = re.sub(r"[\s_/]+", "-", s)
    s = re.sub(r"[^a-z0-9.\-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def _has_token(norm: str, token: str) -> bool:
    return re.search(rf"(^|-){re.escape(token)}($|-)", norm) is not None


def classify_license(raw: str):
    """Return the canonical enum base key (e.g. 'by-nc-nd') for `raw`, or
    None if it doesn't confidently match anything in the enum."""
    if not raw or not raw.strip():
        return None

    norm = _tokenize(raw)
    if not norm:
        return None

    # CC0 is unambiguous on its own (the literal token "cc0" essentially
    # never appears in unrelated prose), so no extra gating needed.
    if _has_token(norm, "cc0"):
        return "cc0"

    if _has_token(norm, "odbl"):
        return "odbl"
    if _has_token(norm, "odc-by"):
        return "odc-by"
    if _has_token(norm, "pddl"):
        return "pddl"

    # The BY-family only matches if the text explicitly signals Creative
    # Commons (an isolated "cc" token, from "CC", "Creative Commons", or a
    # creativecommons.org URL). Without that gate, ordinary sentences
    # containing the word "by" (e.g. "data provided by the depositor")
    # would be misclassified as "CC BY".
    has_cc = _has_token(norm, "cc")
    if not has_cc:
        return None

    # NC+SA together ("Attribution-NonCommercial-ShareAlike") is not part
    # of the requested enum - leave those untouched rather than guessing.
    if _has_token(norm, "nc-sa") or re.search(r"(^|-)nc-sa(-|$)", norm):
        return None

    if _has_token(norm, "by-nc-nd"):
        return "by-nc-nd"
    if _has_token(norm, "by-nc"):
        return "by-nc"
    if _has_token(norm, "by-nd"):
        return "by-nd"
    if _has_token(norm, "by-sa"):
        return "by-sa"
    if _has_token(norm, "by"):
        return "by"

    return None


def normalize_license(raw: str):
    """Return the normalized license string for `raw`, or None if `raw`
    should be left exactly as-is (no confident enum match)."""
    key = classify_license(raw)
    if key is None:
        return None

    label, joiner = _CANONICAL[key]
    version_match = _VERSION_RE.search(raw)
    if version_match:
        return f"{label}{joiner}{version_match.group(0)}"
    return label


# ============================================================
# DATABASE PASS
# ============================================================

def connect(db_path):
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def normalize_all_licenses(conn, dry_run=False):
    rows = conn.execute("SELECT id, project_id, license FROM LICENSES").fetchall()
    changed = 0
    skipped_unmatched = 0

    for row_id, project_id, raw in rows:
        normalized = normalize_license(raw)
        if normalized is None:
            skipped_unmatched += 1
            continue
        if normalized == raw:
            continue

        print(f"[LICENSE] project_id={project_id}: {raw!r} -> {normalized!r}")
        changed += 1
        if not dry_run:
            conn.execute("UPDATE LICENSES SET license = ? WHERE id = ?", (normalized, row_id))

    if not dry_run:
        conn.commit()

    print(
        f"\n[DONE] {changed} license value(s) normalized, "
        f"{skipped_unmatched} left as-is (no confident enum match), "
        f"{len(rows) - changed - skipped_unmatched} already normalized."
    )
    return changed


def main():
    parser = argparse.ArgumentParser(description="Normalize LICENSES.license values onto the fixed license enum")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Path to the classification SQLite DB")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without writing them")
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        normalize_all_licenses(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
