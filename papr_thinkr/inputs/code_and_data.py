"""Code and data that accompany a manuscript: collect files from the paths
the author gave (files or whole directories), summarise data files (CSV/TSV/
XLSX/JSON column stats, row counts, missingness), render notebooks as text,
build a size-bounded "dossier" for the code-audit pass, and run deterministic
reproducibility checks (no README, unpinned dependencies, absolute paths,
missing random seeds, data files the code reads but that weren't provided...)."""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from .documents import decode_bytes
from .matfile import describe_variable, summarize_mat

CODE_EXTS = {
    ".py", ".r", ".rmd", ".qmd", ".ipynb", ".m", ".jl", ".js", ".ts", ".c", ".cc", ".cpp", ".h",
    ".hpp", ".java", ".scala", ".go", ".rs", ".sh", ".bash", ".ps1", ".bat", ".sql", ".stan",
    ".do", ".ado", ".sas", ".sps", ".f90", ".f", ".pl", ".rb", ".swift", ".kt", ".cs", ".mat_script",
    ".nf", ".smk", ".wdl", ".cwl",
}
CONFIG_EXTS = {".yaml", ".yml", ".toml", ".cfg", ".ini", ".lock"}
CONFIG_NAMES = {
    "makefile", "dockerfile", "snakefile", "requirements.txt", "environment.yml", "environment.yaml",
    "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "pipfile.lock", "renv.lock", "description",
    "project.toml", "manifest.toml", "package.json", "conda.yml", "conda.yaml", "install.r",
    "cargo.toml", "go.mod", "citation.cff", ".zenodo.json",
}
DOC_PREFIXES = ("readme", "license", "licence", "copying", "codebook", "data_dictionary", "datadictionary")
DATA_TEXT_EXTS = {".csv", ".tsv", ".tab", ".txt", ".dat", ".json", ".jsonl", ".ndjson"}
DATA_BINARY_EXTS = {
    ".xlsx", ".xls", ".parquet", ".feather", ".arrow", ".h5", ".hdf5", ".nc", ".mat", ".rds", ".rdata",
    ".rda", ".sav", ".dta", ".sas7bdat", ".npy", ".npz", ".pkl", ".pickle", ".zarr", ".fits", ".nii",
    ".gz", ".zip", ".db", ".sqlite", ".edf", ".bdf", ".fif", ".tif", ".tiff",
}
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".env",
    ".ipynb_checkpoints", ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vscode", "build",
    "dist", ".Rproj.user", "renv", "site-packages", ".cache", ".DS_Store", "packrat",
}
MAX_FILES = 3000
MAX_CODE_FILE_CHARS = 60_000
MAX_DATA_ROWS = 200_000

DEP_FILES = {"requirements.txt", "environment.yml", "environment.yaml", "pyproject.toml", "setup.py",
             "setup.cfg", "pipfile", "pipfile.lock", "renv.lock", "description", "project.toml",
             "manifest.toml", "package.json", "conda.yml", "conda.yaml", "dockerfile", "cargo.toml",
             "go.mod", "install.r"}


@dataclass
class FileEntry:
    path: Path
    rel: str
    size: int
    kind: str              # code | config | doc | data | other


@dataclass
class Dossier:
    text: str = ""
    files: list[FileEntry] = field(default_factory=list)
    data_summaries: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "n_files": len(self.files),
            "n_code_files": sum(1 for f in self.files if f.kind == "code"),
            "n_data_files": sum(1 for f in self.files if f.kind == "data"),
            "files": [{"rel": f.rel, "size": f.size, "kind": f.kind} for f in self.files[:400]],
            "data_summaries": self.data_summaries,
            "truncated": self.truncated,
            "notes": self.notes,
            "dossier_chars": len(self.text),
        }


def _classify(p: Path, role: str) -> str:
    name = p.name.lower()
    ext = p.suffix.lower()
    if name in CONFIG_NAMES:
        return "config"
    if name.startswith(DOC_PREFIXES):
        return "doc"
    if ext in CODE_EXTS:
        return "code"
    if ext in DATA_BINARY_EXTS or (ext in DATA_TEXT_EXTS and (role == "data" or ext != ".txt")):
        return "data"
    if ext in CONFIG_EXTS:
        return "config"
    if ext in (".md", ".rst", ".txt"):
        return "doc"
    return "other"


