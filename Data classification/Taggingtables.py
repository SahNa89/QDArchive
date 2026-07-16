import argparse
import csv
import os
import re
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from huggingface_hub import InferenceClient
from huggingface_hub.errors import HfHubHTTPError

try:
    import rarfile
except ImportError:
    rarfile = None

QDA_EXTENSIONS = frozenset({
    "mqda", "mqbac", "mqtc", "mqex", "mqmtr", "mx24", "mx24bac", "mc24", "mex24",
    "mx22", "mx20", "mx18", "mx12", "mx11", "mx5", "mx4", "mx3", "mx2", "m2k",
    "loa", "sea", "mtr", "mod", "mex22", "nvp", "nvpx", "atlasproj", "hpr7",
    "hpr", "hpr5", "hpr6", "hpr8", "hpr9", "hpr10", "qdp", "qdpj", "qdpjx",
    "qda", "qda1", "qda2", "qda3", "qda4", "qdaj", "qpj", "qpjx", "qmx", "qmxj",
    "qmxjx", "qdl", "qdt", "qdtx", "qsf", "qsfj", "qsfjx", "qes", "qesj", "qesjx",
    "qsfm", "qsfmx", "qsfmtr", "qsfmtrx", "qsfmtrj", "qsfmtrjx", "qsfmtrq", "qsfmtrqx",
    "ppj", "pprj", "qlt", "qdpx", "qdc", "qpd",
})

QD_EXTENSIONS = frozenset({
    "rtf", "mp4", "wav", "mp3", "jpg", "jpeg", "png", "svg", "ico", "avif",
    "xlsx", "xls", "pptx", "ps", "csv", "pdf", "docx", "doc", "txt", "zip" , "rar" ,
})

NOT_A_PROJECT_EXTENSIONS = frozenset({
    "rdata", "rproj", "fasta", "phy", "newick", "tree",
})

QDA_SOFTWARE = frozenset({
    "maxqda", "nvivo", "atlas", "dedoose", "qdacity", "qda miner", "f4analyse", "quirkos",
})

QD_KEYWORDS = frozenset({
    "qdc", "qca", "codebook", "survey", "qualitative", "coding", "code", "coded",
    "dedoose", "atlas", "quirkos", "transcripts", "thematic",
    "interviews", "interviewees","Questionnaire",
})

PENDING_PROJECT_TYPE = "NOT_A_PROJECT"


DATA_CLASSIFICATION_DIR = Path(__file__).resolve().parent


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_database_path() -> Path:
    return repo_root() / "23273412-sq26-classification.db"


def normalize_token(value: str) -> str:
    return (value or "").strip().lower().lstrip(".")


def basename(path: str) -> str:
    normalized = (path or "").replace("\\", "/")
    return normalized.rsplit("/", 1)[-1] if normalized else ""


def extension_from_filename(file_name: str) -> str:
    base = basename(file_name)
    if "." not in base:
        return ""
    return normalize_token(base.rsplit(".", 1)[-1])


def is_plausible_extension_token(file_type: str, token: str) -> bool:
    raw = (file_type or "").strip()
    if not raw or any(ch in raw for ch in " /\t\n\\"):
        return False
    if not 1 <= len(token) <= 40:
        return False
    return all(ch.isalnum() or ch in "._-" for ch in token)


def effective_extension(file_name: str, file_type: str) -> str:
    token = normalize_token(file_type)
    if token and is_plausible_extension_token(file_type, token):
        return token
    return extension_from_filename(file_name)


def infer_extension_when_missing(file_name: str) -> str:
    path = file_name or ""
    base = basename(path)
    lower = path.lower()

    if base == "dataset" or lower.endswith("/dataset"):
        return "dat"
    if lower.startswith("https:/") or lower.startswith("http:/"):
        return "html"
    if base in {"Dockerfile", "Pipfile", "artisan", "LICENSE", "LICENSE_MEDIA"}:
        mapping = {
            "Dockerfile": "dockerfile",
            "Pipfile": "pipfile",
            "artisan": "php",
            "LICENSE": "md",
            "LICENSE_MEDIA": "md",
        }
        return mapping[base]
    if "start-container" in lower:
        return "sh"
    if "." in base:
        return extension_from_filename(path)
    return ""


