"""Dayo Vault — storage layer (SQLite + FTS5).

Replaces the flat JSON inventory with a proper SQLite database:
  • O(1) record lookup by id
  • FTS5 full-text search across filenames, tags, notes, AND entire extracted text
  • WAL mode for safe concurrent reads during uploads/transcription
  • Thread-safe writes via a Python RLock

Public API is identical to the previous JSON-backed version — the server,
dayo_sources.py, and vault_server.py import this unchanged.

Migration: on first run, any existing source_inventory.json is read and all records
are imported into the new DB. The JSON is kept as a timestamped backup.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
import os
# All data lives under VAULT_HOME (default: ./data next to this file).
HOME      = Path(os.environ.get("VAULT_HOME", Path(__file__).resolve().parent / "data"))
VAULT     = HOME / "vault"
FILES_DIR = VAULT / "files"
TEXT_DIR  = VAULT / "text"
TRASH_DIR = VAULT / ".trash"
INVENTORY = HOME / "state" / "source_inventory.json"  # kept as backup after migration
DB_PATH   = HOME / "state" / "vault.db"

for _d in (FILES_DIR, TEXT_DIR, TRASH_DIR, DB_PATH.parent):
    _d.mkdir(parents=True, exist_ok=True)

_WRITE_LOCK = threading.RLock()


# ── DB init ───────────────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id                   TEXT PRIMARY KEY,
    original_name        TEXT NOT NULL DEFAULT '',
    stored_path          TEXT NOT NULL DEFAULT '',
    text_path            TEXT NOT NULL DEFAULT '',
    size                 INTEGER DEFAULT 0,
    mime                 TEXT DEFAULT '',
    word_count           INTEGER DEFAULT 0,
    extractor            TEXT DEFAULT '',
    course               TEXT DEFAULT '',
    tags                 TEXT DEFAULT '[]',
    note                 TEXT DEFAULT '',
    preview              TEXT DEFAULT '',
    transcription_status TEXT DEFAULT '',
    uploaded_at          REAL DEFAULT 0,
    deleted              INTEGER DEFAULT 0,
    deleted_at           REAL
);

-- FTS5 table indexes everything Dayo might search for, including full extracted text.
-- Content is denormalized here for speed; updated on every add/patch/delete.
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    id   UNINDEXED,
    body,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE INDEX IF NOT EXISTS idx_sources_course   ON sources(course);
CREATE INDEX IF NOT EXISTS idx_sources_uploaded ON sources(uploaded_at DESC);
"""


def _read_text_head(text_path: str, n: int = 0) -> str:
    try:
        p = Path(text_path)
        if p.exists():
            t = p.read_text(errors="replace")
            return t if n == 0 else t[:n]
    except Exception:
        pass
    return ""


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except Exception:
        d["tags"] = []
    d["deleted"] = bool(d.get("deleted", 0))
    return d


def _fts_body(r: dict, full_text: str = "") -> str:
    tags_list = r.get("tags") if isinstance(r.get("tags"), list) else []
    try:
        if not tags_list and isinstance(r.get("tags"), str):
            tags_list = json.loads(r.get("tags") or "[]")
    except Exception:
        tags_list = []
    return " ".join(filter(None, [
        r.get("original_name", ""),
        r.get("course", ""),
        " ".join(tags_list),
        r.get("note", ""),
        r.get("preview", ""),
        full_text,
    ]))


