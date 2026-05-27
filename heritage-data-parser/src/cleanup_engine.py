"""
cleanup_engine.py
-----------------
Text normalisation pipeline for heritage Bible PDFs.

Handles all artefacts observed in the Ewe 1913 PDF:
  - ABBYY FineReader character substitutions  (\\Vu → wu, wb → wɔ, etc.)
  - Inline footnote markers  (a Do. 17, 24; → stripped, catalogued separately)
  - Cross-reference column noise  (Ps. 136, 7-9. mid-line)
  - Page-header lines  (760   ZAHARYA.   [3, 4-5, 8])
  - Hyphenated line-break rejoining  (gbo-|bo → gbobo)
  - Section headings embedded between columns
  - Unicode normalisation for Ewe special chars  (ƒ ʋ ɖ ɔ ŋ ɛ)
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)


# ── Data structures ───────────────────────────────────────────────────────────

class CleanResult(NamedTuple):
    clean_text: str
    footnotes: list[dict]       # [{marker, reference, raw}]
    flags: list[str]            # parser warnings


# ── Engine ────────────────────────────────────────────────────────────────────

class CleanupEngine:
    """
    Stateless normaliser — instantiate once, call clean_verse() repeatedly.
    """

    # Page-header line:  760   ZAHARYA.   [3, 4-5, 8]
    _HEADER_RE = re.compile(
        r"^\s*\d+\s+[A-ZÄÖÜ][A-ZÄÖÜ\s\.]+\s+\[\d[\d,\s\-]+\]\s*$"
    )

    # Footnote reference line:  a Ps. 136, 7-9.   or   b 8,20; Mo. III. 11.
    _FOOTNOTE_RE = re.compile(
        r"^([a-z]{1,2})\s+([A-Z][a-zA-Z\.]+[\s\d,;:\-\.]+)$"
    )

    # Inline footnote marker embedded in verse text: ...gbo.a 13 Eye...
    # Catches single/double lowercase letter glued to word or after punctuation
    _INLINE_MARKER_RE = re.compile(r"(?<=[\w\.,;!?\)])\b([a-z]{1,2})\b(?=\s+[A-Z\d])")

    # Cross-reference noise inside verse text: (Ps.33,9; Kor. II. 4,6.)
    _CROSSREF_NOISE_RE = re.compile(
        r"\b[A-Z][a-z]+\.?\s*(?:I{1,3}|IV|V|VI|VII|VIII)?\s*\d+[,\s\d\-;:\.]+(?=\s)"
    )

    # Hyphenated line-break: "gbo-\nbo" → "gbobo"
    _HYPHEN_BREAK_RE = re.compile(r"-\n\s*")

    # Multiple whitespace / stray newlines inside a verse
    _WHITESPACE_RE = re.compile(r"\s{2,}")

    # Section heading line (centered, title-case, no verse number, short)
    _SECTION_HEAD_RE = re.compile(r"^\s{10,}[A-ZÄÖÜ][a-zäöüƒʋɖɔŋɛ]")

    def __init__(self, config_path: str | Path):
        with open(config_path, encoding="utf-8") as f:
            self._cfg = json.load(f)

        raw_subs = self._cfg.get("ocr_artifacts", {}).get("character_substitutions", {})
        # Pre-compile substitution patterns (longest first to avoid partial matches)
        self._subs: list[tuple[re.Pattern, str]] = [
            (re.compile(re.escape(k)), v)
            for k, v in sorted(raw_subs.items(), key=lambda x: -len(x[0]))
        ]

        self._special_chars: list[str] = (
            self._cfg.get("special_characters", {}).get(
                "ewe_specific",
                self._cfg.get("special_characters", {}).get("twi_specific", [])
            )
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def is_header_line(self, line: str) -> bool:
        """True if the line is a PDF page-header (should be skipped entirely)."""
        return bool(self._HEADER_RE.match(line))

    def is_footnote_line(self, line: str) -> bool:
        """True if the line is a standalone footnote / cross-reference line."""
        return bool(self._FOOTNOTE_RE.match(line.strip()))

    def is_section_heading(self, line: str) -> bool:
        """True if the line looks like a centered section title."""
        stripped = line.strip()
        return (
            bool(self._SECTION_HEAD_RE.match(line))
            and len(stripped) < 80
            and not re.match(r"^\d", stripped)
        )

    def parse_footnote_line(self, line: str) -> dict | None:
        """Extract {marker, reference, raw} from a footnote line, or None."""
        m = self._FOOTNOTE_RE.match(line.strip())
        if not m:
            return None
        return {"marker": m.group(1), "reference": m.group(2).strip(), "raw": line.strip()}

    def clean_verse(self, raw: str) -> CleanResult:
        """
        Full normalisation pipeline for a single verse string.

        Returns CleanResult(clean_text, footnotes, flags).
        """
        flags: list[str] = []
        footnotes: list[dict] = []
        text = raw

        # 1. Rejoin hyphenated line-breaks first
        text = self._HYPHEN_BREAK_RE.sub("", text)

        # 2. OCR character substitutions (order matters — longest keys first)
        for pattern, replacement in self._subs:
            text = pattern.sub(replacement, text)

        # 3. Strip inline footnote markers and record them
        def _capture_marker(m: re.Match) -> str:
            footnotes.append({"marker": m.group(1), "reference": "", "raw": m.group(0)})
            return ""

        text = self._INLINE_MARKER_RE.sub(_capture_marker, text)

        # 4. Strip cross-reference noise injected by the middle column
        before = text
        text = self._CROSSREF_NOISE_RE.sub("", text)
        if text != before:
            flags.append("crossref_noise_removed")

        # 5. Collapse whitespace
        text = self._WHITESPACE_RE.sub(" ", text).strip()

        # 6. Unicode NFC normalisation
        text = unicodedata.normalize("NFC", text)

        # 7. Sanity checks
        if len(text) < 3:
            flags.append("suspiciously_short")
        if re.search(r"[\\^]{1}", text):
            flags.append("residual_ocr_artefact")
        if footnotes:
            flags.append(f"inline_footnotes:{len(footnotes)}")

        return CleanResult(clean_text=text, footnotes=footnotes, flags=flags)

    def clean_page_block(self, block: str) -> tuple[str, list[dict]]:
        """
        Given the raw text of a merged two-column page block, strip all
        non-verse lines and return (cleaned_prose, footnote_list).

        This is used by the extractor before verse splitting.
        """
        footnotes: list[dict] = []
        kept_lines: list[str] = []

        for line in block.splitlines():
            if self.is_header_line(line):
                continue
            if self.is_section_heading(line):
                continue
            fn = self.parse_footnote_line(line)
            if fn:
                footnotes.append(fn)
                continue
            kept_lines.append(line)

        rejoined = "\n".join(kept_lines)
        # Rejoin hyphen line-breaks across the cleaned lines
        rejoined = self._HYPHEN_BREAK_RE.sub("", rejoined)
        return rejoined, footnotes

    # ── Utility ───────────────────────────────────────────────────────────────

    @staticmethod
    def normalize_unicode(text: str) -> str:
        return unicodedata.normalize("NFC", text)
