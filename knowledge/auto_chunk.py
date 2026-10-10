#!/usr/bin/env python3
"""
NurseVault knowledge auto-chunker
---------------------------------
Ingests PDFs, JSON cards, and Markdown notes from knowledge/incoming/
(and optional extra paths), produces a unified chunks JSON for RAG.

Usage:
  python3 auto_chunk.py
  python3 auto_chunk.py --src /path/to/pdfs --out chunks/kb_all.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
DEFAULT_INCOMING = ROOT / "incoming"
DEFAULT_SAMPLES = ROOT / "samples"
# Public kb.json at repo root so Cloudflare can serve it as /kb.json
DEFAULT_OUT = REPO_ROOT / "kb.json"

# ---- helpers ----

def slug(s: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:max_len] or "item"


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p and len(p.strip()) > 40]


def chunk_long_text(
    text: str,
    *,
    source: str,
    title: str,
    doc_type: str,
    topics: list[str] | None = None,
    id_prefix: str,
    target_chars: int = 1200,
    max_chars: int = 1800,
) -> list[dict]:
    """Merge paragraphs into ~target_chars chunks with light overlap."""
    paras = split_paragraphs(text)
    if not paras:
        # hard split
        paras = [text[i : i + target_chars] for i in range(0, len(text), target_chars)]

    chunks: list[dict] = []
    buf = ""
    idx = 0

    def flush(buf_text: str):
        nonlocal idx
        buf_text = buf_text.strip()
        if len(buf_text) < 80:
            return
        idx += 1
        # derive topic hints from text
        auto_topics = list(topics or [])
        lower = buf_text.lower()
        keyword_map = {
            "diabetes": ["diabetes", "insulin", "glucose", "hypoglycaemia", "hypoglycemia"],
            "hypertension": ["hypertension", "blood pressure", "antihypertensive"],
            "anaphylaxis": ["anaphylaxis", "adrenaline", "epinephrine"],
            "tb": ["tuberculosis", " tb ", "rifampicin", "isoniazid"],
            "hiv": [" hiv", "antiretroviral", "art "],
            "obstetric": ["pregnan", "labour", "labor", "postpartum", "eclampsia", "pph"],
            "pharmacology": ["dose", "side effect", "contraindication", "mechanism"],
            "infection control": ["infection prevention", "hand hygiene", "ppe ", "sterile"],
        }
        for topic, keys in keyword_map.items():
            if any(k in lower for k in keys) and topic not in auto_topics:
                auto_topics.append(topic)

        chunks.append(
            {
                "id": f"{id_prefix}-{idx:04d}",
                "title": f"{title} – part {idx}" if idx > 1 else title,
                "source": source,
                "type": doc_type,
                "topics": auto_topics[:12],
                "text": buf_text[:max_chars],
            }
        )

    for p in paras:
        if not buf:
            buf = p
        elif len(buf) + len(p) + 2 <= target_chars:
            buf = buf + "\n\n" + p
        else:
            flush(buf)
            # light overlap: last 200 chars of previous
            overlap = buf[-200:] if len(buf) > 200 else ""
            buf = (overlap + "\n\n" + p).strip() if overlap else p
    if buf:
        flush(buf)

    return chunks


# ---- ingest by type ----

def ingest_json(path: Path) -> list[dict]:
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
        cid = item.get("id") or f"{slug(path.stem)}-{i+1:03d}"
        if len(text) <= 1800:
            out.append(
                {
                    "id": cid,
                    "title": title,
                    "source": source,
                    "type": doc_type,
                    "topics": topics,
                    "text": text,
                }
            )
        else:
            out.extend(
                chunk_long_text(
                    text,
                    source=source,
                    title=title,
                    doc_type=doc_type,
                    topics=topics,
                    id_prefix=cid,
                )
            )
    return out


def ingest_markdown(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    # title from first H1
    title_m = re.search(r"^#\s+(.+)$", raw, re.M)
    title = title_m.group(1).strip() if title_m else path.stem.replace("_", " ")
    source_m = re.search(r"^Source:\s*(.+)$", raw, re.M | re.I)
    source = source_m.group(1).strip() if source_m else f"Notes:{path.name}"
    topics_m = re.search(r"^Topics:\s*(.+)$", raw, re.M | re.I)
    topics = []
    if topics_m:
        topics = [t.strip() for t in re.split(r"[,;]", topics_m.group(1)) if t.strip()]

    # strip meta lines
    body = re.sub(r"^#\s+.+$", "", raw, count=1, flags=re.M)
    body = re.sub(r"^Source:\s*.+$", "", body, flags=re.M | re.I)
    body = re.sub(r"^Topics:\s*.+$", "", body, flags=re.M | re.I)
    body = clean_text(body)
    return chunk_long_text(
        body,
        source=source,
        title=title,
        doc_type="note",
        topics=topics,
        id_prefix=slug(path.stem),
    )


def extract_pdf_text(path: Path) -> str:
    import subprocess
    # Prefer pdftotext (fast). Fallback to pypdf.
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True,
            timeout=90,
        )
        if r.returncode == 0 and r.stdout:
            return clean_text(r.stdout.decode("utf-8", errors="ignore"))
    except Exception:
        pass
    if PdfReader is None:
        raise RuntimeError("No PDF extractor available")
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            pages.append(t)
    return clean_text("\n\n".join(pages))


def ingest_pdf(path: Path) -> list[dict]:
    text = extract_pdf_text(path)
    if len(text) < 100:
        return []
    # drop very common front-matter noise
    text = re.sub(
        r"(table of contents|acknowledgements|foreword|list of abbreviations)[\s\S]{0,2000}",
        " ",
        text,
        count=1,
        flags=re.I,
    )
    title = path.stem.replace("_", " ").replace("-", " ")
    title = re.sub(r"\s+", " ", title).strip()
    source = f"PDF:{path.name}"
    return chunk_long_text(
        text,
        source=source,
        title=title,
        doc_type="guideline",
        topics=[],
        id_prefix=slug(path.stem),
        target_chars=1100,
        max_chars=1700,
    )


def ingest_file(path: Path) -> list[dict]:
    suf = path.suffix.lower()
    if suf == ".json":
        return ingest_json(path)
    if suf in (".md", ".txt"):
        return ingest_markdown(path)
    if suf == ".pdf":
        return ingest_pdf(path)
    return []


def collect_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.suffix.lower() in {".pdf", ".json", ".md", ".txt"} and f.is_file():
                    files.append(f)
    # unique
    seen = set()
    out = []
    for f in files:
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser(description="Auto-chunk knowledge files for Vault AI RAG")
    ap.add_argument(
        "--src",
        nargs="*",
        default=[],
        help="Extra source files or directories",
    )
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="Output JSON path")
    ap.add_argument(
        "--attachments",
        default="",
        help="Optional extra directory of PDFs (leave empty in GitHub Actions)",
    )
    args = ap.parse_args()

    search_paths = [DEFAULT_INCOMING]
    if DEFAULT_SAMPLES.exists():
        search_paths.append(DEFAULT_SAMPLES)
    if args.attachments and Path(args.attachments).exists():
        search_paths.append(Path(args.attachments))
    for s in args.src:
        search_paths.append(Path(s))

    files = collect_files(search_paths)
    print(f"Found {len(files)} candidate files")

    all_chunks: list[dict] = []
    report = []

    for f in files:
        try:
            chunks = ingest_file(f)
            report.append({"file": f.name, "chunks": len(chunks), "ok": True})
            all_chunks.extend(chunks)
            print(f"  OK  {f.name}: {len(chunks)} chunks")
        except Exception as e:
            report.append({"file": f.name, "chunks": 0, "ok": False, "error": str(e)})
            print(f"  ERR {f.name}: {e}")

    # dedupe by hash of text
    deduped = []
    seen_hash = set()
    for c in all_chunks:
        h = hashlib.sha1(c["text"].encode("utf-8", errors="ignore")).hexdigest()[:16]
        if h in seen_hash:
            continue
        seen_hash.add(h)
        c = dict(c)
        c["hash"] = h
        deduped.append(c)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "chunk_count": len(deduped),
        "chunks": deduped,
        "report": report,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {len(deduped)} chunks → {out_path}")

    # also write a compact version for Worker embedding (cap text)
    compact = []
    for c in deduped:
        compact.append(
            {
                "id": c["id"],
                "title": c["title"][:120],
                "source": c["source"][:120],
                "type": c.get("type", "note"),
                "topics": c.get("topics", [])[:10],
                "text": c["text"][:1600],
            }
        )
    compact_path = out_path.with_name(out_path.stem + "_compact.json")
    compact_path.write_text(
        json.dumps({"version": 1, "chunk_count": len(compact), "chunks": compact}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Wrote compact {len(compact)} chunks → {compact_path}")


if __name__ == "__main__":
    main()