def _insert_row(c: sqlite3.Connection, r: dict):
    tags = r.get("tags", [])
    tags_json = json.dumps(tags if isinstance(tags, list) else [])
    c.execute("""
        INSERT OR IGNORE INTO sources
        (id, original_name, stored_path, text_path, size, mime, word_count,
         extractor, course, tags, note, preview, transcription_status,
         uploaded_at, deleted, deleted_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        r.get("id", ""),
        r.get("original_name", ""),
        r.get("stored_path", ""),
        r.get("text_path", ""),
        r.get("size", 0),
        r.get("mime", ""),
        r.get("word_count", 0),
        r.get("extractor", ""),
        r.get("course", ""),
        tags_json,
        r.get("note", ""),
        r.get("preview", ""),
        r.get("transcription_status", ""),
        r.get("uploaded_at", time.time()),
        1 if r.get("deleted") else 0,
        r.get("deleted_at"),
    ))
    full_text = _read_text_head(r.get("text_path", ""))
    body = _fts_body(r, full_text)
    c.execute("DELETE FROM fts WHERE id=?", (r.get("id", ""),))
    c.execute("INSERT INTO fts(id, body) VALUES (?,?)",
              (r.get("id", ""), body))


def _init_db():
    with _WRITE_LOCK:
        with _conn() as c:
            c.executescript(_SCHEMA)
    _migrate_from_json()


def _migrate_from_json():
    """One-time import of the legacy JSON inventory into SQLite."""
    if not INVENTORY.exists():
        return
    with _conn() as c:
        count = c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    if count > 0:
        return  # already migrated
    try:
        records = json.loads(INVENTORY.read_text())
        if not isinstance(records, list):
            return
        imported = 0
        with _WRITE_LOCK:
            with _conn() as c:
                for r in records:
                    _insert_row(c, r)
                    imported += 1
        # rename JSON so it's kept but not re-imported
        backup = INVENTORY.with_suffix(f".json.bak.{int(time.time())}")
        INVENTORY.rename(backup)
        print(f"[vault_store] Migrated {imported} records from JSON → SQLite. "
              f"Backup: {backup.name}")
    except Exception as e:
        print(f"[vault_store] JSON migration failed (non-fatal): {e}")


_init_db()


# ── Public API ────────────────────────────────────────────────────────────────
def load_inventory() -> list[dict]:
    """Return all non-deleted records, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM sources WHERE deleted=0 ORDER BY uploaded_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_record(file_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM sources WHERE id=?", (file_id,)).fetchone()
    return _row_to_dict(row) if row else None


def add_record(*, file_id: str, original_name: str, stored_path: str,
               text_path: str, size: int, mime: str, word_count: int,
               extractor: str, course: str = "", tags: Optional[list] = None,
               note: str = "", preview: str = "",
               transcription_status: str = "") -> dict:
    tags_json = json.dumps(tags or [])
    now = time.time()
    full_text = _read_text_head(text_path)  # index full text at insert time
    fts_body_str = " ".join(filter(None, [
        original_name, course, " ".join(tags or []), note, preview, full_text
    ]))
    with _WRITE_LOCK:
        with _conn() as c:
            c.execute("""
                INSERT INTO sources
                (id, original_name, stored_path, text_path, size, mime, word_count,
                 extractor, course, tags, note, preview, transcription_status,
                 uploaded_at, deleted)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
            """, (file_id, original_name, stored_path, text_path, size, mime,
                  word_count, extractor, course, tags_json, note, preview,
                  transcription_status, now))
            c.execute("INSERT OR REPLACE INTO fts(id, body) VALUES (?,?)",
                      (file_id, fts_body_str))
    return get_record(file_id)


def update_record(file_id: str, **fields) -> Optional[dict]:
    """User-facing metadata updates (course, tags, note, original_name)."""
    allowed = {"course", "tags", "note", "original_name"}
    return _patch(file_id, {k: v for k, v in fields.items() if k in allowed})


def set_fields(file_id: str, **fields) -> Optional[dict]:
    """Internal updates (transcription worker, extractor). Allows system fields."""
    allowed = {"text_path", "word_count", "extractor", "preview",
               "transcription_status", "course", "tags", "note"}
    return _patch(file_id, {k: v for k, v in fields.items() if k in allowed})


def _patch(file_id: str, fields: dict) -> Optional[dict]:
    if not fields:
        return get_record(file_id)
    with _WRITE_LOCK:
        with _conn() as c:
            for key, val in fields.items():
                if key == "tags" and isinstance(val, list):
                    val = json.dumps(val)
                c.execute(f"UPDATE sources SET {key}=? WHERE id=?", (val, file_id))
            # Rebuild FTS body after any update
            row = c.execute("SELECT * FROM sources WHERE id=?", (file_id,)).fetchone()
            if row:
                r = _row_to_dict(row)
                full_text = _read_text_head(r.get("text_path", ""))
                body = _fts_body(r, full_text)
                c.execute("INSERT OR REPLACE INTO fts(id, body) VALUES (?,?)",
                          (file_id, body))
    return get_record(file_id)


def soft_delete(file_id: str) -> bool:
    """Move bytes + text to vault/.trash and mark record deleted. Non-destructive."""
    with _WRITE_LOCK:
        r = get_record(file_id)
        if not r or r.get("deleted"):
            return False
        for key in ("stored_path", "text_path"):
            p = Path(r.get(key, ""))
            if p.exists():
                dest = TRASH_DIR / f"{file_id}__{p.name}"
                try:
                    shutil.move(str(p), str(dest))
                except Exception:
                    pass
        now = time.time()
        with _conn() as c:
            c.execute("UPDATE sources SET deleted=1, deleted_at=? WHERE id=?",
                      (now, file_id))
            c.execute("DELETE FROM fts WHERE id=?", (file_id,))
    return True


def search(query: str, course: str = "", limit: int = 50) -> list[dict]:
    """Full-text search across filenames, tags, notes, and complete extracted text.
    Falls back to LIKE scan if the FTS query is malformed."""
    q = (query or "").strip()
    course_filter = course.strip().lower()

    with _conn() as c:
        try:
            if q:
                # FTS5 match — searches the ENTIRE text of every document.
                # DISTINCT prevents duplicate rows when FTS has multiple entries per id.
                sql = """
                    SELECT DISTINCT s.* FROM sources s
                    JOIN fts ON fts.id = s.id
                    WHERE fts MATCH ? AND s.deleted = 0
                    {}
                    ORDER BY rank
                    LIMIT ?
                """.format("AND lower(s.course)=?" if course_filter else "")
                params = (q, course_filter, limit) if course_filter else (q, limit)
                rows = c.execute(sql, params).fetchall()
            else:
                sql = """
                    SELECT * FROM sources WHERE deleted=0
                    {}
                    ORDER BY uploaded_at DESC LIMIT ?
                """.format("AND lower(course)=?" if course_filter else "")
                params = (course_filter, limit) if course_filter else (limit,)
                rows = c.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Malformed FTS query — fall back to LIKE
            rows = _fallback_search(c, q, course_filter, limit)
    return [_row_to_dict(r) for r in rows]


def _fallback_search(c, q: str, course_filter: str, limit: int):
    like = f"%{q}%"
    sql = """
        SELECT * FROM sources WHERE deleted=0
        AND (lower(original_name) LIKE lower(?) OR lower(note) LIKE lower(?))
        {}
        ORDER BY uploaded_at DESC LIMIT ?
    """.format("AND lower(course)=?" if course_filter else "")
    params = (like, like, course_filter, limit) if course_filter else (like, like, limit)
    return c.execute(sql, params).fetchall()


def read_full_text(file_id: str) -> str:
    r = get_record(file_id)
    if not r:
        return ""
    try:
        return Path(r["text_path"]).read_text(errors="replace")
    except Exception:
        return ""


def stats() -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM sources WHERE deleted=0").fetchone()[0]
        words = c.execute("SELECT COALESCE(SUM(word_count),0) FROM sources WHERE deleted=0").fetchone()[0]
        size  = c.execute("SELECT COALESCE(SUM(size),0) FROM sources WHERE deleted=0").fetchone()[0]
        rows  = c.execute(
            "SELECT COALESCE(course,'Uncategorized') as c, COUNT(*) as n "
            "FROM sources WHERE deleted=0 GROUP BY c ORDER BY n DESC"
        ).fetchall()
    by_course = {r["c"] or "Uncategorized": r["n"] for r in rows}
    readable  = sum(1 for r in load_inventory() if r.get("word_count", 0) > 0)
    return {
        "total_files": total,
        "total_words": words,
        "total_size": size,
        "by_course": by_course,
        "dayo_readable": readable,
    }


# ── OCR ───────────────────────────────────────────────────────────────────────
def _ocr_pdf(p: Path, dpi: int = 200) -> str:
    """Rasterize each PDF page and run Tesseract OCR. Slow but handles scanned docs."""
    try:
        from pdf2image import convert_from_path
        import pytesseract
        pages = convert_from_path(str(p), dpi=dpi)
        parts = []
        for img in pages:
            try:
                parts.append(pytesseract.image_to_string(img, lang="eng"))
            except Exception:
                continue
        return "\n".join(parts).strip()
    except Exception:
        return ""


def ocr_file(file_id: str, dpi: int = 200) -> dict:
    """Re-OCR a vault file that has no extracted text. Updates the record in place."""
    r = get_record(file_id)
    if not r:
        return {"error": "not found"}
    p = Path(r.get("stored_path", ""))
    if not p.exists():
        return {"error": "file bytes missing"}

    print(f"  OCR: {r['original_name']} ({p.stat().st_size // 1024}KB)…")
    text = _ocr_pdf(p, dpi=dpi)
    if not text:
        return {"error": "OCR produced no text", "id": file_id}

    text_path = Path(r.get("text_path", ""))
    if not text_path.parent.exists():
        text_path = TEXT_DIR / f"{file_id}.txt"
    text_path.write_text(text)

    word_count = len(text.split())
    preview = make_preview(text)
    set_fields(file_id, text_path=str(text_path), word_count=word_count,
               extractor="tesseract-ocr", preview=preview)
    reindex_fts(file_id)
    return {"id": file_id, "word_count": word_count, "extractor": "tesseract-ocr"}


# ── Text extraction ───────────────────────────────────────────────────────────
def extract_text(path: str, mime: str, original_name: str = "") -> tuple[str, str]:
    """Extract plain text from an uploaded file. Returns (text, extractor_used)."""
    p = Path(path)
    suffix = p.suffix.lower()

    if suffix in (".txt", ".md", ".markdown", ".csv", ".json", ".py", ".js",
                  ".html", ".css", ".rtf", ".tex", ".log") or mime.startswith("text/"):
        try:
            return p.read_text(errors="replace"), "text"
        except Exception:
            return "", "none"

    if suffix == ".pdf" or mime == "application/pdf":
        # Try PyPDF2 first (fast, works on text-layer PDFs)
        text = ""
        try:
            import PyPDF2
            parts = []
            with open(p, "rb") as fh:
                reader = PyPDF2.PdfReader(fh)
                for page in reader.pages:
                    try:
                        parts.append(page.extract_text() or "")
                    except Exception:
                        continue
            text = "\n".join(parts).strip()
        except Exception:
            pass

        if text and len(text.split()) > 50:
            return text, "pypdf2"

        # Text layer empty or sparse — scanned PDF. Fall back to Tesseract OCR.
        return _ocr_pdf(p), "tesseract-ocr"

    if suffix == ".docx" or "wordprocessingml" in mime:
        try:
            import docx
            d = docx.Document(str(p))
            return "\n".join(par.text for par in d.paragraphs).strip(), "python-docx"
        except Exception:
            return "", "none"

    return "", "none"


def make_preview(text: str, n: int = 240) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:n]


# ── Course keyword mapping and autotagging ────────────────────────────────────
KEYWORD_CONFIG = DB_PATH.parent / "dayo_vault_course_keywords.json"

_DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "EAS 2030": [
        # Perrow / accidents / safety
        "normal accidents", "perrow", "charles perrow",
        "three mile island", "tmi", "china syndrome",
        "normalization of deviance", "challenger", "vaughan", "diane vaughan",
        "cultures of safety",
        # Engineering ethics authors / topics
        "weapons of math destruction", "cathy o'neil",
        "buolamwini", "joy buolamwini", "algorithmic bias",
        "doudna", "crispr", "gene editing",
        "equifax", "apache struts",
        "fleddermann",
        # Autonomous systems
        "urmson", "cummings", "driverless", "autonomous vehicle",
        # Research ethics
        "researcher's duty", "informed consent", "irb",
        # Big data / surveillance
        "big data",
    ],
    "STSC 1120": [
        # Philosophy / ethics — what this course actually covers
        "rawls", "john rawls", "a theory of justice", "veil of ignorance",
        "kant", "immanuel kant", "categorical imperative", "deontolog",
        "bentham", "utilitarianism", "greatest happiness",
        "aristotle", "nicomachean ethics", "virtue ethics",
        "du bois", "w.e.b. du bois", "double consciousness",
        "lindemann",
        # History of science & technology
        "dupree", "postwar science", "research university",
        "vannevar bush", "science the endless frontier",
        "eisenhower", "military-industrial complex",
        "world war", "big science",
        # Proactive ethical design
        "proactive ethical design",
        # Tech & society podcast/lectures
        "technology in history",
    ],
}