def collect(paths: list[str], role: str) -> tuple[list[FileEntry], list[str]]:
    """Walk the given files/directories. role is 'code' or 'data' (affects how
    ambiguous files like .txt are classified)."""
    entries: list[FileEntry] = []
    notes: list[str] = []
    for raw in paths or []:
        raw = (raw or "").strip().strip('"')
        if not raw:
            continue
        root = Path(raw).expanduser()
        if not root.exists():
            notes.append(f"Path not found: {raw}")
            continue
        if root.is_file():
            entries.append(FileEntry(root, root.name, root.stat().st_size, _classify(root, role)))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for fn in sorted(filenames):
                if fn.startswith(".") and fn.lower() != ".zenodo.json":
                    continue
                p = Path(dirpath) / fn
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                rel = f"{root.name}/{p.relative_to(root).as_posix()}"
                entries.append(FileEntry(p, rel, size, _classify(p, role)))
                if len(entries) >= MAX_FILES:
                    notes.append(f"Stopped after {MAX_FILES} files under {root}.")
                    return entries, notes
    return entries, notes


# ---------------------------------------------------------------------------
# Data summaries
# ---------------------------------------------------------------------------

def _num(s: str):
    try:
        v = float(s)
    except ValueError:
        if not re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):   # 1,234.5
            return None
        v = float(s.replace(",", ""))
    return v if math.isfinite(v) else None


_MISSING = {"", "na", "n/a", "nan", "null", "none", ".", "-", "missing", "#n/a"}
_PII_RE = re.compile(r"^(first_?name|last_?name|full_?name|name|surname|e-?mail|phone|telephone|address|"
                     r"street|zip|postcode|postal_?code|dob|date_?of_?birth|birth_?date|ssn|social_?security|"
                     r"mrn|medical_?record|patient_?name|nhs_?number|ip_?address|national_?id)$", re.I)


def summarize_table(rows_iter, header: list[str], max_rows: int = MAX_DATA_ROWS) -> dict:
    ncol = len(header)
    stats = [{"name": h or f"col{i+1}", "n": 0, "missing": 0, "numeric": 0, "min": None, "max": None,
              "sum": 0.0, "sumsq": 0.0, "distinct": set(), "distinct_overflow": False}
             for i, h in enumerate(header)]
    n_rows = 0
    ragged = 0
    seen_rows: set[int] = set()
    dup_rows = 0
    for row in rows_iter:
        if n_rows >= max_rows:
            break
        n_rows += 1
        if len(row) != ncol:
            ragged += 1
        h = hash(tuple(row))
        if h in seen_rows:
            dup_rows += 1
        elif len(seen_rows) < 500_000:
            seen_rows.add(h)
        for i in range(min(ncol, len(row))):
            v = (row[i] or "").strip()
            st = stats[i]
            st["n"] += 1
            if v.lower() in _MISSING:
                st["missing"] += 1
                continue
            x = _num(v)
            if x is not None:
                st["numeric"] += 1
                st["min"] = x if st["min"] is None else min(st["min"], x)
                st["max"] = x if st["max"] is None else max(st["max"], x)
                st["sum"] += x
                st["sumsq"] += x * x
            if not st["distinct_overflow"]:
                st["distinct"].add(v[:80])
                if len(st["distinct"]) > 200:
                    st["distinct_overflow"] = True
    cols = []
    for st in stats:
        present = st["n"] - st["missing"]
        col = {"name": st["name"], "missing": st["missing"],
               "missing_pct": round(100 * st["missing"] / st["n"], 1) if st["n"] else 0.0}
        if present and st["numeric"] >= 0.9 * present:
            mean = st["sum"] / st["numeric"]
            var = max(0.0, st["sumsq"] / st["numeric"] - mean * mean)
            col.update(type="numeric", min=_r(st["min"]), max=_r(st["max"]), mean=_r(mean), sd=_r(math.sqrt(var)))
        else:
            col["type"] = "categorical"
            col["distinct"] = ">200" if st["distinct_overflow"] else len(st["distinct"])
            if not st["distinct_overflow"]:
                col["examples"] = sorted(st["distinct"])[:6]
        if present and not st["distinct_overflow"] and len(st["distinct"]) == 1:
            col["constant"] = True
        cols.append(col)
    return {"n_rows": n_rows, "n_cols": ncol, "rows_capped": n_rows >= max_rows,
            "ragged_rows": ragged, "duplicate_rows": dup_rows, "columns": cols}


def _r(x):
    if x is None:
        return None
    return float(f"{x:.4g}")


def summarize_delimited(path: Path) -> dict:
    raw = path.read_bytes()[:50_000_000]
    text, _meta = decode_bytes(raw)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() in (".tsv", ".tab") else csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        return {"error": "empty file"}
    has_header = True
    try:
        has_header = csv.Sniffer().has_header(sample)
    except csv.Error:
        pass
    if not has_header:
        first = header
        header = [f"col{i+1}" for i in range(len(first))]
        rows = _chain([first], reader)
    else:
        rows = reader
    return summarize_table(rows, header)