def cleaned_file_type(file_name: str, file_type: str) -> str | None:
    path = file_name or ""
    raw = (file_type or "").strip()

    if normalize_token(raw) == "url" and path.lower().endswith(".txt"):
        return "txt"
    if not raw:
        inferred = infer_extension_when_missing(path)
        return inferred or None
    if any(ch in raw for ch in " \t\n"):
        return extension_from_filename(path) or infer_extension_when_missing(path) or None

    token = normalize_token(raw)
    if not is_plausible_extension_token(raw, token):
        return extension_from_filename(path) or infer_extension_when_missing(path) or None
    return None


def category_for_extension(ext: str) -> str:
    if not ext:
        return "OTHER_PROJECT"
    token = normalize_token(ext)
    if token in QDA_EXTENSIONS :
        return "QDA_PROJECT"
    if token in QD_EXTENSIONS :
        return "QD_PROJECT"
    if token in NOT_A_PROJECT_EXTENSIONS:
        return "NOT_A_PROJECT"
    return "OTHER_PROJECT"


def archive_extensions_from_path(archive_path: Path) -> list[str]:
    extensions: list[str] = []
    if archive_path.suffix.lower() == ".zip" and archive_path.exists():
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for name in zf.namelist():
                    ext = extension_from_filename(name)
                    if ext:
                        extensions.append(ext)
        except Exception:
            pass
    elif archive_path.suffix.lower() == ".rar" and rarfile is not None and archive_path.exists():
        try:
            with rarfile.RarFile(archive_path, "r") as rf:
                for name in rf.namelist():
                    ext = extension_from_filename(name)
                    if ext:
                        extensions.append(ext)
        except Exception:
            pass
    return extensions


def archive_contains_qda(archive_path: Path) -> bool:
    return any(ext in QDA_EXTENSIONS for ext in archive_extensions_from_path(archive_path))

def archive_contains_qd(archive_path: Path) -> bool:
    return any(ext in QD_EXTENSIONS for ext in archive_extensions_from_path(archive_path))


def isic_classification_catalog_path() -> Path:
    return (Path(__file__).resolve().parent / "ISIC5_Exp_Notes_11Mar2024.xlsx - Divisions.csv").resolve()


def load_isic_classification_catalog(csv_path: Path | None = None) -> list[dict[str, str]]:
    """Reads the division-level ISIC Rev. 5 catalog (one row per division,
    e.g. code "A01", title "Crop and animal production..."). The first
    column ("ISIC5 lable class") is already at the target granularity for
    classify_project_isic_class, so every row is a candidate label sent to
    the model as-is - no section/group/class rollup needed."""
    path = Path(csv_path) if csv_path is not None else isic_classification_catalog_path()
    if not path.exists():
        return []

    catalog: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw_code = str(row.get("ISIC5 lable class", "") or "").strip().upper().replace(" ", "")
            title = str(row.get("title", "") or "").strip()
            if raw_code and title:
                catalog.append({"code": raw_code, "title": title, "intro": ""})
    return catalog




# ============================================================
# .env LOADING (HF_API_TOKEN, optionally HF_MODEL)
# ============================================================

def load_dotenv_file() -> None:
    """Loads .env via python-dotenv, checking next to this script first
    (Data classification/.env - where it's actually kept) and falling back
    to the repo root (.env) if that one isn't present. load_dotenv() only
    fills in variables not already set in the environment."""
    load_dotenv(DATA_CLASSIFICATION_DIR / ".env")
    load_dotenv(repo_root() / ".env")


# ============================================================
# GEMMA 3 27B VIA HUGGING FACE'S HOSTED INFERENCE API
# ============================================================