def _load_keyword_map() -> dict:
    try:
        if KEYWORD_CONFIG.exists():
            return json.loads(KEYWORD_CONFIG.read_text())
    except Exception:
        pass
    return _DEFAULT_KEYWORDS


def auto_categorize(file_id: str) -> Optional[str]:
    """Conservative auto-tagging: if exactly one course matches, set it."""
    rec = get_record(file_id)
    if not rec:
        return None
    if rec.get("course"):
        return rec.get("course")

    keyword_map = _load_keyword_map()
    hay = " ".join([
        rec.get("original_name", ""),
        rec.get("preview", ""),
        _read_text_head(rec.get("text_path", ""), 6000),
    ]).lower()

    matches = []
    for course, kws in keyword_map.items():
        for k in kws:
            if k.lower() in hay:
                matches.append(course)
                break

    matches = list(dict.fromkeys(matches))
    if len(matches) == 1:
        course = matches[0]
        note = (rec.get("note") or "").strip()
        audit = f"auto_tag:{course}@{int(time.time())}"
        note = (note + "\n" + audit).strip()
        set_fields(file_id, course=course, note=note)
        return course
    return None


# ── FTS re-index (run after bulk text extraction / transcription updates) ─────
def reindex_fts(file_id: str) -> bool:
    """Rebuild the FTS body for a single record (call after transcription completes)."""
    r = get_record(file_id)
    if not r:
        return False
    full_text = _read_text_head(r.get("text_path", ""))
    body = _fts_body(r, full_text)
    with _WRITE_LOCK:
        with _conn() as c:
            c.execute("INSERT OR REPLACE INTO fts(id, body) VALUES (?,?)",
                      (file_id, body))
    return True


def reindex_all() -> int:
    """Rebuild FTS for every non-deleted record. Run once after upgrading or migration."""
    recs = load_inventory()
    n = 0
    for r in recs:
        if reindex_fts(r["id"]):
            n += 1
    return n
