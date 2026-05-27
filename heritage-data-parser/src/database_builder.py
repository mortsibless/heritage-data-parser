"""
database_builder.py
-------------------
Handles all SQLite interactions: schema creation, language/book seeding,
and bulk verse insertion.

Keeps SQL entirely out of the extractor and cleanup engine.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# Relative to this file: ../../schemas/database_layout.sql
_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "database_layout.sql"


# ── Connection helper ─────────────────────────────────────────────────────────

def open_db(db_path: str | Path) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database, apply schema, return connection.
    Uses WAL mode and foreign keys for safety.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    # Apply schema (idempotent — uses CREATE IF NOT EXISTS throughout)
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    con.executescript(schema_sql)
    con.commit()

    logger.info("Database ready: %s", db_path)
    return con


# ── Language & book seeding ───────────────────────────────────────────────────

def upsert_language(con: sqlite3.Connection, config: dict) -> int:
    """
    Insert or update a language row from a config dict.
    Returns the language.id.
    """
    code = f"{config['language']}_{config['year']}"
    cur = con.execute(
        """
        INSERT INTO languages (code, name, year, source_file, testament)
        VALUES (:code, :name, :year, :source_file, 'both')
        ON CONFLICT(code) DO UPDATE SET
            year        = excluded.year,
            source_file = excluded.source_file
        """,
        {
            "code":        code,
            "name":        config["language"].replace("_", " ").title(),
            "year":        config["year"],
            "source_file": config.get("source_file", ""),
        },
    )
    con.commit()

    row = con.execute("SELECT id FROM languages WHERE code = ?", (code,)).fetchone()
    lang_id = row["id"]
    logger.info("Language '%s' → id=%d", code, lang_id)
    return lang_id


def seed_books(con: sqlite3.Connection, lang_id: int, config: dict) -> dict[str, int]:
    """
    Insert all books from config book_names (OT + NT).
    Returns {local_name: book_id} mapping.
    """
    book_map: dict[str, int] = {}
    order = 1

    for testament_key, testament_label in [("old_testament", "OT"), ("new_testament", "NT")]:
        for local_name, meta in config["book_names"].get(testament_key, {}).items():
            con.execute(
                """
                INSERT INTO books
                    (language_id, local_name, standard_name, testament, book_order, total_chapters)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(language_id, local_name) DO UPDATE SET
                    standard_name  = excluded.standard_name,
                    testament      = excluded.testament,
                    book_order     = excluded.book_order,
                    total_chapters = excluded.total_chapters
                """,
                (lang_id, local_name, meta["standard"], testament_label, order, meta.get("chapters")),
            )
            row = con.execute(
                "SELECT id FROM books WHERE language_id=? AND local_name=?",
                (lang_id, local_name),
            ).fetchone()
            book_map[local_name] = row["id"]
            order += 1

    con.commit()
    logger.info("Seeded %d books for language_id=%d", len(book_map), lang_id)
    return book_map


def get_or_create_chapter(con: sqlite3.Connection, book_id: int, chapter_num: int) -> int:
    """Return chapter.id, creating the row if it doesn't exist."""
    con.execute(
        "INSERT OR IGNORE INTO chapters (book_id, number) VALUES (?, ?)",
        (book_id, chapter_num),
    )
    row = con.execute(
        "SELECT id FROM chapters WHERE book_id=? AND number=?",
        (book_id, chapter_num),
    ).fetchone()
    return row["id"]


# ── Bulk insertion ────────────────────────────────────────────────────────────

def insert_page(
    con: sqlite3.Connection,
    lang_id: int,
    pdf_page: int,
    printed_page: int | None,
    raw_text: str,
    left_col: str,
    right_col: str,
    book_header: str,
    chapter_range: str,
) -> None:
    con.execute(
        """
        INSERT INTO pages
            (language_id, pdf_page, printed_page, raw_text,
             left_column, right_column, book_header, chapter_range)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(language_id, pdf_page) DO UPDATE SET
            raw_text      = excluded.raw_text,
            left_column   = excluded.left_column,
            right_column  = excluded.right_column,
            book_header   = excluded.book_header,
            chapter_range = excluded.chapter_range
        """,
        (lang_id, pdf_page, printed_page, raw_text, left_col, right_col, book_header, chapter_range),
    )