# https://huggingface.co/google/gemma-3-27b-it lists Featherless AI as its
# Inference Providers host, so the provider is pinned explicitly via HF's
# documented "model:provider" suffix (https://huggingface.co/docs/inference-providers)
# rather than relying on auto ("fastest") provider selection picking it up.
HF_MODEL = os.environ.get("HF_MODEL", "google/gemma-3-27b-it:featherless-ai")

_inference_client: InferenceClient | None = None
_inference_client_token: str | None = None


def get_inference_client() -> InferenceClient | None:
    """Lazily built, cached huggingface_hub InferenceClient - rebuilt only
    if the token changes. Returns None if no token is configured."""
    global _inference_client, _inference_client_token
    token = os.environ.get("HF_API_TOKEN") or os.environ.get("HF_TOKEN")
    if not token:
        return None
    if _inference_client is None or _inference_client_token != token:
        _inference_client = InferenceClient(api_key=token)
        _inference_client_token = token
    return _inference_client


def estimate_token_count(text: str, *, chars_per_token: float = 4.0) -> int:
    """Same ~4-chars/token approximation used by truncate_to_token_budget,
    for logging how big a request actually is - no real tokenizer dependency."""
    text = text or ""
    return max(1, round(len(text) / chars_per_token)) if text else 0


PAYMENT_REQUIRED_PAUSE_SECONDS = 5 * 60


def call_huggingface_gemma(text: str, labels: list[str], *, max_tokens: int = 20, retries: int = 1) -> str | None:
    """One classification call to Gemma 3 27B via Hugging Face's hosted
    Inference Providers API, through the official huggingface_hub client.
    Requires HF_API_TOKEN (or HF_TOKEN) to be set - see .env / load_dotenv().
    Returns the raw model reply text, or None if the token is missing or
    the request fails for any reason (network, auth, model unavailable) -
    every outcome is printed so it's visible whether a real answer came
    back or the caller is about to fall back to the first candidate label.

    A 402 (out of Inference Providers credits) is treated as transient
    rather than fatal: it pauses PAYMENT_REQUIRED_PAUSE_SECONDS and retries
    the same call indefinitely, since credits/quota can come back (top-up,
    daily reset) and the caller's already-classified projects must not be
    reclassified/re-billed on the next run."""
    client = get_inference_client()
    if client is None:
        print("[LLM] HF_API_TOKEN/HF_TOKEN not set - skipping API call, caller will use the fallback label")
        return None

    prompt = (
        f"Classify the following text into exactly one of these labels: {', '.join(labels)}.\n\n"
        f"Text:\n{text}\n\n"
        "Respond with only the single best and deepest matching label and nothing else."
    )
    prompt_tokens = estimate_token_count(prompt)
    print(f"[LLM] -> {HF_MODEL}: {len(labels)} candidate label(s), prompt ~{prompt_tokens} tokens ({len(prompt)} chars)")

    attempts = max(1, retries + 1)
    while True:
        payment_required = False
        for attempt in range(1, attempts + 1):
            try:
                completion = client.chat.completions.create(
                    model=HF_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=0,
                )
                content = str(completion.choices[0].message.content or "").strip()
                print(f"[LLM] <- response: {content!r}")
                return content
            except HfHubHTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                print(f"[LLM] <- HTTP error from Hugging Face (attempt {attempt}/{attempts}): {exc}")
                if status == 402:
                    payment_required = True
                    break
                if status in (401, 403):
                    print("[LLM] Auth/permission error - check HF_API_TOKEN has 'Make calls to Inference "
                          "Providers' permission. Not retrying.")
                    return None
                if status == 404:
                    print(f"[LLM] Model/provider not found ({HF_MODEL}) - check HF_MODEL is correct. Not retrying.")
                    return None
            except Exception as exc:
                print(f"[LLM] <- request failed (attempt {attempt}/{attempts}): {exc}")

        if not payment_required:
            return None

        print(f"[LLM] 402 Payment Required - your Hugging Face account has no Inference Providers "
              f"credits left. Pausing {PAYMENT_REQUIRED_PAUSE_SECONDS // 60} minutes before retrying "
              "the same request (already-classified projects are safe - they're committed to the "
              "database as soon as they're classified)...")
        time.sleep(PAYMENT_REQUIRED_PAUSE_SECONDS)
        print("[LLM] resuming after pause")