def _chain(first, rest):
    yield from first
    yield from rest


def summarize_xlsx(path: Path) -> dict:
    """Sheet names and dimensions, plus a full summary of the first sheet --
    using only zipfile + XML (no openpyxl)."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        with zipfile.ZipFile(path) as z:
            wb = ElementTree.fromstring(z.read("xl/workbook.xml"))
            sheets = [s.get("name") for s in wb.findall("m:sheets/m:sheet", ns)]
            shared: list[str] = []
            if "xl/sharedStrings.xml" in z.namelist():
                sst = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
                for si in sst.findall("m:si", ns):
                    shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
            first = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
            out: dict = {"sheets": sheets}
            if not first:
                return out
            ws = ElementTree.fromstring(z.read(first[0]))
            dim = ws.find("m:dimension", ns)
            out["first_sheet_dimension"] = dim.get("ref") if dim is not None else None
            rows = []
            for r in ws.iter(f"{{{ns['m']}}}row"):
                vals: dict[int, str] = {}
                for c in r.findall("m:c", ns):
                    ref = c.get("r", "")
                    col = _col_index(re.sub(r"\d", "", ref)) if ref else len(vals)
                    v = c.find("m:v", ns)
                    t = c.get("t")
                    if t == "s" and v is not None and v.text is not None:
                        val = shared[int(v.text)] if int(v.text) < len(shared) else ""
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
                    else:
                        val = v.text if v is not None and v.text is not None else ""
                    vals[col] = val
                if vals:
                    width = max(vals) + 1
                    rows.append([vals.get(i, "") for i in range(width)])
                if len(rows) > MAX_DATA_ROWS:
                    break
            if rows:
                width = max(len(r) for r in rows)
                header = rows[0] + [""] * (width - len(rows[0]))
                body = (r + [""] * (width - len(r)) for r in rows[1:])
                out.update(summarize_table(body, header))
            return out
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        return {"error": f"could not read workbook: {e}"}


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return max(0, n - 1)


def summarize_json(path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) > 20_000_000:
        return {"note": "large JSON file; not parsed"}
    text = decode_bytes(raw)[0]
    try:
        if path.suffix.lower() in (".jsonl", ".ndjson"):
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            records = json.loads(text)
    except json.JSONDecodeError as e:
        return {"error": f"invalid JSON: {e}"}
    if isinstance(records, list) and records and all(isinstance(r, dict) for r in records[:100]):
        keys: list[str] = []
        for r in records[:1000]:
            for k in r:
                if k not in keys:
                    keys.append(k)
        rows = ([str(r.get(k, "")) if not isinstance(r.get(k), (dict, list)) else "<nested>" for k in keys]
                for r in records)
        return summarize_table(rows, keys)
    if isinstance(records, dict):
        return {"top_level_keys": list(records.keys())[:40]}
    return {"type": type(records).__name__, "length": len(records) if hasattr(records, "__len__") else None}


def summarize_data_file(entry: FileEntry) -> dict:
    ext = entry.path.suffix.lower()
    out = {"file": entry.rel, "size_bytes": entry.size}
    try:
        if ext in (".csv", ".tsv", ".tab", ".dat") or (ext == ".txt" and entry.kind == "data"):
            out.update(summarize_delimited(entry.path))
        elif ext == ".xlsx":
            out.update(summarize_xlsx(entry.path))
        elif ext in (".json", ".jsonl", ".ndjson"):
            out.update(summarize_json(entry.path))
        elif ext == ".mat":
            out.update(summarize_mat(entry.path))
        else:
            out["note"] = f"binary format ({ext}); contents not inspected"
    except Exception as e:  # never let one odd file break the review
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def format_data_summary(s: dict) -> str:
    lines = [f"DATA FILE {s['file']} ({_human(s.get('size_bytes', 0))})"]
    for k in ("error", "note"):
        if s.get(k):
            lines.append(f"  {k}: {s[k]}")
    if s.get("sheets"):
        lines.append(f"  sheets: {', '.join(map(str, s['sheets']))}")
    if s.get("variables") is not None:
        lines.append(f"  {s.get('format', 'MAT')} file, {len(s['variables'])} variable(s):")
        for v in s["variables"][:60]:
            lines.extend(describe_variable(v, indent="  - "))
    if "n_rows" in s:
        lines.append(f"  rows: {s['n_rows']}{'+' if s.get('rows_capped') else ''}, columns: {s['n_cols']}, "
                     f"duplicate rows: {s.get('duplicate_rows', 0)}, ragged rows: {s.get('ragged_rows', 0)}")
        for c in s.get("columns", [])[:60]:
            if c.get("type") == "numeric":
                desc = f"numeric min={c['min']} max={c['max']} mean={c['mean']} sd={c['sd']}"
            else:
                ex = ", ".join(map(str, c.get("examples", [])))
                desc = f"categorical distinct={c.get('distinct')}" + (f" e.g. [{ex}]" if ex else "")
            miss = f", missing={c['missing']} ({c['missing_pct']}%)" if c.get("missing") else ""
            lines.append(f"  - {c['name']}: {desc}{miss}{' CONSTANT' if c.get('constant') else ''}")
        if len(s.get("columns", [])) > 60:
            lines.append(f"  ... {len(s['columns']) - 60} more columns")
    if s.get("top_level_keys"):
        lines.append(f"  top-level keys: {', '.join(map(str, s['top_level_keys']))}")
    return "\n".join(lines)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# Code rendering
# ---------------------------------------------------------------------------

def notebook_to_text(path: Path) -> str:
    try:
        nb = json.loads(decode_bytes(path.read_bytes())[0])
    except (json.JSONDecodeError, OSError) as e:
        return f"(could not parse notebook: {e})"
    out = []
    for i, cell in enumerate(nb.get("cells", []), 1):
        src = "".join(cell.get("source", [])) if isinstance(cell.get("source"), list) else str(cell.get("source", ""))
        if cell.get("cell_type") == "code":
            out.append(f"# [cell {i}, code, exec {cell.get('execution_count')}]\n{src}")
        elif cell.get("cell_type") == "markdown":
            out.append(f"# [cell {i}, markdown]\n" + "\n".join("# " + l for l in src.splitlines()))
    return "\n\n".join(out)


def notebook_exec_counts(path: Path) -> list:
    try:
        nb = json.loads(decode_bytes(path.read_bytes())[0])
    except Exception:
        return []
    return [c.get("execution_count") for c in nb.get("cells", []) if c.get("cell_type") == "code"]


def read_code(entry: FileEntry) -> str:
    if entry.path.suffix.lower() == ".ipynb":
        return notebook_to_text(entry.path)
    raw = entry.path.read_bytes()[:MAX_CODE_FILE_CHARS * 2]
    if b"\x00" in raw[:2048]:
        return "(binary file)"
    return decode_bytes(raw)[0]


def _numbered(text: str, max_chars: int) -> tuple[str, bool]:
    lines = text.splitlines()
    width = len(str(len(lines)))
    out = []
    size = 0
    for i, line in enumerate(lines, 1):
        s = f"{i:>{width}}| {line}"
        if size + len(s) > max_chars:
            out.append(f"... [truncated at line {i - 1} of {len(lines)}]")
            return "\n".join(out), True
        out.append(s)
        size += len(s) + 1
    return "\n".join(out), False


# ---------------------------------------------------------------------------
# Static (deterministic) reproducibility checks
# ---------------------------------------------------------------------------

_ABS_PATH_RE = re.compile(r"""["']((?:[A-Za-z]:[\\/]|/Users/|/home/|/mnt/|/media/|/Volumes/|~/)[^"'\n]{2,200})["']""")
_DATA_LITERAL_RE = re.compile(
    r"""["']([^"'\n]{1,200}\.(?:csv|tsv|xlsx|xls|json|jsonl|parquet|feather|h5|hdf5|mat|rds|rdata|rda|sav|dta|"""
    r"""npy|npz|pkl|pickle|txt|dat|nc|zip|gz))["']""", re.I)
