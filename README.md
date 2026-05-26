# 🌍 Project Heritage-Data: Historical West African Text Parser

A high-performance text optimization and parsing engine developed by **Chained Stars Technology**. This project automates the extraction, cleanup, and structuring of raw, public-domain historical African language texts (starting with the 1871 Twi and 1913 Ewe translations) into optimized database assets.

## 🚀 The Mission

Modern digital language assets often simplify indigenous vocabulary. Chained Stars Technology is bridge-building between cultural preservation and modern data engineering. By rescuing 100+ year-old public domain texts from raw, flawed 1.1GB PDF/OCR scans, we are preserving deep cultural idioms, rare proverbs, and classical grammatical structures.

We transform messy historical scans into production-ready, lightweight, and structured formats (SQLite/JSON) for modern mobile and web application ecosystems.

## 🛠️ Technical Challenges & Architecture

Processing century-old West African prints involves unique data engineering hurdles:
1. **Specialized Extended Orthographies**: Handling distinct linguistic characters (`ɔ`, `ɛ`, `ɖ`, `ƒ`, `ʋ`) that generic OCR engines misinterpret or corrupt.
2. **Data De-noising**: Algorithmic parsing to strip out printing press artifacts, faded ink noise, and page-bleed text.
3. **Relational Structuring**: Tokenizing raw plain text blocks into clean, indexed relational tables mapping Books, Chapters, and Verses.

### Data Pipeline Flow
[Raw Scanned Image/PDF] ➔ [Custom Tesseract OCR Pipeline] ➔ [Python Text Standardization Script] ➔ [Structured SQLite Database Asset]

## 📁 Repository Structure

This public repository contains our foundational infrastructure, extraction tools, and schema layouts. 

```text
├── config/                  # Language mapping and character replacement configs
├── src/
│   ├── cleanup_engine.py    # Python scripts for regex-based text standardization
│   └── database_builder.py  # SQLite schema initializer and population scripts
├── schemas/
│   └── database_layout.sql  # Normalized relational database architecture
└── README.md
```

*Note: The final, fully compiled and verified Twi and Ewe SQLite database assets are proprietary assets of Chained Stars Technology and are maintained in a private repository.*

## 📊 Database Schema Preview

We structure our target datasets using a highly normalized relational model optimized for quick indexing on mobile devices:

```sql
CREATE TABLE languages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language_name TEXT NOT NULL, -- e.g., "Akuapem Twi (1871)", "Anlo Ewe (1913)"
    iso_code TEXT NOT NULL
);

CREATE TABLE books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language_id INTEGER,
    book_order INTEGER NOT NULL,
    book_name TEXT NOT NULL,
    testament TEXT CHECK(testament IN ('Old', 'New')),
    FOREIGN KEY(language_id) REFERENCES languages(id)
);

CREATE TABLE verses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER,
    chapter_num INTEGER NOT NULL,
    verse_num INTEGER NOT NULL,
    verse_text TEXT NOT NULL,
    FOREIGN KEY(book_id) REFERENCES books(id)
);
```

## 📈 About Chained Stars Technology

Chained Stars Technology builds robust software solutions that merge advanced data engineering with impactful local applications. 

* **Website**: https://chained-stars-technology.github.io/
* **Contact**: info.chainedstarstechnology@gmail.com 
* **LinkedIn**: linkedin.com/company/chainedstarstechnology
---
© 2026 Chained Stars Technology. All rights reserved. Code tooling is licensed under the MIT License.