def match_isic_label(answer: str, labels: list[str]) -> str | None:
    """Matches one Gemma answer fragment against the candidate label list.
    Exact-match first, then longest-substring-wins: ISIC candidate codes
    are hierarchical ("A01" is a literal substring of "A0111"), so naively
    returning the first substring match could return a coarser code than
    the one Gemma actually picked."""
    if not answer:
        return None
    upper = answer.upper()
    cleaned = re.sub(r"[^A-Z0-9]", "", upper)
    for label in labels:
        if re.sub(r"[^A-Z0-9]", "", label.upper()) == cleaned:
            return label

    substring_matches = [label for label in labels if label.upper() in upper]
    if substring_matches:
        return max(substring_matches, key=len)
    return None


LLM_BATCH_SIZE = 7

_BATCH_ANSWER_LINE_RE = re.compile(r"(\d+)\s*[:\-]\s*([A-Za-z0-9]+)")


def run_yes_no_batch_check(prompt_text: str, projects: list[dict]) -> dict[int, bool]:
    """Runs one Gemma batch call expecting a YES/NO verdict per project
    (numbered by its 1-based position in `projects`), returns id -> bool.
    A project with no parseable answer line defaults to False, same as an
    explicit "NO" - it just falls through to whatever check runs next."""
    results = {p["id"]: False for p in projects}
    if not projects:
        return results
    answer = call_huggingface_gemma(prompt_text, ["YES", "NO"], max_tokens=10 * len(projects))
    if not answer:
        return results
    by_position = {i: p["id"] for i, p in enumerate(projects, start=1)}
    for match in _BATCH_ANSWER_LINE_RE.finditer(answer):
        position = int(match.group(1))
        project_id = by_position.get(position)
        if project_id is None:
            continue
        results[project_id] = match.group(2).strip().upper() == "YES"
    return results


# ============================================================
# CLASSIFICATION TASK 1: type (QDA_PROJECT / QD_PROJECT / OTHER_PROJECT)
# ============================================================

def truncate_to_token_budget(text: str, *, min_tokens: int = 150, max_tokens: int = 350, chars_per_token: float = 4.0) -> str:
    """Rough truncation to a token budget without a real tokenizer - uses
    the commonly-cited ~4-characters-per-token approximation for English
    text, targeting the middle of the requested 300-350 range, cut at a
    word boundary."""
    text = (text or "").strip()
    if not text:
        return ""
    target_tokens = (min_tokens + max_tokens) / 2
    max_chars = int(target_tokens * chars_per_token)
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "..."


def has_qda_extension(extensions: Iterable[str]) -> bool:
    return any(category_for_extension(ext) == "QDA_PROJECT" for ext in extensions)


def determine_project_type_from_signals(query_string: str, extensions: Iterable[str]) -> str | None:
    """Deterministic pre-check, no LLM call: a QDA-software file extension
    settles QDA_PROJECT outright; otherwise a query_string keyword hit
    settles QD_PROJECT. Returns None when neither signal fires, meaning
    only the LLM description/file-name checks in classify_projects_type_batch
    can decide."""
    if has_qda_extension(extensions):
        return "QDA_PROJECT"
    if check_query_string_for_qd(query_string):
        return "QD_PROJECT"
    return None