def insert_verse(
    con: sqlite3.Connection,
    chapter_id: int,
    verse_number: int,
    raw_text: str,
    clean_text: str,
    pdf_page: int,
    has_footnote: bool,
    flags: list[str],
) -> int:
    """Insert a verse row. Returns the new verse id."""
    flags_json = json.dumps(flags) if flags else None
    cur = con.execute(
        """
        INSERT INTO verses
            (chapter_id, verse_number, raw_text, clean_text,
             pdf_page, has_footnote, extraction_flags)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(chapter_id, verse_number) DO UPDATE SET
            raw_text         = excluded.raw_text,
            clean_text       = excluded.clean_text,
            pdf_page         = excluded.pdf_page,
            has_footnote     = excluded.has_footnote,
            extraction_flags = excluded.extraction_flags
        """,
        (chapter_id, verse_number, raw_text, clean_text,
         pdf_page, int(has_footnote), flags_json),
    )
    return cur.lastrowid


def insert_footnotes(con: sqlite3.Connection, verse_id: int, footnotes: list[dict]) -> None:
    for fn in footnotes:
        con.execute(
            "INSERT INTO footnotes (verse_id, marker, reference, raw_text) VALUES (?,?,?,?)",
            (verse_id, fn.get("marker", ""), fn.get("reference", ""), fn.get("raw", "")),
        )


def update_chapter_verse_count(con: sqlite3.Connection, chapter_id: int) -> None:
    con.execute(
        """
        UPDATE chapters SET verse_count = (
            SELECT COUNT(*) FROM verses WHERE chapter_id = ?
        ) WHERE id = ?
        """,
        (chapter_id, chapter_id),
    )


def rebuild_fts(con: sqlite3.Connection) -> None:
    """Rebuild the FTS5 index after bulk inserts."""
    logger.info("Rebuilding FTS index …")
    con.execute("INSERT INTO verses_fts(verses_fts) VALUES('rebuild')")
    con.commit()
    logger.info("FTS index ready.")


# ── Logging ───────────────────────────────────────────────────────────────────

def log_run(
    con: sqlite3.Connection,
    lang_id: int,
    pages: int,
    verses: int,
    warnings: int,
    errors: int,
    duration: float,
    notes: str = "",
) -> None:
    con.execute(
        """
        INSERT INTO extraction_log
            (language_id, pdf_pages_processed, verses_inserted,
             warnings, errors, duration_seconds, notes)
        VALUES (?,?,?,?,?,?,?)
        """,
        (lang_id, pages, verses, warnings, errors, round(duration, 2), notes),
    )
    con.commit()


# ── Query helpers (used by CLI / notebooks) ───────────────────────────────────

def fetch_verse(con: sqlite3.Connection, lang_code: str, book: str, chapter: int, verse: int):
    """Look up a single verse by standard book name."""
    return con.execute(
        """
        SELECT * FROM v_verses
        WHERE language_code = ?
          AND (book_standard = ? OR book_local = ?)
          AND chapter = ?
          AND verse   = ?
        """,
        (lang_code, book, book, chapter, verse),
    ).fetchone()


def search_text(con: sqlite3.Connection, query: str, lang_code: str | None = None):
    """Full-text search across clean_text."""
    if lang_code:
        return con.execute(
            """
            SELECT v.* FROM verses_fts f
            JOIN v_verses v ON v.verse_id = f.rowid
            WHERE verses_fts MATCH ?
              AND v.language_code = ?
            ORDER BY rank
            LIMIT 50
            """,
            (query, lang_code),
        ).fetchall()
    return con.execute(
        """
        SELECT v.* FROM verses_fts f
        JOIN v_verses v ON v.verse_id = f.rowid
        WHERE verses_fts MATCH ?
        ORDER BY rank
        LIMIT 50
        """,
        (query,),
    ).fetchall()