_WRITE_HINT_RE = re.compile(r"(to_csv|to_excel|to_parquet|to_json|write|save|dump|savefig|ggsave|export|"
                            r"open\([^)]*['\"]w|sink\(|png\(|pdf\(|output)", re.I)
_RANDOM_PY = re.compile(r"\b(np\.random\.|numpy\.random\.|random\.(random|randint|choice|shuffle|sample|uniform|gauss)|"
                        r"torch\.rand|tf\.random|train_test_split|KFold|StratifiedKFold|RandomForest|"
                        r"bootstrap|permutation|sample\(|resample)")
_SEED_PY = re.compile(r"(seed\s*\(|random_state\s*=|set_seed|manual_seed|default_rng\(\s*\d|RandomState\(\s*\d|PYTHONHASHSEED)")
_RANDOM_R = re.compile(r"\b(sample|rnorm|runif|rbinom|rpois|boot|bootstrap|createDataPartition|randomForest|"
                       r"train|kmeans|permute|replicate)\s*\(")
_SEED_R = re.compile(r"set\.seed\s*\(")
_SETWD = re.compile(r"^\s*setwd\s*\(")
_RM_ALL = re.compile(r"rm\s*\(\s*list\s*=\s*ls\s*\(|^\s*clear\s+all\b|^\s*clearvars\b")
_INSTALL = re.compile(r"install\.packages\s*\(|^\s*!?pip install|BiocManager::install")
_ATTACH = re.compile(r"^\s*attach\s*\(")
_LIBRARY = re.compile(r"^\s*(?:library|require|suppressPackageStartupMessages\(\s*library)\s*\(\s*['\"]?([A-Za-z0-9.]+)")
_DROPS_MISSING = re.compile(r"na\.rm\s*=\s*(TRUE|T)\b|na\.omit\s*\(|complete\.cases\s*\(|!\s*is\.na\s*\(|drop_na\s*\(|"
                            r"\.dropna\s*\(|'omitnan'|\bnanmean\s*\(|rmmissing\s*\(")