def build_qda_software_check_text(projects: list[dict]) -> str:
    software_list = ", ".join(sorted(QDA_SOFTWARE))
    project_blocks = "\n".join(
        f"Project {i}: description={truncate_to_token_budget(p['description']) or '(none)'}"
        for i, p in enumerate(projects, start=1)
    )
    return (
        f"Known QDA (qualitative data analysis) software: {software_list}.\n\n"
        "For EACH project below, decide whether its description indicates "
        "the project was built with, or uses, one of these QDA software "
        "tools:\n"
        f"{project_blocks}\n\n"
        "Respond with exactly one line per project, formatted as "
        "\"<project number>: YES\" or \"<project number>: NO\", nothing else."
    )


def build_qualitative_data_check_text(projects: list[dict]) -> str:
    project_blocks = "\n".join(
        f"Project {i}: description={truncate_to_token_budget(p['description']) or '(none)'}; "
        f"files={', '.join(p['file_names'][:5]) or '(none)'}"
        for i, p in enumerate(projects, start=1)
    )
    return (
        "For EACH project below, decide whether it involves qualitative "
        "data (e.g. interview transcripts, coded text, thematic analysis, "
        "a survey/questionnaire) based on its description and file names - "
        "for example a file named \"questionnaire\" (any extension) is a "
        "strong signal:\n"
        f"{project_blocks}\n\n"
        "Respond with exactly one line per project, formatted as "
        "\"<project number>: YES\" or \"<project number>: NO\", nothing else."
    )


def classify_projects_type_batch(projects: list[dict]) -> dict[int, str]:
    """Resolves QDA_PROJECT / QD_PROJECT / OTHER_PROJECT for a batch of
    projects (dicts with id/description/query_string/extensions/file_names).
    The deterministic extension/query_string check runs first and settles a
    project without any LLM call. Only what's still undecided goes to
    Gemma - first a batched YES/NO check for QDA-software use, then (for
    whatever's still undecided after that) a batched YES/NO check for
    qualitative-data use - so a project already settled by QDA_PROJECT
    never gets asked about QD_PROJECT. Anything left undecided after both
    LLM checks (e.g. a project with no files and a generic description) is
    OTHER_PROJECT - every project gets a result, so none are left at the
    PENDING_PROJECT_TYPE sentinel."""
    results: dict[int, str] = {}
    undecided = []
    for p in projects:
        settled = determine_project_type_from_signals(p["query_string"], p["extensions"])
        if settled:
            results[p["id"]] = settled
        else:
            undecided.append(p)

    if undecided:
        qda_hits = run_yes_no_batch_check(build_qda_software_check_text(undecided), undecided)
        still_undecided = []
        for p in undecided:
            if qda_hits.get(p["id"]):
                results[p["id"]] = "QDA_PROJECT"
            else:
                still_undecided.append(p)
        undecided = still_undecided

    if undecided:
        qd_hits = run_yes_no_batch_check(build_qualitative_data_check_text(undecided), undecided)
        for p in undecided:
            results[p["id"]] = "QD_PROJECT" if qd_hits.get(p["id"]) else "OTHER_PROJECT"

    return results


# ============================================================
# CLASSIFICATION TASK 2: class (ISIC Rev. 5 section + 2-digit division)
# ============================================================

def build_isic_batch_classification_text(projects: list[dict], catalog: list[dict[str, str]]) -> str:
    catalog_lines = "\n".join(
        f"{item['code']}: {item['title']}" for item in catalog if item.get("code") and item.get("title")
    )
    project_blocks = "\n".join(
        f"Project {i}: title={p['title'] or '(none)'}; "
        f"description={truncate_to_token_budget(p['description']) or '(none)'}"
        for i, p in enumerate(projects, start=1)
    )
    print(
        f"[LLM] batch class text: {len(projects)} project(s), "
        f"catalog ~{estimate_token_count(catalog_lines)} tokens ({len(catalog)} candidates)"
    )
    return (
        "ISIC Rev. 5 division-level candidate activities - for EACH project "
        "listed below, pick whichever single entry most closely matches:\n"
        f"{catalog_lines}\n\n"
        f"{project_blocks}\n\n"
        "Respond with exactly one line per project, formatted as "
        "\"<project number>: <code>\" (e.g. \"1: A01\"), and nothing else."
    )


