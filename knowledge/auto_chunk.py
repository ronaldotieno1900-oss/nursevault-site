#!/usr/bin/env python3
"""
NurseVault knowledge auto-chunker  (v2)
---------------------------------------
Ingests PDFs, JSON cards, and Markdown/TXT notes from knowledge/incoming/
(and optional extra paths) and writes the same kb.json / kb_compact.json
format the Worker already reads. No Worker changes are required.

What changed from v1
  * Headings are detected (numbered 1.13.4, CHAPTER n, ALL CAPS, standalone
    short lines, Markdown #). Each chunk carries a `section` and its title is
    "<document> › <section>" instead of "<file name> – part N".
  * Chunks break at section boundaries; the overlap starts at a word/sentence
    boundary and never crosses into another section.
  * Nothing is silently truncated: oversized blocks are split, not cut.
  * Short heading lines are kept (v1 dropped every paragraph <= 40 chars).
  * Page numbers are recorded (`page`) from pdftotext page breaks.
  * Running headers/footers, bare page numbers, table-of-contents pages and
    foreword/acknowledgement pages are removed.
  * Tags use word-boundary regexes ("heart" no longer triggers HIV,
    "laboratory" no longer triggers obstetric). Many more topics.
  * pdftotext now runs in reading-order mode by default (-layout interleaves
    table columns). Use --layout to restore the old behaviour.
  * Chunks that look like tables get flags=["table"] so you can check doses.
  * Optional --enrich: Claude writes a descriptive title, topics and summary
    for every chunk (cached, so re-runs cost nothing for unchanged chunks).

Usage
  python3 auto_chunk.py
  python3 auto_chunk.py --src /path/to/pdfs --out chunks/kb_all.json
  python3 auto_chunk.py --enrich                 # needs ANTHROPIC_API_KEY
  python3 auto_chunk.py --enrich --enrich-limit 50   # try on 50 chunks first
  python3 auto_chunk.py --selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
DEFAULT_INCOMING = ROOT / "incoming"
DEFAULT_SAMPLES = ROOT / "samples"
DEFAULT_OUT = REPO_ROOT / "kb.json"  # served by Cloudflare as /kb.json

TARGET_CHARS = 1100        # preferred chunk size
MAX_CHARS = 1700           # hard ceiling (text is split, never cut)
MIN_CHARS = 80             # smaller leftovers are merged or dropped
MIN_SECTION_CHARS = 250    # don't break at a heading until the buffer is this big
OVERLAP_CHARS = 200
DEFAULT_ENRICH_MODEL = os.environ.get("NV_ENRICH_MODEL", "claude-haiku-5-5")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def slug(s: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:max_len] or "item"


_TITLE_NOISE = re.compile(
    r"\(\d+\)|\b(compressed|signed|shareable|do not use for print|copy|final)\b", re.I
)


def pretty_title(stem: str) -> str:
    t = stem.replace("_", " ").replace("-", " ")
    t = _TITLE_NOISE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip(" -\u2013")


def clean_page(page: str) -> str:
    page = page.replace("\x00", " ").replace("\f", "")
    page = re.sub(r"[ \t]+", " ", page)
    page = re.sub(r" ?\n ?", "\n", page)
    page = re.sub(r"\n{3,}", "\n\n", page)
    return page.strip()


def clean_text(text: str) -> str:
    return clean_page(text)


# --------------------------------------------------------------------------
# topic tags (word boundaries; acronyms are case-sensitive)
# --------------------------------------------------------------------------

_TOPIC_PATTERNS = {
    "diabetes": r"\bdiabet\w*|\binsulin\b|\bglucose\b|\bhypoglyc\w*|\bhyperglyc\w*|\bketoacidosis\b|\bhba1c\b",
    "hypertension": r"\bhypertensi\w*|\bblood pressure\b|\bantihypertensive\w*",
    "cardiovascular": r"\bcardiac\b|\bcardiovascular\b|\bheart (?:failure|disease|attack)\b|\bmyocardial\b|\bstroke\b|\barrhythmias?\b|\bangina\b",
    "anaphylaxis": r"\banaphyla\w*|\badrenaline\b|\bepinephrine\b",
    "allergy": r"\ballerg\w*",
    "tb": r"\btuberculosis\b|\brifampicin\b|\bisoniazid\b",
    "hiv": r"\bhiv\b|\bantiretroviral\w*|\bpmtct\b",
    "malaria": r"\bmalaria\b|\bartesunate\b|\bartemether\b|\blumefantrine\b",
    "obstetric": r"\bpregnan\w*|\blabou?r\b|\bpostpartum\b|\beclampsia\b|\bantenatal\b|\bpostnatal\b|\bmidwi\w*",
    "neonatal": r"\bneonat\w*|\bnewborn\w*",
    "paediatrics": r"\bpaediatric\w*|\bpediatric\w*|\binfants?\b|\bimci\b",
    "respiratory": r"\basthma\b|\bpneumonia\b|\brespiratory\b",
    "cancer": r"\bcancer\b|\bchemotherapy\b|\bmalignan\w*|\boncolog\w*|\bleuka?emia\b",
    "mental health": r"\bpsychiatr\w*|\bdepress\w*|\bschizophreni\w*|\bpsychosis\b|\bsuicid\w*",
    "family planning": r"\bcontracepti\w*|\bfamily planning\b",
    "immunization": r"\bimmuni[sz]ation\b|\bvaccin\w*",
    "infection control": r"\binfection prevention\b|\bhand hygiene\b|\bsterili[sz]\w*|\bppe\b",
    "pharmacology": r"\bcontraindicat\w*|\bside[- ]effects?\b|\badverse (?:effects?|reactions?)\b|\b\d+(?:\.\d+)?\s?(?:mg|mcg|\u00b5g|iu)\b",
    "emergency": r"\bresuscitat\w*|\bcardiac arrest\b|\bshock\b|\bairway\b|\bcpr\b",
}
_ACRONYM_PATTERNS = {
    "hiv": r"\bART\b|\bARVs?\b",
    "obstetric": r"\bPPH\b|\bAPH\b|\bMgSO4\b",
    "diabetes": r"\bDKA\b|\bHHS\b|\bT[12]DM\b",
    "hypertension": r"\bHTN\b",
}
TOPIC_RX = {k: re.compile(v, re.I) for k, v in _TOPIC_PATTERNS.items()}
ACRONYM_RX = {k: re.compile(v) for k, v in _ACRONYM_PATTERNS.items()}


def auto_topics(text: str, given: list[str] | None = None, cap: int = 12) -> list[str]:
    out = list(given or [])
    for topic, rx in TOPIC_RX.items():
        if topic not in out and rx.search(text):
            out.append(topic)
    for topic, rx in ACRONYM_RX.items():
        if topic not in out and rx.search(text):
            out.append(topic)
    return out[:cap]


# --------------------------------------------------------------------------
# PDF text -> pages -> cleaned pages
# --------------------------------------------------------------------------

def extract_pdf_pages(path: Path, layout: bool = False) -> list[str]:
    """One string per PDF page (empty strings keep page numbering intact)."""
    cmd = ["pdftotext"] + (["-layout"] if layout else []) + [str(path), "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode == 0 and r.stdout.strip():
            pages = r.stdout.decode("utf-8", errors="ignore").split("\f")
            if pages and not pages[-1].strip():
                pages.pop()
            return pages
    except (OSError, subprocess.SubprocessError):
        pass
    if PdfReader is None:
        raise RuntimeError("No PDF extractor available (install poppler-utils or pypdf)")
    pages = []
    for page in PdfReader(str(path)).pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return pages


_PAGE_NUM = re.compile(r"^(?:page\s+)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)


def _norm_line(line: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", line.strip().lower()))


def strip_running_lines(pages: list[str]) -> list[str]:
    """Remove headers/footers that repeat on many pages, and bare page numbers."""
    n = len(pages)
    running: set[str] = set()
    if n >= 4:
        counts: Counter = Counter()
        for p in pages:
            ls = [l for l in p.splitlines() if l.strip()]
            edge = ls[:3] + ls[-3:]
            for k in {_norm_line(l) for l in edge if len(l.strip()) <= 140}:
                counts[k] += 1
        threshold = max(3, int(n * 0.3))
        running = {k for k, c in counts.items() if c >= threshold and k not in ("", "#")}
    out = []
    for p in pages:
        lines = p.splitlines()
        idx = [i for i, l in enumerate(lines) if l.strip()]
        edge = set(idx[:3] + idx[-3:])
        keep = [
            l for i, l in enumerate(lines)
            if not (i in edge and (_norm_line(l) in running or _PAGE_NUM.match(l.strip())))
        ]
        out.append("\n".join(keep))
    return out


def is_toc_page(page: str) -> bool:
    ls = [l.strip() for l in page.splitlines() if l.strip()]
    n = len(ls)
    if n < 6:
        return False
    dots = sum(1 for l in ls if re.search(r"\.{4,}\s*\d{1,4}$", l))
    entries = sum(
        1 for l in ls
        if re.match(r"^(table|figure|annex|appendix|chapter|section)\s+[\w.]+", l, re.I)
        and re.search(r"\s\d{1,4}$", l)
    )
    head = "\n".join(ls[:6])
    hint = bool(re.search(r"^(table of )?contents$|list of (?:tables|figures)", head, re.I | re.M))
    ends = sum(1 for l in ls if re.search(r"\s\d{1,4}$", l))
    return dots / n >= 0.3 or entries / n >= 0.3 or (hint and ends / n >= 0.4)


_FRONT_TITLE = re.compile(
    r"^(foreword|acknowledg\w*|preface|disclaimer|list of contributors|contributors"
    r"|list of abbreviations|abbreviations and acronyms|abbreviations)\s*$", re.I
)


def drop_front_matter(pages: list[str]) -> tuple[list[str], int]:
    kept, dropped = [], 0
    for i, p in enumerate(pages):
        head = [l.strip() for l in p.splitlines() if l.strip()][:4]
        front = i < 30 and any(_FRONT_TITLE.match(l) for l in head)
        if is_toc_page(p) or front:
            kept.append("")
            dropped += 1
        else:
            kept.append(p)
    return kept, dropped


# --------------------------------------------------------------------------
# headings and units
# --------------------------------------------------------------------------

_NUM_HEAD = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+([A-Z][^\n]*)$")
_CHAPTER = re.compile(r"^(chapter|section|annex|appendix)\s+([0-9]+|[ivxlc]+|[A-Z])\b[\s:.\-\u2013]*(.*)$", re.I)
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_END_NUM = re.compile(r"(\.{3,}|\s)\d{1,4}$")


def heading_info(line: str, md: bool = False):
    """Return (depth, text) if the line is a heading, else None."""
    s = line.strip()
    if not s or len(s) > 110:
        return None
    if md:
        m = _MD_HEAD.match(s)
        return (len(m.group(1)), m.group(2).strip()) if m else None
    if re.match(r"^(table|figure|fig\.)\s+\d", s, re.I):
        return None
    m = _NUM_HEAD.match(s)
    if m:
        num, text = m.group(1), m.group(2).strip()
        levels = num.count(".") + 1
        words = len(text.split())
        if text[-1:] in ".,;:" or re.search(r"\.{3,}", s) or _END_NUM.search(s):
            return None
        if levels == 1 and (words > 7 or len(text) > 60 or "," in text):
            return None  # "1. Mobilise early to prevent ..." is a list item
        if levels > 1 and words > 14:
            return None
        return (levels, f"{num} {text}")
    m = _CHAPTER.match(s)
    if m and len(s.split()) <= 12 and s[-1] not in ".,;:" and not _END_NUM.search(s):
        return (1, s)
    letters = [c for c in s if c.isalpha()]
    if (len(letters) >= 4 and s.upper() == s and len(s.split()) <= 10
            and len(letters) / len(s) >= 0.6 and s[-1] not in ".,;:" and not _END_NUM.search(s)):
        return (2, s)
    return None


def standalone_heading(block: str, next_block: str | None):
    """A short title-like line alone in its block, followed by a real paragraph."""
    s = block.strip()
    if "\n" in s or not next_block or len(next_block) < 100:
        return None
    if len(s) > 60 or not s[:1].isupper() or s[-1:] in ".,;:?" or _END_NUM.search(s):
        return None
    if len(s.split()) > 8 or re.match(r"^(table|figure)\s", s, re.I):
        return None
    return (2, s)  # weak evidence: sibling of ALL-CAPS/numbered level-2, never nested under them


def push_heading(stack: list, depth: int, text: str) -> None:
    while stack and stack[-1][0] >= depth:
        stack.pop()
    stack.append((depth, text))


def path_of(stack: list) -> tuple:
    return tuple(t for _, t in stack)


def section_str(path: tuple) -> str:
    return " > ".join(re.sub(r"\s+", " ", p)[:70] for p in path[-2:])


def build_units(pages: list[str], md: bool = False) -> list[dict]:
    """Split pages into text units, each tagged with its page and heading path.
    Heading-only blocks travel with the text that follows them (never dropped)."""
    units: list[dict] = []
    stack: list = []
    carry: list = []
    carry_page = None

    def emit(cur, page, path):
        text = "\n".join(l for l, _ in cur).strip()
        if text:
            units.append({"page": page, "path": path, "text": text})

    for pno, raw in enumerate(pages, start=1):
        page = clean_page(raw)
        if not page:
            continue
        blocks = re.split(r"\n\s*\n", page)
        for bi, block in enumerate(blocks):
            cur = carry
            cur_page = carry_page if carry else pno
            carry, carry_page = [], None
            lines = block.split("\n")
            sh = None
            if not md and len(lines) == 1:
                sh = standalone_heading(block, blocks[bi + 1] if bi + 1 < len(blocks) else None)
            for line in lines:
                h = heading_info(line, md) or sh
                if h:
                    if cur and not all(isH for _, isH in cur):
                        emit(cur, cur_page, path_of(stack))
                        cur, cur_page = [], pno
                    push_heading(stack, *h)
                    cur.append((line.strip(), True))
                else:
                    cur.append((line, False))
            if cur and all(isH for _, isH in cur):
                carry, carry_page = cur, cur_page
            else:
                emit(cur, cur_page, path_of(stack))
    if carry:
        emit(carry, carry_page, path_of(stack))
    return units


# --------------------------------------------------------------------------
# splitting / packing
# --------------------------------------------------------------------------

def _atoms(text: str, limit: int) -> list[str]:
    atoms: list[str] = []
    for line in text.split("\n"):
        if len(line) <= limit:
            atoms.append(line)
            continue
        for sent in re.split(r"(?<=[.!?])\s+", line):
            while len(sent) > limit:
                cut = sent.rfind(" ", 0, limit)
                if cut <= 0:
                    cut = limit
                atoms.append(sent[:cut])
                sent = sent[cut:].lstrip()
            if sent:
                atoms.append(sent)
    return atoms


def split_oversized(text: str, limit: int) -> list[str]:
    """Split text into pieces <= limit on line/sentence/word boundaries. Loses nothing."""
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for a in _atoms(text, limit):
        if not buf:
            buf = a
        elif len(buf) + 1 + len(a) <= limit:
            buf += "\n" + a
        else:
            out.append(buf)
            buf = a
    if buf:
        out.append(buf)
    return out


def tail_overlap(text: str, n: int = OVERLAP_CHARS) -> str:
    """Last ~n chars, starting at a line/sentence/word boundary (never mid-word)."""
    if len(text) <= n:
        return ""
    tail = text[-n:]
    m = re.search(r"\n|(?<=[.!?]) ", tail)
    if m:
        return tail[m.end():].strip()
    m = re.search(r"\s", tail)
    return tail[m.end():].strip() if m else ""


def page_label(p0, p1) -> str:
    if p0 is None:
        return ""
    return str(p0) if p1 in (None, p0) else f"{p0}\u2013{p1}"


def looks_like_table(text: str) -> bool:
    """Advisory only: a run of >= 6 consecutive very short lines (table cells),
    or a 'Table N' caption next to dose units."""
    run = best = 0
    for l in text.split("\n"):
        if l.strip() and len(l.strip()) <= 30:
            run += 1
            best = max(best, run)
        else:
            run = 0
    if best >= 6:
        return True
    return bool(re.search(r"^table\s+\d+", text, re.I | re.M)) and bool(
        re.search(r"\d\s?(?:mg|mcg|ml|g|iu|%)", text, re.I)
    )


def pack_units(units, *, doc_title, source, doc_type, topics, id_prefix,
               target_chars=TARGET_CHARS, max_chars=MAX_CHARS) -> list[dict]:
    chunks: list[dict] = []
    sec_counts: Counter = Counter()

    def emit(text, paths, p0, p1):
        text = text.strip()
        if len(text) > max_chars:  # safety net: split, never truncate
            for part in split_oversized(text, max_chars):
                emit(part, paths, p0, p1)
            return
        if len(text) < MIN_CHARS:
            if chunks and chunks[-1]["_path"] == paths[0] and len(chunks[-1]["text"]) + 1 + len(text) <= max_chars:
                chunks[-1]["text"] += "\n" + text
            return
        # A chunk that merged several tiny sections names all of them.
        section = section_str(paths[0])
        extras = [p[-1] for p in paths[1:] if p and p[-1] not in section]
        if extras:
            section = (section + " + " + " + ".join(dict.fromkeys(extras)))[:160]
        sec_counts[section] += 1
        k = sec_counts[section]
        base = f"{doc_title} \u203a {section}" if section else doc_title
        chunks.append({
            "id": f"{id_prefix}-{len(chunks) + 1:04d}",
            "title": (base if k == 1 else f"{base} \u2013 part {k}")[:200],
            "source": source,
            "type": doc_type,
            "topics": [],
            "section": section,
            "page": page_label(p0, p1),
            "text": text,
            "_path": paths[0],
        })

    buf, buf_paths, p0, p1 = "", [], None, None
    overlap, overlap_path = "", ()

    for u in units:
        for piece in split_oversized(u["text"], target_chars):
            new_chapter = bool(buf) and u["path"][:1] != buf_paths[-1][:1]
            if buf and u["path"] != buf_paths[-1] and (
                    len(buf) >= MIN_SECTION_CHARS or (new_chapter and len(buf) >= 2 * MIN_CHARS)):
                emit(buf, buf_paths, p0, p1)
                buf, overlap = "", ""
            elif buf and len(buf) + 2 + len(piece) > target_chars:
                emit(buf, buf_paths, p0, p1)
                overlap, overlap_path = tail_overlap(buf), buf_paths[-1]
                buf = ""
            if not buf:
                use_overlap = overlap and overlap_path == u["path"]
                buf = f"{overlap}\n{piece}" if use_overlap else piece
                buf_paths, p0, p1 = [u["path"]], u["page"], u["page"]
                overlap = ""
            else:
                buf += "\n\n" + piece
                if u["path"] != buf_paths[-1]:
                    buf_paths.append(u["path"])
                p1 = u["page"]
    if buf:
        emit(buf, buf_paths, p0, p1)

    for c in chunks:
        c.pop("_path", None)
        c["topics"] = auto_topics(f"{c['section']}\n{c['text']}", topics)
        if looks_like_table(c["text"]):
            c["flags"] = ["table"]
    return chunks


def chunk_document(pages, *, source, title, doc_type, topics=None, id_prefix,
                   md=False, target_chars=TARGET_CHARS, max_chars=MAX_CHARS) -> list[dict]:
    units = build_units(pages, md=md)
    return pack_units(units, doc_title=title, source=source, doc_type=doc_type,
                      topics=topics or [], id_prefix=id_prefix,
                      target_chars=target_chars, max_chars=max_chars)


# --------------------------------------------------------------------------
# ingest by file type  (each returns (chunks, info))
# --------------------------------------------------------------------------

def ingest_json(path: Path, opts) -> tuple[list[dict], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else [data]
    out: list[dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        text = clean_text(str(item.get("text") or item.get("content") or ""))
        if len(text) < 40:
            continue
        title = item.get("title") or item.get("name") or path.stem
        source = item.get("source") or f"JSON:{path.name}"
        topics = item.get("topics") or item.get("tags") or []
        if isinstance(topics, str):
            topics = [t.strip() for t in topics.split(",") if t.strip()]
        doc_type = item.get("type") or "note"
        cid = item.get("id") or f"{slug(path.stem)}-{i + 1:03d}"
        if len(text) <= MAX_CHARS:
            out.append({"id": cid, "title": title, "source": source, "type": doc_type,
                        "topics": topics, "text": text})
        else:
            out.extend(chunk_document([text], source=source, title=title, doc_type=doc_type,
                                      topics=topics, id_prefix=cid))
    return out, {}


def ingest_markdown(path: Path, opts) -> tuple[list[dict], dict]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    title_m = re.search(r"^#\s+(.+)$", raw, re.M)
    title = title_m.group(1).strip() if title_m else path.stem.replace("_", " ")
    source_m = re.search(r"^Source:\s*(.+)$", raw, re.M | re.I)
    source = source_m.group(1).strip() if source_m else f"Notes:{path.name}"
    topics_m = re.search(r"^Topics:\s*(.+)$", raw, re.M | re.I)
    topics = [t.strip() for t in re.split(r"[,;]", topics_m.group(1)) if t.strip()] if topics_m else []
    body = re.sub(r"^#\s+.+$", "", raw, count=1, flags=re.M)
    body = re.sub(r"^Source:\s*.+$", "", body, flags=re.M | re.I)
    body = re.sub(r"^Topics:\s*.+$", "", body, flags=re.M | re.I)
    chunks = chunk_document([body], source=source, title=title, doc_type="note",
                            topics=topics, id_prefix=slug(path.stem), md=path.suffix.lower() == ".md")
    return chunks, {}


def ingest_pdf(path: Path, opts) -> tuple[list[dict], dict]:
    pages = extract_pdf_pages(path, layout=opts.layout)
    info = {"pages": len(pages)}
    pages = strip_running_lines(pages)
    if not opts.keep_front_matter:
        pages, info["pages_dropped"] = drop_front_matter(pages)
    if sum(len(p.strip()) for p in pages) < 100:
        info["warning"] = "almost no text found (scanned PDF? it needs OCR first)"
        return [], info
    chunks = chunk_document(pages, source=f"PDF:{path.name}", title=pretty_title(path.stem),
                            doc_type="guideline", id_prefix=slug(path.stem))
    info["table_chunks"] = sum(1 for c in chunks if "table" in c.get("flags", []))
    return chunks, info


def ingest_file(path: Path, opts) -> tuple[list[dict], dict]:
    suf = path.suffix.lower()
    if suf == ".json":
        return ingest_json(path, opts)
    if suf in (".md", ".txt"):
        return ingest_markdown(path, opts)
    if suf == ".pdf":
        return ingest_pdf(path, opts)
    return [], {}


def collect_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.suffix.lower() in {".pdf", ".json", ".md", ".txt"} and f.is_file():
                    files.append(f)
    seen, out = set(), []
    for f in files:
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


# --------------------------------------------------------------------------
# optional AI enrichment (title / topics / summary per chunk)
# --------------------------------------------------------------------------

ENRICH_SYSTEM = (
    "You label chunks of Kenyan clinical guideline text for a search index used by nursing "
    "students. Use ONLY facts present in the text; never add doses or claims. "
    "Reply with a single JSON object and nothing else."
)


def call_claude(prompt: str, system: str, model: str, key: str,
                max_tokens: int = 350, retries: int = 4):
    body = json.dumps({
        "model": model, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {"content-type": "application/json", "x-api-key": key,
               "anthropic-version": "2023-06-01"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                         data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            return "".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception:
            time.sleep(2 ** attempt)
    return None


def enrich_one(chunk: dict, model: str, key: str):
    prompt = (
        f"Document: {chunk['source']}\nSection: {chunk.get('section') or 'n/a'}\n"
        f"Page: {chunk.get('page') or 'n/a'}\n\nTEXT:\n{chunk['text']}\n\n"
        'Return JSON: {"title": "specific descriptive title, max 90 chars, no file name", '
        '"topics": ["3-8 lowercase clinical keywords incl. common abbreviations"], '
        '"summary": "one sentence, max 200 chars", '
        '"keep": false only if the text is a table of contents, reference list, copyright '
        "notice or has no clinical content, otherwise true}"
    )
    raw = call_claude(prompt, ENRICH_SYSTEM, model, key)
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    topics = [str(t).lower().strip()[:40] for t in (d.get("topics") or []) if str(t).strip()][:8]
    return {
        "title": str(d.get("title") or "").strip()[:100],
        "topics": topics,
        "summary": str(d.get("summary") or "").strip()[:240],
        "keep": bool(d.get("keep", True)),
    }


def enrich_chunks(chunks, *, model, key, limit, drop_junk, cache_path: Path, workers=4):
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    ck = lambda c: f"{model}:{c['hash']}"
    todo = [c for c in chunks if ck(c) not in cache]
    if limit:
        todo = todo[:limit]
    print(f"Enrich: {len(chunks) - len(todo)} cached/skipped, {len(todo)} to call ({model})")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for c, res in zip(todo, ex.map(lambda c: enrich_one(c, model, key), todo)):
            if res:
                cache[ck(c)] = res
            done += 1
            if done % 50 == 0:
                cache_path.write_text(json.dumps(cache), encoding="utf-8")
                print(f"  enriched {done}/{len(todo)}")
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    out, applied, junk = [], 0, 0
    for c in chunks:
        res = cache.get(ck(c))
        if res:
            applied += 1
            if res["title"]:
                c["title"] = res["title"]
            c["topics"] = list(dict.fromkeys(res["topics"] + c.get("topics", [])))[:12]
            if res["summary"]:
                c["summary"] = res["summary"]
            if not res["keep"]:
                junk += 1
                if drop_junk:
                    continue
                c["flags"] = list(dict.fromkeys(c.get("flags", []) + ["junk"]))
        out.append(c)
    print(f"Enrich: applied to {applied} chunks, {junk} flagged junk"
          f"{' (dropped)' if drop_junk else ''}")
    return out


# --------------------------------------------------------------------------
# self test (no PDF or network needed)
# --------------------------------------------------------------------------

def selftest() -> int:
    fails = []

    def check(name, cond):
        print(("PASS  " if cond else "FAIL  ") + name)
        if not cond:
            fails.append(name)

    hdr = "Kenya Clinical Guidelines 2026"
    pages = [
        f"{hdr}\n\nTable of Contents\n" + "\n".join(f"Table {i}: Something about care {i} {100 + i}" for i in range(1, 9)) + "\n\n1",
        f"{hdr}\n\nForeword\n\nThis foreword welcomes readers to the guidelines and thanks partners.\n\n2",
        f"{hdr}\n\n1.13 Anaphylaxis\n\nAnaphylaxis is a life-threatening emergency. Patients with heart failure need "
        "laboratory tests early. Adrenaline is first-line treatment and must be given without delay by the intramuscular route into the "
        "anterolateral thigh, with airway, breathing and circulation assessed continuously by the nurse in charge.\n\n1.13.4 Management\n\n"
        "1. Mobilise early to prevent complications such as pneumonia and DVT.\n2. Give IM adrenaline 0.5 mg and repeat after five minutes if "
        "there is no improvement, then set up an infusion and monitor blood pressure, pulse and oxygen saturation every five minutes "
        "until the patient is stable and transferred.\n\nPage 3",
        f"{hdr}\n\nEclampsia\n\nOccurrence of one or more generalized tonic clonic seizures superimposed on "
        "pre-eclampsia at any point in pregnancy requires magnesium sulphate, control of blood pressure, protection of the airway and urgent "
        "escalation of care to a facility with an obstetrician, with continuous monitoring of the mother and the fetus.\n\nPage 4",
        f"{hdr}\n\n" + ("Drug dosing table row alpha beta gamma. " * 90) + "ENDMARK\n\nPage 5",
        f"{hdr}\n\nTREATMENT OF HIV\n\nStart ART in all people living with HIV regardless of CD4 count, using "
        "antiretroviral combinations per national guidelines.\n\nPage 6",
    ]
    pages = strip_running_lines(pages)
    pages, dropped = drop_front_matter(pages)
    chunks = chunk_document(pages, source="PDF:test.pdf", title="Test Guidelines",
                            doc_type="guideline", id_prefix="test")
    alltext = "\n".join(c["text"] for c in chunks)

    check("running header removed", hdr not in alltext)
    check("bare page numbers removed", not re.search(r"^Page \d$", alltext, re.M))
    check("TOC page and foreword dropped", dropped == 2 and "Table 4: Something" not in alltext
          and "foreword" not in alltext.lower())
    ana = next((c for c in chunks if "Adrenaline is first-line" in c["text"]), None)
    check("heading path kept in section/title", bool(ana) and "1.13" in ana["section"] and "Test Guidelines" in ana["title"])
    check("'1. Mobilise early...' is not treated as a heading", all("Mobilise" not in c["section"] for c in chunks))
    check("false tags gone (heart/laboratory)", bool(ana) and "hiv" not in ana["topics"]
          and "obstetric" not in ana["topics"] and "anaphylaxis" in ana["topics"])
    ecl = next((c for c in chunks if "tonic clonic" in c["text"]), None)
    check("standalone short heading kept as section", bool(ecl) and "Eclampsia" in ecl["section"] and "Eclampsia" in ecl["text"])
    check("page numbers recorded", bool(ecl) and ecl["page"] == "4")
    check("weak heading gets no wrong parent", bool(ecl) and "Anaphylaxis" not in ecl["title"])
    tiny = chunk_document(["1.1 Alpha\n\nShort alpha note about oedema.\n\n1.2 Beta\n\nShort beta note about fluids and urine output monitoring."],
                          source="s", title="T", doc_type="g", id_prefix="m")
    check("tiny sections merge, name both, lose no text", len(tiny) == 1 and "Alpha" in tiny[0]["section"]
          and "Beta" in tiny[0]["section"] and "alpha note" in tiny[0]["text"] and "beta note" in tiny[0]["text"])
    check("oversized block loses nothing (ENDMARK kept)", "ENDMARK" in alltext)
    check("no chunk exceeds MAX_CHARS", all(len(c["text"]) <= MAX_CHARS for c in chunks))
    hiv = next((c for c in chunks if "antiretroviral" in c["text"]), None)
    check("real HIV text tagged hiv", bool(hiv) and "hiv" in hiv["topics"])
    sample = "alpha beta gamma delta. " * 40
    ov = tail_overlap(sample)
    check("overlap starts at a boundary, not mid-word",
          bool(ov) and sample.rstrip().endswith(ov) and sample.rstrip()[-len(ov) - 1].isspace())
    check("no chunk starts mid-word", all(not re.match(r"^[a-z]{1,4}\s", c["text"]) or c["text"].split()[0] in
                                          {"and", "the", "of", "in", "to", "or", "a", "for", "on", "at", "is", "as"}
                                          for c in chunks))
    print(f"\n{len(chunks)} chunks built; {'ALL TESTS PASSED' if not fails else str(len(fails)) + ' FAILED'}")
    return 1 if fails else 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Auto-chunk knowledge files for Vault AI RAG")
    ap.add_argument("--src", nargs="*", default=[], help="Extra source files or directories")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="Output JSON path")
    ap.add_argument("--attachments", default="", help="Optional extra directory of PDFs")
    ap.add_argument("--layout", action="store_true", help="Use pdftotext -layout (v1 behaviour)")
    ap.add_argument("--keep-front-matter", action="store_true",
                    help="Keep TOC / foreword / abbreviation pages")
    ap.add_argument("--enrich", action="store_true",
                    help="Ask Claude for a title, topics and summary per chunk (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--enrich-model", default=DEFAULT_ENRICH_MODEL)
    ap.add_argument("--enrich-limit", type=int, default=0, help="Max uncached chunks to enrich this run")
    ap.add_argument("--drop-junk", action="store_true",
                    help="With --enrich: drop chunks Claude marks as TOC/references/no clinical content")
    ap.add_argument("--selftest", action="store_true", help="Run built-in checks and exit")
    opts = ap.parse_args()

    if opts.selftest:
        sys.exit(selftest())

    search_paths = [DEFAULT_INCOMING]
    if DEFAULT_SAMPLES.exists():
        search_paths.append(DEFAULT_SAMPLES)
    if opts.attachments and Path(opts.attachments).exists():
        search_paths.append(Path(opts.attachments))
    for s in opts.src:
        search_paths.append(Path(s))

    files = collect_files(search_paths)
    print(f"Found {len(files)} candidate files")

    all_chunks: list[dict] = []
    report = []
    for f in files:
        try:
            chunks, info = ingest_file(f, opts)
            report.append({"file": f.name, "chunks": len(chunks), "ok": True, **info})
            all_chunks.extend(chunks)
            extra = []
            if info.get("pages_dropped"):
                extra.append(f"{info['pages_dropped']} front-matter pages dropped")
            if info.get("table_chunks"):
                extra.append(f"{info['table_chunks']} table-like chunks flagged (check doses)")
            if info.get("warning"):
                extra.append("WARNING: " + info["warning"])
            print(f"  OK  {f.name}: {len(chunks)} chunks" + (f"  [{'; '.join(extra)}]" if extra else ""))
        except Exception as e:
            report.append({"file": f.name, "chunks": 0, "ok": False, "error": str(e)})
            print(f"  ERR {f.name}: {e}")

    deduped, seen_hash = [], set()
    for c in all_chunks:
        h = hashlib.sha1(c["text"].encode("utf-8", errors="ignore")).hexdigest()[:16]
        if h in seen_hash:
            continue
        seen_hash.add(h)
        c = dict(c)
        c["hash"] = h
        deduped.append(c)

    if opts.enrich:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            print("--enrich needs the ANTHROPIC_API_KEY environment variable; skipping enrichment.")
        else:
            cache_path = Path(opts.out).with_name("enrich_cache.json")
            deduped = enrich_chunks(deduped, model=opts.enrich_model, key=key,
                                    limit=opts.enrich_limit, drop_junk=opts.drop_junk,
                                    cache_path=cache_path)

    out_path = Path(opts.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 2, "chunk_count": len(deduped), "chunks": deduped, "report": report}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {len(deduped)} chunks \u2192 {out_path}")

    # Compact file for the Worker. Text is NOT truncated: the chunker already
    # guarantees every chunk is <= MAX_CHARS.
    compact = []
    for c in deduped:
        item = {
            "id": c["id"],
            "title": c["title"][:160],
            "source": c["source"][:120],
            "type": c.get("type", "note"),
            "topics": c.get("topics", [])[:10],
            "text": c["text"],
        }
        for k in ("section", "page", "flags"):
            if c.get(k):
                item[k] = c[k]
        compact.append(item)
    compact_path = out_path.with_name(out_path.stem + "_compact.json")
    compact_path.write_text(
        json.dumps({"version": 2, "chunk_count": len(compact), "chunks": compact}, ensure_ascii=False),
        encoding="utf-8")
    print(f"Wrote compact {len(compact)} chunks \u2192 {compact_path}")

    if deduped:
        lens = [len(c["text"]) for c in deduped]
        print(f"Chunk size: avg {sum(lens) // len(lens)}, max {max(lens)} chars | "
              f"with section: {sum(1 for c in deduped if c.get('section'))} | "
              f"table-flagged: {sum(1 for c in deduped if 'table' in c.get('flags', []))}")


if __name__ == "__main__":
    main()