_STAT_TEST = re.compile(r"\b(t\.test|wilcox\.test|chisq\.test|fisher\.test|cor\.test|aov|anova|kruskal\.test|"
                        r"ttest2?|ranksum|signrank|anova1|anovan|ttest_ind|ttest_rel|mannwhitneyu)\s*\(")
_P_ADJUST = re.compile(r"p\.adjust|multcomp|emmeans|TukeyHSD|bonferroni|holm|fdr|multcompare|multipletests")
_SESSION = re.compile(r"sessionInfo\s*\(|renv::|groundhog|checkpoint\s*\(|pip freeze|ver\s*$|version\s*\(")


def _f(title, category, severity, issue, suggestion, location=None, quote=None, hours=(0.5, 2),
       level="small", confidence=0.8, rationale=""):
    return {"title": title, "category": category, "severity": severity, "location": location, "quote": quote,
            "issue": issue, "rationale": rationale, "suggestion": suggestion, "effort_level": level,
            "hours_low": hours[0], "hours_high": hours[1], "needs_new_data": False,
            "effort_confidence": 0.7, "confidence": confidence, "persona": "Code & data checks"}


def static_findings(code_entries: list[FileEntry], data_entries: list[FileEntry],
                    code_texts: dict[str, str], data_summaries: list[dict]) -> list[dict]:
    out: list[dict] = []
    all_entries = code_entries + data_entries
    names_lower = {e.path.name.lower() for e in all_entries}
    code_files = [e for e in code_entries if e.kind == "code"]

    if code_entries:
        if not any(n.startswith("readme") for n in names_lower):
            out.append(_f("No README for the analysis code", "reproducibility", "minor",
                          "The code has no README explaining what each script does, in what order to run it, "
                          "and which figure/table each produces.",
                          "Add a README with setup, run order, and a script-to-figure/table map.",
                          hours=(1, 3), confidence=0.9))
        if code_files and not (names_lower & DEP_FILES):
            out.append(_f("Software dependencies are not specified", "reproducibility", "minor",
                          "No dependency specification (requirements.txt, environment.yml, renv.lock, "
                          "DESCRIPTION, pyproject.toml, Dockerfile, ...) was provided, so the computational "
                          "environment cannot be recreated.",
                          "Export the environment with pinned versions (pip freeze / conda env export / renv::snapshot).",
                          hours=(0.5, 2), confidence=0.85))
        if not any(n.startswith(("license", "licence", "copying")) for n in names_lower):
            out.append(_f("No license for the code/data", "reproducibility", "editorial",
                          "No LICENSE file was found, so others cannot legally reuse the code or data.",
                          "Add a license (e.g. MIT/BSD for code, CC-BY for data).", hours=(0.1, 0.5),
                          level="trivial", confidence=0.7))

    abs_hits = []
    py_random, py_seed, r_random, r_seed = [], False, [], False
    setwd_hits, rm_hits, install_hits, attach_hits, drop_hits, test_hits = [], [], [], [], [], []
    packages: list[str] = []
    p_adjusted = records_session = False
    referenced: dict[str, str] = {}
    for e in code_files:
        text = code_texts.get(e.rel, "")
        ext = e.path.suffix.lower()
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "%")) and not stripped.startswith("# [cell"):
                continue
            if not _SETWD.search(line):          # setwd() gets its own finding
                for m in _ABS_PATH_RE.finditer(line):
                    abs_hits.append(f"{e.rel}:{i} {m.group(1)[:80]}")
            for m in _DATA_LITERAL_RE.finditer(line):
                if _WRITE_HINT_RE.search(line):
                    continue
                referenced.setdefault(Path(m.group(1).replace("\\", "/")).name.lower(), f"{e.rel}:{i}")
            if ext in (".py", ".ipynb"):
                if _RANDOM_PY.search(line):
                    py_random.append(f"{e.rel}:{i}")
                if _SEED_PY.search(line):
                    py_seed = True
            if ext in (".r", ".rmd", ".qmd"):
                if _RANDOM_R.search(line):
                    r_random.append(f"{e.rel}:{i}")
                if _SEED_R.search(line):
                    r_seed = True
                if _SETWD.search(line):
                    setwd_hits.append(f"{e.rel}:{i}")
                if _ATTACH.search(line):
                    attach_hits.append(f"{e.rel}:{i}")
                m = _LIBRARY.search(line)
                if m and m.group(1) not in packages:
                    packages.append(m.group(1))
            if _RM_ALL.search(line):
                rm_hits.append(f"{e.rel}:{i}")
            if _INSTALL.search(line):
                install_hits.append(f"{e.rel}:{i}")
            if _DROPS_MISSING.search(line):
                drop_hits.append(f"{e.rel}:{i}: {stripped[:90]}")
            if _STAT_TEST.search(line):
                test_hits.append(f"{e.rel}:{i}")
            if _P_ADJUST.search(line):
                p_adjusted = True
            if _SESSION.search(line):
                records_session = True

    if abs_hits:
        out.append(_f(f"Hard-coded absolute paths ({len(abs_hits)})", "reproducibility", "minor",
                      "The code uses machine-specific absolute paths, so it will not run elsewhere: "
                      + "; ".join(abs_hits[:5]),
                      "Use paths relative to the project root (or a single configurable data directory).",
                      location=abs_hits[0].split(" ")[0], quote=abs_hits[0].split(" ", 1)[-1],
                      hours=(0.5, 2), confidence=0.9))
    if setwd_hits:
        out.append(_f("Scripts call setwd()", "reproducibility", "minor",
                      "setwd() ties the scripts to one computer's folder layout (" + ", ".join(setwd_hits[:4]) +
                      "); anyone else running them gets an error before the analysis starts.",
                      "Remove setwd() and use paths relative to the project (an RStudio project or here::here()).",
                      location=setwd_hits[0], hours=(0.25, 1), level="trivial", confidence=0.9))
    if rm_hits:
        out.append(_f("Workspace-clearing lines (rm(list = ls()) / clear all)", "reproducibility", "editorial",
                      "Clearing the workspace (" + ", ".join(rm_hits[:4]) + ") does not reset loaded packages or "
                      "options, so results can still depend on what was run before.",
                      "Run each script in a fresh session (Rscript / restart R) instead.",
                      location=rm_hits[0], hours=(0.1, 0.5), level="trivial", confidence=0.55))
    if install_hits:
        out.append(_f("Analysis scripts install packages", "reproducibility", "minor",
                      "Packages are installed from inside the analysis (" + ", ".join(install_hits[:4]) + "), so "
                      "whatever version is current that day gets used.",
                      "Move installation to a setup step and pin versions (renv::snapshot() / requirements.txt).",
                      location=install_hits[0], hours=(0.25, 1), confidence=0.65))
    if attach_hits:
        out.append(_f("attach() used", "code", "editorial",
                      "attach() (" + ", ".join(attach_hits[:4]) + ") makes it ambiguous which data a variable comes "
                      "from and is a classic source of silent errors.", "Refer to columns explicitly (d$x or with()).",
                      location=attach_hits[0], hours=(0.25, 1), level="trivial", confidence=0.55))
    if packages and not records_session:
        out.append(_f(f"R package versions not recorded ({len(packages)} package{'s' if len(packages) != 1 else ''})",
                      "reproducibility", "minor",
                      f"The code uses {', '.join(packages[:12])} but nothing records which versions "
                      f"(no sessionInfo(), renv.lock or DESCRIPTION).",
                      "Add renv (renv::init(); renv::snapshot()) or at least save sessionInfo() output with the results, "
                      "and cite R and key package versions in the Methods.",
                      hours=(0.25, 1), confidence=0.75))
    if drop_hits:
        out.append(_f(f"Missing values are dropped silently ({len(drop_hits)} place(s))", "data", "minor",
                      "The code removes missing observations without reporting how many: " + "; ".join(drop_hits[:4]) +
                      ". The n in each figure/test may therefore differ from the n stated in the manuscript.",
                      "Count and report exclusions per analysis (and say why the data are missing).",
                      location=drop_hits[0].split(":")[0] + ":" + drop_hits[0].split(":")[1],
                      hours=(0.5, 2), confidence=0.55))
    if len(test_hits) >= 3 and not p_adjusted:
        out.append(_f(f"{len(test_hits)} hypothesis tests in the code, no multiple-comparison correction",
                      "statistics", "minor",
                      "The code runs several tests (" + ", ".join(test_hits[:5]) + ") but never adjusts p-values.",
                      "Pre-specify the primary test, or correct the family of tests (p.adjust(..., 'holm')).",
                      location=test_hits[0], hours=(0.5, 2), confidence=0.5))
    if py_random and not py_seed:
        out.append(_f("Random operations without a fixed seed (Python)", "reproducibility", "minor",
                      "Stochastic operations are used (" + ", ".join(py_random[:4]) + ") but no random seed is set, "
                      "so results (splits, bootstraps, model fits) will differ between runs.",
                      "Set and report seeds (np.random.default_rng(seed), random_state=...) and report variability across seeds.",
                      location=py_random[0], hours=(0.5, 3), confidence=0.6))
    if r_random and not r_seed:
        out.append(_f("Random operations without set.seed() (R)", "reproducibility", "minor",
                      "Stochastic functions are used (" + ", ".join(r_random[:4]) + ") but set.seed() never appears.",
                      "Call set.seed() once at the top of each script and report it.",
                      location=r_random[0], hours=(0.25, 1), confidence=0.6))

    provided = {e.path.name.lower() for e in all_entries}
    missing = sorted((name, where) for name, where in referenced.items() if name not in provided)
    if missing and (data_entries or code_entries):
        out.append(_f(f"Code reads {len(missing)} data file(s) that were not provided", "data", "major",
                      "These files are read by the code but are not among the provided code/data: "
                      + "; ".join(f"{n} ({w})" for n, w in missing[:8])
                      + ". Without them the analysis cannot be reproduced.",
                      "Deposit the missing input files (or a script that generates them) with the code.",
                      location=missing[0][1], quote=missing[0][0], hours=(1, 4), confidence=0.6,
                      rationale="Some of these may be intermediate outputs generated by other scripts -- check before acting."))

    for e in code_entries:
        if e.path.suffix.lower() == ".ipynb":
            counts = [c for c in notebook_exec_counts(e.path)]
            executed = [c for c in counts if c is not None]
            if counts and (len(executed) < len(counts) or executed != sorted(executed)):
                out.append(_f(f"Notebook not run top-to-bottom: {e.rel}", "reproducibility", "minor",
                              "Cells were executed out of order or some were never run, so the saved outputs may "
                              "not correspond to the code as written.",
                              "Restart the kernel and run all cells in order before sharing.",
                              location=e.rel, hours=(0.25, 2), confidence=0.75))

    for s in data_summaries:
        mat_vars = [v for v in s.get("variables") or [] if (v.get("stats") or {}).get("n")]
        nan_vars = [f"{v['name']} ({100 * v['stats']['nan'] / v['stats']['n']:.0f}% NaN)" for v in mat_vars
                    if v["stats"].get("nan") and v["stats"]["nan"] / v["stats"]["n"] >= 0.01]
        if nan_vars:
            out.append(_f(f"Missing values (NaN) in {s['file']}", "data", "minor",
                          f"Variables contain NaN: {', '.join(nan_vars[:8])}. The manuscript should say how these "
                          f"were handled and whether they change n.",
                          "Report missing measurements per group and the handling method.",
                          location=s["file"], hours=(0.5, 2), confidence=0.6))
        pii_vars = [v["name"] for v in s.get("variables") or [] if _PII_RE.match(str(v.get("name", "")))]
        if pii_vars:
            out.append(_f(f"Possible identifiable personal data in {s['file']}", "ethics", "major",
                          f"Variables that look like personal identifiers: {', '.join(pii_vars[:8])}.",
                          "Remove or pseudonymise identifiers before sharing the data.", location=s["file"],
                          hours=(1, 4), confidence=0.55))
        cols = s.get("columns") or []
        pii = [c["name"] for c in cols if _PII_RE.match(str(c["name"]).strip())]
        if pii:
            out.append(_f(f"Possible identifiable personal data in {s['file']}", "ethics", "major",
                          f"Columns that look like personal identifiers are present: {', '.join(pii[:8])}.",
                          "Remove or pseudonymise direct identifiers before sharing, and confirm consent covers data sharing.",
                          location=s["file"], hours=(1, 4), confidence=0.55))
        high_missing = [f"{c['name']} ({c['missing_pct']}%)" for c in cols if c.get("missing_pct", 0) >= 20]
        if high_missing:
            out.append(_f(f"Substantial missing data in {s['file']}", "data", "minor",
                          f"Columns with >=20% missing values: {', '.join(high_missing[:8])}. The manuscript should "
                          f"state how missingness was handled and whether it could bias results.",
                          "Report missingness per variable and the handling method (complete-case, imputation, ...).",
                          location=s["file"], hours=(1, 4), level="small", confidence=0.6))
        if s.get("duplicate_rows"):
            out.append(_f(f"{s['duplicate_rows']} duplicate row(s) in {s['file']}", "data", "minor",
                          "Exact duplicate rows exist; if they are not intentional (e.g. repeated measures with "
                          "identical values) they will inflate n and distort statistics.",
                          "Check whether the duplicates are genuine and deduplicate or explain them.",
                          location=s["file"], hours=(0.5, 2), confidence=0.5))
        if s.get("ragged_rows"):
            out.append(_f(f"Malformed rows in {s['file']}", "data", "minor",
                          f"{s['ragged_rows']} row(s) have a different number of fields than the header.",
                          "Fix quoting/delimiters so every row parses cleanly.", location=s["file"],
                          hours=(0.25, 1), confidence=0.75))
    return out