def classify_projects_isic_class_batch(projects: list[dict], catalog: list[dict[str, str]]) -> dict[int, str]:
    """Classifies up to LLM_BATCH_SIZE projects in a single Gemma call -
    one call per batch instead of one per project keeps API usage/credits
    down. The reply is matched back to each project's id by its 1-based
    position in the batch; any project whose line is missing or doesn't
    match a candidate falls back to the first candidate label."""
    candidate_labels = list(dict.fromkeys(item["code"] for item in catalog if item.get("code")))
    fallback_label = candidate_labels[0] if candidate_labels else "UNKNOWN"
    results = {p["id"]: fallback_label for p in projects}
    if not candidate_labels or not projects:
        return results

    text = build_isic_batch_classification_text(projects, catalog)
    answer = call_huggingface_gemma(text, candidate_labels, max_tokens=20 * len(projects))
    if not answer:
        print(f"[LLM] no response for batch of {len(projects)} project(s) - "
              f"using fallback label {fallback_label!r} for all of them")
        return results

    by_position = {i: p["id"] for i, p in enumerate(projects, start=1)}
    for match in _BATCH_ANSWER_LINE_RE.finditer(answer):
        position = int(match.group(1))
        project_id = by_position.get(position)
        if project_id is None:
            continue
        matched = match_isic_label(match.group(2), candidate_labels)
        if matched:
            results[project_id] = matched
        else:
            print(f"[LLM] project {position} answer {match.group(2)!r} matched no candidate - using fallback")

    return results


def check_query_string_for_qd(query_string: str | None) -> bool:
    if not query_string:
        return False
    lowered = (query_string or "").lower().replace("-", " ").replace("_", " ")
    return any(keyword in lowered for keyword in QD_KEYWORDS)


def archive_contains_qda_software(archive_path: Path) -> bool:
    exts = archive_extensions_from_path(archive_path)
    for ext in exts:
        if ext in QDA_EXTENSIONS:
            return True
    lowered_names = " ".join(exts).lower()
    return any(software in lowered_names for software in QDA_SOFTWARE)




def repair_files(conn: sqlite3.Connection, files_table: str = "FILES") -> int:
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(f'SELECT id, file_name, file_type FROM "{files_table}"')

    count = 0
    for row in cursor.fetchall():
        repaired = cleaned_file_type(str(row["file_name"] or ""), str(row["file_type"] or ""))
        if repaired is not None and normalize_token(str(row["file_type"] or "")) != repaired:
            count += 1
    return count


def table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall())


