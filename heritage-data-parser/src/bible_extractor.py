"""
bible_extractor.py
------------------
Main extraction pipeline for heritage Bible PDFs.

Observations from direct inspection of Ewe 1913 PDF:
  - pdfplumber merges both columns into ONE stream per line (~62 chars wide)
  - Page header:  '         10                 MOSE I.           [6, 21-8, 6     '
    - 9 spaces indent, then printed page num, then book name, then [range]
  - Verse numbers inline within prose: '         1 Le gomedzedzea...'
    - Left-column verse numbers start at ~indent 9-11
    - Right-column verse numbers appear mid-line after the left column text
  - Cross-reference noise is embedded mid-line (not separate lines)
  - Chapter title pages (e.g. page 5) have no [bracket] — just the book name centred
  - Hyphenated line breaks: 'gbo-' continued on same line as right col text

Usage:
    python src/bible_extractor.py --pdf data/raw/ewe_1913.pdf --config config/ewe_1913.json
    python src/bible_extractor.py --pdf data/raw/ewe_1913.pdf --config config/ewe_1913.json --pages 5-52
    python src/bible_extractor.py --pdf data/raw/ewe_1913.pdf --config config/ewe_1913.json --probe

Dependencies:
    pip install pdfplumber tqdm
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("ERROR: pdfplumber not installed.  Run: pip install pdfplumber")

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

sys.path.insert(0, str(Path(__file__).parent))
from cleanup_engine import CleanupEngine
from database_builder import (
    open_db, upsert_language, seed_books,
    get_or_create_chapter, insert_page, insert_verse,
    insert_footnotes, update_chapter_verse_count,
    rebuild_fts, log_run,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Regexes built from actual PDF line samples ────────────────────────────────

# '         10                 MOSE I.           [6, 21-8, 6     '
# '         50                 MOSE I.         [47,17-48, 10     '
# '         20                  MOSE I.        [19, 22 — 20, 13  '
_HEADER_RE = re.compile(
    r"^\s+"                          # leading indent (9 spaces)
    r"(\d+)"                         # printed page number
    r"\s{2,}"                        # gap
    r"([A-ZÄÖÜ][A-ZÄÖÜ\s\.]+?)"     # book name (e.g. MOSE I.)
    r"\s{2,}"                        # gap
    r"\[([\d,\s\.\-—]+)\]"          # [chapter range]  — note — dash variant
    r"\s*$"
)

# Book-title-only line (chapter title pages, e.g. page 5):
# '                 MOSE   FE AGBALE    GBATO.                   '
_BOOK_TITLE_RE = re.compile(
    r"^\s{10,}"                      # heavy indent (centred)
    r"([A-ZÄÖÜ][A-ZÄÖÜ\s\.]{3,})"  # all-caps name
    r"\s*$"
)

# Inline verse number:
#   left-col:  '         1 Le gomedzedzea...'   → verse num at col ~10
#   right-col: '...woa- fomeviwo nu. 15 Eye...' → num mid-line after hyphen gap
# Strategy: find digits surrounded by spaces with a capital/Ewe char following
_VERSE_INLINE_RE = re.compile(
    r"(?<!\d)"           # not preceded by digit
    r"\b(\d{1,3})\b"     # 1-3 digit number
    r"(?!\s*[\.,;:\-])"  # not followed by punctuation (avoids cross-refs)
    r"\s+"
    r"(?=[A-ZÄÖÜ\wƒʋɖɔŋɛ])"  # followed by word char (capital preferred)
)

# Cross-reference noise patterns embedded mid-line
# e.g. 'a Do. 17, 24;'  'b Ps. 136, 7-9.'  'H N e y b a . d . 1 4 1'
_CROSSREF_RE = re.compile(
    r"\b[a-z]{1,2}\s+[A-Z][a-zA-Z]+\.?\s*[\d,\s;\.\-]+"
)
_SPACED_OCR_RE = re.compile(
    r"(?:[A-Za-z]\s){3,}[A-Za-z]"   # 'H N e y b a' style OCR noise
)

# Footnote standalone markers in cleaned text
_FOOTNOTE_MARKER_RE = re.compile(r"\b([a-z]{1,2})\s+[A-Z][a-z]+\.\s*\d")


# ── Page parser ───────────────────────────────────────────────────────────────

def parse_header(lines: list[str]) -> tuple[int | None, str, str]:
    """Return (printed_page, book_name, chapter_range) from first matching header line."""
    for line in lines[:8]:
        m = _HEADER_RE.match(line)
        if m:
            return int(m.group(1)), m.group(2).strip().rstrip("."), m.group(3).strip()
    return None, "", ""


def detect_book_from_title(lines: list[str], known_books: list[str]) -> str:
    """
    For chapter-title pages that have no [bracket] header,
    detect book name from the centred title line.
    """
    for line in lines[:10]:
        stripped = line.strip()
        # Match against known book names
        for book in known_books:
            # Allow for extra spaces that pdfplumber inserts in layout mode
            book_squished = re.sub(r"\s+", r"\\s+", book)
            if re.search(book_squished, stripped, re.IGNORECASE):
                return book
        # Fallback: all-caps centred line
        m = _BOOK_TITLE_RE.match(line)
        if m:
            candidate = re.sub(r"\s+", " ", m.group(1)).strip()
            return candidate
    return ""


def clean_line(line: str) -> str:
    """Strip cross-ref noise and OCR artefacts from a single line."""
    line = _SPACED_OCR_RE.sub(" ", line)
    line = _CROSSREF_RE.sub(" ", line)
    line = re.sub(r"\s{2,}", " ", line)
    return line.strip()


def extract_verses_from_page(
    raw_text: str,
    current_book: str,
    current_chapter: int,
    pdf_page: int,
    known_books: list[str],
) -> tuple[list[tuple], str, int]:
    """
    Parse a page's raw text into verse tuples.

    Returns:
        (verses_list, updated_book, updated_chapter)
        verses_list items: (book, chapter, verse_num, raw_text)
    """
    lines = raw_text.splitlines()
    verses = []

    # --- Step 1: detect header to update book/chapter context ---
    printed_page, book_from_header, chapter_range = parse_header(lines)

    if book_from_header:
        current_book = book_from_header
    elif not current_book:
        current_book = detect_book_from_title(lines, known_books)

    # Extract starting chapter from header range e.g. "6, 21-8, 6" → 6
    if chapter_range:
        m = re.match(r"(\d+)", chapter_range.strip())
        if m:
            header_chapter = int(m.group(1))
            # Only update if it looks valid
            if header_chapter > 0:
                current_chapter = header_chapter

    # --- Step 2: build a clean prose stream from all lines ---
    prose_parts = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip the header line itself
        if _HEADER_RE.match(line):
            continue
        cleaned = clean_line(stripped)
        if cleaned:
            prose_parts.append(cleaned)

    # Rejoin, healing hyphenated line-breaks: "gbo- bo" → "gbobo"
    prose = " ".join(prose_parts)
    prose = re.sub(r"-\s+", "", prose)   # heal hyphen breaks
    prose = re.sub(r"\s{2,}", " ", prose)

    # --- Step 3: split prose on inline verse numbers ---
    # Replace each verse-number token with a sentinel  ⟦N⟧
    def _sentinel(m):
        return f" ⟦{m.group(1)}⟧ "

    sentinelled = _VERSE_INLINE_RE.sub(_sentinel, prose)
    segments = re.split(r"⟦(\d+)⟧", sentinelled)

    # segments alternates: [pre-text, num, text, num, text, ...]
    i = 0
    while i < len(segments) - 1:
        # odd indices are verse numbers, even are text bodies
        if i % 2 == 1:
            try:
                vnum = int(segments[i])
            except ValueError:
                i += 1
                continue
            body = segments[i + 1].strip() if i + 1 < len(segments) else ""

            # If verse 1 appears and we had a pending chapter advance, handle it
            if vnum == 1 and current_chapter == 0:
                current_chapter = 1

            if body and current_chapter > 0 and current_book:
                verses.append((current_book, current_chapter, vnum, body))
            i += 2
        else:
            i += 1

    return verses, current_book, current_chapter


# ── Main extractor class ──────────────────────────────────────────────────────

class BibleExtractor:

    def __init__(self, pdf_path: Path, config_path: Path, db_path: Path):
        self.pdf_path = pdf_path
        self.db_path  = db_path

        with open(config_path, encoding="utf-8") as f:
            self.config = json.load(f)
        self.config["source_file"] = pdf_path.name

        self.cleaner = CleanupEngine(config_path)

        ot = list(self.config["book_names"].get("old_testament", {}).keys())
        nt = list(self.config["book_names"].get("new_testament", {}).keys())
        self.all_book_names: list[str] = sorted(ot + nt, key=lambda x: -len(x))

    def probe(self) -> None:
        print(f"\n📄  PDF: {self.pdf_path}")
        with pdfplumber.open(self.pdf_path) as pdf:
            total = len(pdf.pages)
            print(f"    Pages : {total}\n")
            for pnum in [1, 5, 10, 20, 50]:
                if pnum <= total:
                    raw = pdf.pages[pnum - 1].extract_text(layout=True) or ""
                    lines = [l for l in raw.splitlines() if l.strip()]
                    print(f"  --- PDF page {pnum} (first 6 non-empty lines) ---")
                    for line in lines[:6]:
                        print(f"    {repr(line)}")
                    # Try header parse
                    pp, book, cr = parse_header(lines)
                    print(f"    → parsed: page={pp} book={book!r} range={cr!r}")
                    print()

    def extract(self, page_range: tuple[int, int] | None = None) -> None:
        start_time = time.time()

        con = open_db(self.db_path)
        lang_id  = upsert_language(con, self.config)
        book_map = seed_books(con, lang_id, self.config)

        # Build a reverse map: normalised header name → canonical local_name
        # e.g. "MOSE I" → "MOSE I"  (handles trailing dots, extra spaces)
        def normalise_book(name: str) -> str:
            return re.sub(r"\s+", " ", name).strip().rstrip(".")

        norm_to_canonical: dict[str, str] = {}
        for local in book_map:
            norm_to_canonical[normalise_book(local).upper()] = local

        current_book    = ""
        current_chapter = 0

        pages_done  = 0
        verses_done = 0
        warnings    = 0
        errors      = 0
        chapter_cache: dict[tuple[str, int], int] = {}

        with pdfplumber.open(self.pdf_path) as pdf:
            total = len(pdf.pages)
            if page_range:
                indices = range(max(0, page_range[0] - 1), min(total, page_range[1]))
            else:
                indices = range(total)

            iterator = tqdm(indices, desc="Extracting", unit="pg") if HAS_TQDM else indices

            for idx in iterator:
                pdf_page_num = idx + 1
                try:
                    raw = pdf.pages[idx].extract_text(layout=True) or ""
                    if not raw.strip():
                        continue

                    lines = [l for l in raw.splitlines() if l.strip()]

                    # Parse header for page metadata
                    printed_page, book_header, chapter_range = parse_header(lines)

                    # Resolve book name to canonical form
                    if book_header:
                        norm = normalise_book(book_header).upper()
                        canonical_book = norm_to_canonical.get(norm, "")
                        if not canonical_book:
                            # Try partial match (e.g. "MOSE I" vs "MOSE I.")
                            for key, val in norm_to_canonical.items():
                                if norm.startswith(key) or key.startswith(norm):
                                    canonical_book = val
                                    break
                        if canonical_book:
                            current_book = canonical_book

                    # Save raw page
                    insert_page(
                        con, lang_id, pdf_page_num, printed_page,
                        raw, "", "",   # no column split — single stream
                        current_book, chapter_range,
                    )

                    # Extract verses
                    verse_tuples, current_book, current_chapter = extract_verses_from_page(
                        raw, current_book, current_chapter,
                        pdf_page_num, self.all_book_names,
                    )

                    for (book_local, chapter, vnum, raw_verse) in verse_tuples:
                        book_id = book_map.get(book_local)
                        if not book_id:
                            warnings += 1
                            logger.debug("Unknown book %r page %d", book_local, pdf_page_num)
                            continue

                        if chapter == 0:
                            warnings += 1
                            continue

                        cache_key = (book_local, chapter)
                        if cache_key not in chapter_cache:
                            chapter_cache[cache_key] = get_or_create_chapter(
                                con, book_id, chapter
                            )
                        chapter_id = chapter_cache[cache_key]

                        result = self.cleaner.clean_verse(raw_verse)
                        has_fn = bool(result.footnotes)

                        verse_id = insert_verse(
                            con, chapter_id, vnum,
                            raw_verse, result.clean_text,
                            pdf_page_num, has_fn, result.flags,
                        )
                        if result.footnotes:
                            insert_footnotes(con, verse_id, result.footnotes)

                        verses_done += 1

                    pages_done += 1

                    if pages_done % 50 == 0:
                        con.commit()
                        logger.info(
                            "Page %d/%d | verses: %d | warns: %d",
                            pdf_page_num, total, verses_done, warnings,
                        )

                except Exception as exc:
                    errors += 1
                    logger.error("Page %d failed: %s", pdf_page_num, exc, exc_info=True)

        con.commit()

        logger.info("Updating chapter verse counts …")
        for (_, _), chapter_id in chapter_cache.items():
            update_chapter_verse_count(con, chapter_id)
        con.commit()

        try:
            rebuild_fts(con)
        except Exception as e:
            logger.warning("FTS rebuild failed (non-fatal): %s", e)

        duration = time.time() - start_time
        log_run(con, lang_id, pages_done, verses_done, warnings, errors, duration)
        con.close()

        print(f"\n{'='*55}")
        print(f"  ✅  Extraction complete")
        print(f"  Pages processed : {pages_done}")
        print(f"  Verses inserted : {verses_done}")
        print(f"  Warnings        : {warnings}")
        print(f"  Errors          : {errors}")
        print(f"  Duration        : {duration:.1f}s")
        print(f"  Database        : {self.db_path}")
        print(f"{'='*55}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_page_range(s: str) -> tuple[int, int]:
    parts = s.split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Format: START-END e.g. 1-200")
    return int(parts[0]), int(parts[1])


def main() -> None:
    ap = argparse.ArgumentParser(description="Heritage Bible PDF → SQLite extractor")
    ap.add_argument("--pdf",     required=True)
    ap.add_argument("--config",  required=True)
    ap.add_argument("--db",      default=None)
    ap.add_argument("--pages",   type=parse_page_range, default=None, metavar="START-END")
    ap.add_argument("--probe",   action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    pdf_path    = Path(args.pdf)
    config_path = Path(args.config)

    if not pdf_path.exists():
        sys.exit(f"ERROR: PDF not found: {pdf_path}")
    if not config_path.exists():
        sys.exit(f"ERROR: Config not found: {config_path}")

    if args.db:
        db_path = Path(args.db)
    else:
        with open(config_path) as f:
            cfg = json.load(f)
        stem = f"{cfg['language']}_{cfg['year']}"
        db_path = Path(__file__).parent.parent / "data" / "output" / f"{stem}.sqlite"

    extractor = BibleExtractor(pdf_path, config_path, db_path)

    if args.probe:
        extractor.probe()
    else:
        extractor.extract(page_range=args.pages)


if __name__ == "__main__":
    main()
