# Heritage Data Parser

A structured Python pipeline for extracting, cleaning, and storing text from
19th-century African-language Bible PDFs into a queryable SQLite database.

## Supported Sources

| Bible | Language | Year | Script | Status |
|-------|----------|------|--------|--------|
| Biblia alo Nonlo Kokoe la le Ewegbe me | Ewe | 1913 | Latin + ƒ ʋ ɖ ɔ ŋ ɛ | ✅ Configured |
| Akuapem Twi Bible | Akuapem Twi | 1871 | Latin + ɛ ɔ ŋ | ✅ Configured |

## Tech Stack

- **Extraction** — [`pdfplumber`](https://github.com/jsvine/pdfplumber) (layout-aware, two-column)
- **Storage** — SQLite 3 with FTS5 full-text search
- **Normalisation** — Custom regex pipeline per language config
- **Schema** — Normalised relational: Languages → Books → Chapters → Verses
- **Config** — JSON per language; no code changes needed to add a new Bible

## Project Structure

```
heritage-data-parser/
│
├── .gitignore               # Blocks *.pdf, *.sqlite, *.db, and /data/
├── README.md
│
├── config/
│   ├── ewe_1913.json        # Ewe: OCR artefact map, book names, page layout rules
│   └── akuapem_1871.json    # Twi: same structure, Twi-specific book names
│
├── data/                    # LOCAL ONLY — never pushed to GitHub
│   ├── raw/                 # Drop your PDFs here
│   └── output/              # Generated .sqlite databases land here
│
├── schemas/
│   └── database_layout.sql  # Normalised schema (Languages, Books, Chapters, Verses)
│
└── src/
    ├── __init__.py
    ├── cleanup_engine.py    # Text normalisation: OCR fixes, footnote stripping, Unicode NFC
    ├── database_builder.py  # All SQLite operations: schema, upserts, FTS, logging
    └── bible_extractor.py   # Pipeline orchestrator: PDF → columns → verses → DB
```

## Installation

```bash
# Clone and enter the project
git clone <your-repo-url>
cd heritage-data-parser

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# Install dependencies
pip install pdfplumber tqdm
```

## Usage

### 1. Add your PDFs

```bash
cp /path/to/ewe_1913.pdf       data/raw/
cp /path/to/akuapem_1871.pdf   data/raw/
```

### 2. Probe a PDF (inspect structure before extracting)

```bash
python src/bible_extractor.py \
  --pdf    data/raw/ewe_1913.pdf \
  --config config/ewe_1913.json \
  --probe
```

This prints the first few lines of sample pages so you can verify page headers,
book names, and verse number formatting match what the config expects.

### 3. Extract everything

```bash
# Ewe 1913
python src/bible_extractor.py \
  --pdf    data/raw/ewe_1913.pdf \
  --config config/ewe_1913.json

# Akuapem 1871
python src/bible_extractor.py \
  --pdf    data/raw/akuapem_1871.pdf \
  --config config/akuapem_1871.json
```

Databases are written to `data/output/ewe_1913.sqlite` and
`data/output/akuapem_1871.sqlite`.

### 4. Extract a page range (good for testing)

```bash
python src/bible_extractor.py \
  --pdf    data/raw/ewe_1913.pdf \
  --config config/ewe_1913.json \
  --pages  1-100
```

### 5. Query the database

```bash
sqlite3 data/output/ewe_1913.sqlite

-- First 10 verses of Genesis
SELECT book_local, chapter, verse, clean_text
FROM v_verses
WHERE book_standard = 'Genesis'
LIMIT 10;

-- Full-text search across all Ewe text
SELECT book_standard, chapter, verse, clean_text
FROM verses_fts
JOIN v_verses ON v_verses.verse_id = verses_fts.rowid
WHERE verses_fts MATCH 'Mawu'
LIMIT 20;

-- Book statistics
SELECT * FROM v_book_stats WHERE language_code = 'ewe_1913';
```

## PDF Structure (Ewe 1913)

The pipeline was designed around what direct inspection revealed:

- **Digitisation**: ABBYY FineReader 14 (2020) — text is selectable, no OCR needed
- **Layout**: Two text columns + narrow centre cross-reference column
- **Page header**: `PAGE_NUM   BOOK_NAME.   [chapter, verse-range]`
- **Verse numbers**: Inline within prose — e.g. `...enyo. 16 Tete Mawu gblo...`
- **Chapter headings**: Centred italic titles, indented ≥10 spaces
- **Footnotes**: Lowercase letter markers glued to words — `a`, `b`, `ff`
- **Cross-refs**: Mid-line noise from centre column — `Ps. 136, 7-9.`
- **Page numbering**: OT resets to 1, NT resets to 1 (tracked separately)
- **Special chars**: ƒ ʋ ɖ ɔ ŋ ɛ (UTF-8 NFC)
- **OCR artefacts**: `\\Vu` → `wu`, `wb` → `wɔ`, `riu` → `ŋu` etc. (see config)

## Database Schema

```
languages ──< books ──< chapters ──< verses
                                       │
                                       └──< footnotes

pages          (raw per-page dump for debugging)
extraction_log (one row per run)
verses_fts     (FTS5 virtual table on clean_text)
```

Views: `v_verses` (full join), `v_book_stats` (per-book counts).

## Adding a New Bible

1. Copy `config/ewe_1913.json` to `config/<lang>_<year>.json`
2. Update `language`, `year`, `book_names` (local → standard), and `ocr_artifacts`
3. Run `--probe` to verify headers match your config's `page_header_regex`
4. Extract

No code changes required.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Low verse count | Book names in PDF don't match config | Run `--probe`, compare headers, update `book_names` in config |
| High warning rate | Chapter tracker can't find chapter 1 | Check `chapter_range` format in `--probe` output |
| Garbled text | Unusual OCR artefacts | Open `.sqlite` pages table, find patterns, add to `ocr_artifacts.character_substitutions` |
| Missing Ewe chars | Font not embedded in PDF | Check `pdffonts data/raw/file.pdf`; may need OCR for that source |

## License

Source PDFs are historical documents (pre-1928, public domain).
This extraction code is MIT licensed.