def assign_project_classes(
    conn: sqlite3.Connection, cursor: sqlite3.Cursor, *, files_table: str, projects_table: str,
    has_query_string: bool, limit: int | None, batch_size: int,
) -> tuple[int, int]:
    """Classifies every project whose class is still 'UNKNOWN' AND whose
    type is already QDA_PROJECT or QD_PROJECT (OTHER_PROJECT/NOT_A_PROJECT
    projects are skipped - an ISIC economic-activity class isn't meaningful
    for them, and skipping them saves LLM calls). Requires
    assign_project_types to have already run, since it relies on `type`
    being resolved rather than still sitting at PENDING_PROJECT_TYPE.
    Classifies batch_size at a time, committing each project's class - and
    its files' class - to the database the moment that project is
    classified (instead of batching every update to the very end of the
    run). That makes a run resumable at project granularity: re-running
    only ever fetches WHERE class = 'UNKNOWN', so a project already
    classified in a prior run (or earlier in this one) is never re-sent to
    the LLM/re-billed, and a crash or a call_huggingface_gemma 402 pause
    loses at most the in-flight batch. To force reclassification of
    specific projects, set their class column back to 'UNKNOWN' before
    running."""
    select_cols = "id, description, title" + (", query_string" if has_query_string else "")
    catalog = load_isic_classification_catalog()

    project_updates = 0
    file_updates = 0
    while limit is None or project_updates < limit:
        fetch_size = batch_size if limit is None else min(batch_size, limit - project_updates)
        cursor.execute(
            f'SELECT {select_cols} FROM "{projects_table}" '
            'WHERE class = "UNKNOWN" AND type IN ("QDA_PROJECT", "QD_PROJECT") LIMIT ?',
            (fetch_size,),
        )
        batch_rows = cursor.fetchall()
        if not batch_rows:
            break

        projects = [
            {
                "id": int(row["id"]),
                "title": str(row["title"] or ""),
                "description": str(row["description"] or ""),
            }
            for row in batch_rows
        ]

        class_by_id = classify_projects_isic_class_batch(projects, catalog)

        for project in projects:
            project_id = project["id"]
            class_label = class_by_id.get(project_id, "UNKNOWN")

            cursor.execute(
                f'UPDATE "{projects_table}" SET class = ? WHERE id = ?',
                (class_label, project_id),
            )
            files_cursor = cursor.execute(
                f'UPDATE "{files_table}" SET class = ? WHERE project_id = ?',
                (class_label, project_id),
            )
            conn.commit()
            project_updates += 1
            file_updates += max(files_cursor.rowcount, 0)

        print(f"[INFO] Classified {project_updates} project(s) so far...")

    return file_updates, project_updates


def assign_project_types(
    conn: sqlite3.Connection, cursor: sqlite3.Cursor, *, projects_table: str,
    has_query_string: bool, ext_by_project: dict[int, list[str]], file_names_by_project: dict[int, list[str]],
    limit: int | None, batch_size: int,
) -> int:
    """Determines every project whose type is still PENDING_PROJECT_TYPE
    ('NOT_A_PROJECT'), batch_size at a time, committing each project's type
    the moment it's decided. Mirrors assign_project_classes for
    resumability: re-running only ever fetches WHERE type =
    PENDING_PROJECT_TYPE, so an already-typed project is never re-sent to
    the LLM. Every project gets a real answer (QDA_PROJECT / QD_PROJECT /
    OTHER_PROJECT) via classify_projects_type_batch, so none are left
    sitting at the pending sentinel once a full run completes."""
    select_cols = "id, description" + (", query_string" if has_query_string else "")

    project_updates = 0
    while limit is None or project_updates < limit:
        fetch_size = batch_size if limit is None else min(batch_size, limit - project_updates)
        cursor.execute(
            f'SELECT {select_cols} FROM "{projects_table}" WHERE type = ? LIMIT ?',
            (PENDING_PROJECT_TYPE, fetch_size),
        )
        batch_rows = cursor.fetchall()
        if not batch_rows:
            break

        projects = [
            {
                "id": int(row["id"]),
                "description": str(row["description"] or ""),
                "query_string": str(row["query_string"] or "") if has_query_string else "",
                "extensions": ext_by_project.get(int(row["id"]), []),
                "file_names": file_names_by_project.get(int(row["id"]), []),
            }
            for row in batch_rows
        ]

        type_by_id = classify_projects_type_batch(projects)

        for project in projects:
            project_id = project["id"]
            type_label = type_by_id.get(project_id, "OTHER_PROJECT")

            cursor.execute(
                f'UPDATE "{projects_table}" SET type = ? WHERE id = ?',
                (type_label, project_id),
            )
            conn.commit()
            project_updates += 1

        print(f"[INFO] Determined type for {project_updates} project(s) so far...")

    return project_updates