# ---------------------------------------------------------------------------
# Dossier
# ---------------------------------------------------------------------------

def build_dossier(code_paths: list[str], data_paths: list[str], budget_chars: int = 120_000) -> Dossier:
    d = Dossier()
    code_entries, n1 = collect(code_paths, "code")
    data_entries, n2 = collect(data_paths, "data")
    # data files found inside the code folder count as data too
    data_entries += [e for e in code_entries if e.kind == "data"]
    code_entries = [e for e in code_entries if e.kind != "data"]
    d.files = code_entries + data_entries
    d.notes.extend(n1 + n2)
    if not d.files:
        return d

    d.data_summaries = [summarize_data_file(e) for e in data_entries
                        if e.path.suffix.lower() in (".csv", ".tsv", ".tab", ".dat", ".txt", ".xlsx", ".json", ".jsonl", ".ndjson", ".mat")][:60]
    for e in data_entries:
        if e.path.suffix.lower() not in (".csv", ".tsv", ".tab", ".dat", ".txt", ".xlsx", ".json", ".jsonl", ".ndjson", ".mat"):
            d.data_summaries.append({"file": e.rel, "size_bytes": e.size,
                                     "note": f"binary format ({e.path.suffix}); contents not inspected"})

    code_texts: dict[str, str] = {}
    for e in code_entries:
        if e.kind in ("code", "config", "doc") and e.size <= 5_000_000:
            try:
                code_texts[e.rel] = read_code(e)
            except OSError:
                pass
    d.findings = static_findings(code_entries, data_entries, code_texts, d.data_summaries)

    # Assemble within budget: tree, docs, data summaries, then code (configs and
    # smaller files first so many files are seen whole rather than one huge one).
    parts = ["FILES PROVIDED (path, size, kind):"]
    for e in d.files[:300]:
        parts.append(f"  {e.rel}  {_human(e.size)}  [{e.kind}]")
    if len(d.files) > 300:
        parts.append(f"  ... and {len(d.files) - 300} more files")
    header = "\n".join(parts)
    data_block = "\n\n".join(format_data_summary(s) for s in d.data_summaries)
    budget_left = budget_chars - len(header) - min(len(data_block), budget_chars // 4) - 200
    chunks = [header]
    if data_block:
        if len(data_block) > budget_chars // 4:
            data_block = data_block[:budget_chars // 4] + "\n... [data summaries truncated]"
            d.truncated = True
        chunks.append("DATA SUMMARIES:\n" + data_block)

    order = sorted((e for e in code_entries if e.rel in code_texts),
                   key=lambda e: ({"doc": 0, "config": 1, "code": 2}.get(e.kind, 3), e.size))
    per_file_cap = max(4000, budget_left // max(1, min(len(order), 12)))
    for e in order:
        if budget_left < 1500:
            d.truncated = True
            chunks.append(f"(code budget exhausted; {len(order) - order.index(e)} more file(s) not shown)")
            break
        body, trunc = _numbered(code_texts[e.rel], min(per_file_cap, budget_left, MAX_CODE_FILE_CHARS))
        d.truncated |= trunc
        block = f"===== FILE {e.rel} =====\n{body}"
        chunks.append(block)
        budget_left -= len(block)
    d.text = "\n\n".join(chunks)
    return d
