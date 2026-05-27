-- =============================================================================
-- Heritage Bible Database Schema
-- heritage-data-parser/schemas/database_layout.sql
--
-- Normalized schema supporting multiple Bible translations/languages.
-- Designed around the two-column, inline-verse structure observed in the
-- Ewe 1913 and Akuapem 1871 PDFs.
-- =============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- -----------------------------------------------------------------------------
-- LANGUAGES
-- One row per Bible source file.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS languages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL UNIQUE,   -- e.g. 'ewe_1913', 'akuapem_1871'
    name          TEXT    NOT NULL,          -- e.g. 'Ewe'
    year          INTEGER,                   -- publication year
    source_file   TEXT,                      -- original PDF filename
    testament     TEXT    CHECK(testament IN ('both','OT','NT')),
    notes         TEXT,
    created_at    TEXT    DEFAULT (datetime('now'))
);


-- -----------------------------------------------------------------------------
-- BOOKS
-- Canonical book list shared across translations; maps Ewe/Twi names → standard.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS books (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    language_id     INTEGER NOT NULL REFERENCES languages(id) ON DELETE CASCADE,
    local_name      TEXT    NOT NULL,   -- name as it appears in the PDF header
    standard_name   TEXT    NOT NULL,   -- English canonical name
    testament       TEXT    NOT NULL CHECK(testament IN ('OT','NT')),
    book_order      INTEGER NOT NULL,   -- canonical ordering (1=Genesis … 66=Revelation)
    total_chapters  INTEGER,
    UNIQUE(language_id, local_name)
);

CREATE INDEX IF NOT EXISTS idx_books_language ON books(language_id);
CREATE INDEX IF NOT EXISTS idx_books_standard ON books(standard_name);


-- -----------------------------------------------------------------------------
-- CHAPTERS
-- Lightweight table; lets us track extraction completeness per chapter.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chapters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    verse_count INTEGER,                -- filled after all verses are inserted
    UNIQUE(book_id, number)
);

CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id);


-- -----------------------------------------------------------------------------
-- VERSES
-- Core content table. raw_text = straight from pdfplumber; clean_text = after
-- cleanup_engine normalization; both are stored so you can re-run cleanup
-- without re-extracting.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS verses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id    INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    verse_number  INTEGER NOT NULL,
    raw_text      TEXT    NOT NULL,   -- text exactly as extracted from PDF
    clean_text    TEXT,               -- text after cleanup_engine normalization
    pdf_page      INTEGER,            -- PDF page number (continuous, 1-based)
    has_footnote  INTEGER DEFAULT 0 CHECK(has_footnote IN (0,1)),
    extraction_flags TEXT,            -- JSON array of any parser warnings
    UNIQUE(chapter_id, verse_number)
);

CREATE INDEX IF NOT EXISTS idx_verses_chapter   ON verses(chapter_id);
CREATE INDEX IF NOT EXISTS idx_verses_pdf_page  ON verses(pdf_page);
-- Full-text search on clean Ewe/Twi text
CREATE VIRTUAL TABLE IF NOT EXISTS verses_fts USING fts5(
    clean_text,
    content='verses',
    content_rowid='id'
);


-- -----------------------------------------------------------------------------
-- FOOTNOTES
-- Inline footnote markers (a, b, c…) extracted separately with their
-- cross-reference target text.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS footnotes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    verse_id    INTEGER NOT NULL REFERENCES verses(id) ON DELETE CASCADE,
    marker      TEXT    NOT NULL,   -- e.g. 'a', 'b', 'ff'
    reference   TEXT,               -- e.g. 'Ps. 136, 7-9'
    raw_text    TEXT                -- full footnote line as seen in PDF
);

CREATE INDEX IF NOT EXISTS idx_footnotes_verse ON footnotes(verse_id);


-- -----------------------------------------------------------------------------
-- PAGES
-- Raw per-page text dump — useful for debugging extraction and re-parsing
-- without touching the PDF again.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    language_id   INTEGER NOT NULL REFERENCES languages(id) ON DELETE CASCADE,
    pdf_page      INTEGER NOT NULL,
    printed_page  INTEGER,           -- the page number printed in the PDF header
    raw_text      TEXT,              -- full pdfplumber layout text for this page
    left_column   TEXT,              -- left column text (post split)
    right_column  TEXT,              -- right column text (post split)
    book_header   TEXT,              -- book name from page header
    chapter_range TEXT,              -- e.g. '6, 21-8, 6' from header bracket
    UNIQUE(language_id, pdf_page)
);

CREATE INDEX IF NOT EXISTS idx_pages_language ON pages(language_id);


-- -----------------------------------------------------------------------------
-- EXTRACTION_LOG
-- One row per extraction run — tracks progress and any errors.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extraction_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    language_id   INTEGER REFERENCES languages(id),
    run_at        TEXT    DEFAULT (datetime('now')),
    pdf_pages_processed INTEGER,
    verses_inserted     INTEGER,
    warnings            INTEGER,
    errors              INTEGER,
    duration_seconds    REAL,
    notes               TEXT
);


-- -----------------------------------------------------------------------------
-- CONVENIENCE VIEWS
-- -----------------------------------------------------------------------------

-- Full verse reference with all context joined
CREATE VIEW IF NOT EXISTS v_verses AS
SELECT
    v.id                        AS verse_id,
    l.code                      AS language_code,
    l.name                      AS language_name,
    b.testament,
    b.book_order,
    b.standard_name             AS book_standard,
    b.local_name                AS book_local,
    c.number                    AS chapter,
    v.verse_number              AS verse,
    v.clean_text,
    v.raw_text,
    v.pdf_page,
    v.has_footnote,
    v.extraction_flags
FROM verses v
JOIN chapters  c ON c.id = v.chapter_id
JOIN books     b ON b.id = c.book_id
JOIN languages l ON l.id = b.language_id;

-- Quick stats per book
CREATE VIEW IF NOT EXISTS v_book_stats AS
SELECT
    l.code          AS language_code,
    b.standard_name AS book,
    b.testament,
    b.book_order,
    COUNT(DISTINCT c.id)  AS chapter_count,
    COUNT(v.id)           AS verse_count,
    SUM(v.has_footnote)   AS footnoted_verses
FROM books b
JOIN languages l ON l.id = b.language_id
LEFT JOIN chapters c ON c.book_id = b.id
LEFT JOIN verses   v ON v.chapter_id = c.id
GROUP BY l.code, b.id
ORDER BY l.code, b.book_order;