def assign_types_and_classes(
    conn: sqlite3.Connection, *, files_table: str = "FILES", projects_table: str = "PROJECTS",
    limit: int | None = None, batch_size: int = LLM_BATCH_SIZE,
) -> tuple[int, int, int, int]:
    """Runs the type task (assign_project_types) first, then the class task
    (assign_project_classes), as two resumable passes - a project is only
    ever sent to the LLM for whichever of type/class is still pending for
    it (type = PENDING_PROJECT_TYPE / class = 'UNKNOWN'). Type must run
    first: assign_project_classes only classifies projects whose type is
    already QDA_PROJECT or QD_PROJECT, skipping OTHER_PROJECT/
    NOT_A_PROJECT."""
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    repaired_count = repair_files(conn, files_table=files_table)

    cursor.execute(f'SELECT id, project_id, file_name, file_type FROM "{files_table}"')
    file_rows = cursor.fetchall()
    ext_by_project: dict[int, list[str]] = {}
    file_names_by_project: dict[int, list[str]] = {}
    for row in file_rows:
        project_id = int(row["project_id"])
        file_name = str(row["file_name"] or "")
        if len(file_names_by_project.get(project_id, [])) < 20:
            file_names_by_project.setdefault(project_id, []).append(basename(file_name))
        ext = effective_extension(file_name, str(row["file_type"] or ""))
        if not ext:
            ext = infer_extension_when_missing(file_name)

        if ext in {"zip", "rar"}:
            archive_path = Path(file_name)
            if not archive_path.is_absolute():
                archive_path = Path.cwd() / archive_path
            if archive_path.exists():
                if archive_contains_qda_software(archive_path):
                    ext_by_project.setdefault(project_id, []).append("qda")
                elif archive_contains_qda(archive_path):
                    ext_by_project.setdefault(project_id, []).append("qda")
                else:
                    ext_by_project.setdefault(project_id, []).append(ext)
            else:
                ext_by_project.setdefault(project_id, []).append(ext)
        else:
            ext_by_project.setdefault(project_id, []).append(ext)

    has_query_string = table_has_column(conn, projects_table, "query_string")

    project_type_updates = assign_project_types(
        conn, cursor, projects_table=projects_table, has_query_string=has_query_string,
        ext_by_project=ext_by_project, file_names_by_project=file_names_by_project,
        limit=limit, batch_size=batch_size,
    )

    file_updates, project_class_updates = assign_project_classes(
        conn, cursor, files_table=files_table, projects_table=projects_table,
        has_query_string=has_query_string, limit=limit, batch_size=batch_size,
    )

    return repaired_count, file_updates, project_class_updates, project_type_updates


def main() -> None:
    load_dotenv_file()

    parser = argparse.ArgumentParser(description="Assign class values via Gemma 3 27B (Hugging Face) and type values via extension/query heuristics")
    parser.add_argument("--db", type=Path, default=default_database_path(), help="Path to the SQLite database")
    parser.add_argument("--limit", type=int, default=None, help="Only process the next N still-pending projects per task (class = 'UNKNOWN' / type = 'NOT_A_PROJECT') - for a cheap test run before committing to a full pass")
    parser.add_argument("--batch-size", type=int, default=LLM_BATCH_SIZE, help=f"Number of projects sent to Gemma per API call (default {LLM_BATCH_SIZE})")
    args = parser.parse_args()

    db_path = args.db.resolve() if args.db.is_absolute() else (Path.cwd() / args.db).resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    if not (os.environ.get("HF_API_TOKEN") or os.environ.get("HF_TOKEN")):
        print("[WARN] HF_API_TOKEN not set (checked .env and the environment) - "
              "Gemma classification will fall back to the first candidate label for every project.")

    conn = sqlite3.connect(db_path)
    try:
        repaired_count, file_updates, project_class_updates, project_type_updates = assign_types_and_classes(
            conn, limit=args.limit, batch_size=args.batch_size
        )
        print(f"Updated database: {db_path}")
        print(f"Scanned file_type values: {repaired_count}")
        print(f"Updated file class values: {file_updates}")
        print(f"Updated project class values: {project_class_updates}")
        print(f"Updated project type values: {project_type_updates}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
