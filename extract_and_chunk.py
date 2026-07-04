"""
Jubilee AI Agent -- Document Ingestion Pipeline (MVP)
======================================================

Turns raw product documents (PPTX, PDF, converted PPT) into metadata-tagged
chunks ready for embedding, and optionally pushes them into a vector store.

WHY THIS SHAPE:
  Insurance product docs mix marketing decks (lower authority) with legal
  Key Facts Documents / policy wording (higher authority), and the same
  fact (e.g. minimum premium) can genuinely conflict between them. This
  pipeline tags every chunk with its source file and a configurable
  authority tier, so conflicts get resolved deliberately rather than left
  to whichever chunk vector search happens to surface first.

USAGE:
  1. Drop new files into ./raw_docs/
  2. Fill in FILE_CONFIG below (product_id, doc_type, per file)
  3. Run: python extract_and_chunk.py
  4. Review chunks.json, then run embed_and_upsert() once DEEPSEEK_API_KEY
     and your vector DB are configured.

DEPENDENCIES:
  pip install pdfplumber requests --break-system-packages
  extract-text CLI (bundled with the pptx skill) must be on PATH for pptx
  soffice (LibreOffice) must be on PATH to convert legacy .ppt -> .pptx
"""

import json
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG -- edit this per batch of documents you ingest
# ---------------------------------------------------------------------------

RAW_DOCS_DIR = Path("./raw_docs")
OUTPUT_CHUNKS_PATH = Path("./chunks.json")

# Authority tiers: lower number wins when two chunks conflict on a fact.
# Adjust this mapping once Jubilee confirms their real document hierarchy
# (typically: policy wording / KFD > sales handbook > raw marketing deck).
SOURCE_PRIORITY = {
    "KFD_legal": 1,
    "policy_wording": 1,
    "sales_handbook": 2,
    "product_calc_deck": 2,
    "marketing_deck": 3,
    "unclassified": 9,
}

# Map each raw filename (or a regex) to product_id + doc_type.
# This is the one manual step per new document -- takes a minute, and is
# what makes retrieval filterable by product later.
FILE_CONFIG = {
    # "example_new_product_KFD.pdf": {"product_id": "example_plan", "doc_type": "KFD_legal"},
    # "example_handbook.pptx":        {"product_id": None, "doc_type": "sales_handbook"},
    #     ^ product_id=None means the file covers multiple products; the
    #       chunker will tag chunks generically and you assign product_id
    #       per-chunk afterward (see review_and_tag step below).
}


# ---------------------------------------------------------------------------
# EXTRACTION
# ---------------------------------------------------------------------------

def extract_pptx_text(path: Path) -> str:
    """Uses the extract-text CLI (from the pptx skill) -- one '## Slide N' per slide."""
    result = subprocess.run(["extract-text", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"extract-text failed for {path}: {result.stderr}")
    return result.stdout


def convert_legacy_ppt(path: Path, out_dir: Path) -> Path:
    """Converts old binary .ppt to .pptx via LibreOffice so extract-text can read it."""
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "pptx", str(path), "--outdir", str(out_dir)],
        check=True, capture_output=True,
    )
    return out_dir / (path.stem + ".pptx")


def extract_pdf_text_and_tables(path: Path) -> str:
    """
    Extracts PDF text page by page, preserving tables as markdown-ish rows
    rather than flattening them into prose -- critical for premium/surrender
    tables where column alignment carries the meaning.
    """
    import pdfplumber

    out = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            out.append(f"## Page {i}\n")
            text = page.extract_text() or ""
            out.append(text)
            for table in page.extract_tables():
                out.append("\n[TABLE]")
                for row in table:
                    out.append(" | ".join(cell or "" for cell in row))
                out.append("[/TABLE]\n")
    return "\n".join(out)


def extract_document(path: Path, scratch_dir: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pptx":
        return extract_pptx_text(path)
    if suffix == ".ppt":
        converted = convert_legacy_ppt(path, scratch_dir)
        return extract_pptx_text(converted)
    if suffix == ".pdf":
        return extract_pdf_text_and_tables(path)
    raise ValueError(f"Unsupported file type: {suffix} ({path.name})")


# ---------------------------------------------------------------------------
# CHUNKING
# ---------------------------------------------------------------------------

SECTION_HEADING_RE = re.compile(r"^##\s+(Slide|Page)\s+\d+", re.MULTILINE)


def chunk_by_section(raw_text: str, filename: str, product_id, doc_type: str):
    """
    Splits on slide/page headings rather than fixed token windows, so a
    benefit table or exclusion clause stays intact in one chunk instead of
    being cut mid-sentence or mid-row.

    For production, consider a second pass that further splits any single
    slide/page exceeding ~500 tokens into sub-sections by blank-line
    paragraph breaks, and a pass that merges near-empty chunks (e.g. a
    title-only slide) into the following chunk.
    """
    matches = list(SECTION_HEADING_RE.finditer(raw_text))
    sections = []  # (heading_text, body_text) pairs, position-aligned
    for i, m in enumerate(matches):
        heading = m.group(0)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
        body = raw_text[start:end].strip()
        sections.append((heading, body))
    if not matches:
        # no headings found at all -- treat whole doc as one section
        sections = [("full_document", raw_text.strip())]

    chunks = []
    priority = SOURCE_PRIORITY.get(doc_type, 9)
    for idx, (heading, body) in enumerate(sections):
        if len(body) < 15:  # skip near-empty slides (title-only, etc.)
            continue
        chunks.append({
            "chunk_id": f"{Path(filename).stem}_{idx:03d}",
            "product_id": product_id,  # None if file covers multiple products -- tag manually after review
            "section_label": heading.replace("## ", ""),
            "text": body,
            "source_file": filename,
            "doc_type": doc_type,
            "source_priority": priority,
            "tags": [],  # fill in during review pass (see below) -- e.g. ["benefits", "riders"]
        })
    return chunks


# ---------------------------------------------------------------------------
# REVIEW STEP (manual, but the pipeline flags where it's needed)
# ---------------------------------------------------------------------------

def flag_chunks_needing_review(chunks):
    """
    Auto-flags chunks likely to need a human pass before embedding:
    - product_id missing (multi-product source file)
    - contains a number that looks like a premium/amount, so it's worth
      cross-checking against other sources for conflicts
    """
    amount_re = re.compile(r"(ushs|shs|kes|ugx)\s?[\d,]+", re.IGNORECASE)
    for c in chunks:
        reasons = []
        if c["product_id"] is None:
            reasons.append("no product_id assigned -- tag manually")
        if amount_re.search(c["text"]):
            reasons.append("contains a monetary figure -- cross-check against other sources for conflicts")
        c["needs_review"] = reasons
    return chunks


# ---------------------------------------------------------------------------
# EMBEDDING + VECTOR STORE (template -- fill in credentials to activate)
# ---------------------------------------------------------------------------

def embed_and_upsert(chunks, deepseek_api_key: str = None, qdrant_url: str = None):
    """
    Template for the embedding step. DeepSeek's API is OpenAI-compatible;
    swap base_url/model if you later move providers -- the rest of this
    pipeline doesn't change.

    NOTE: at time of writing, confirm DeepSeek's current embeddings-model
    name and endpoint from their docs before wiring this up for real --
    don't assume the values below without a doc check.
    """
    if not deepseek_api_key:
        print("No DEEPSEEK_API_KEY provided -- skipping embedding step. "
              "Chunks are saved to chunks.json for manual inspection.")
        return

    import requests

    for c in chunks:
        resp = requests.post(
            "https://api.deepseek.com/v1/embeddings",  # verify against current docs before use
            headers={"Authorization": f"Bearer {deepseek_api_key}"},
            json={"model": "deepseek-embedding", "input": c["text"]},  # verify model name
        )
        resp.raise_for_status()
        c["embedding"] = resp.json()["data"][0]["embedding"]

    if qdrant_url:
        # from qdrant_client import QdrantClient
        # client = QdrantClient(url=qdrant_url)
        # client.upsert(collection_name="jubilee_products", points=[
        #     {"id": c["chunk_id"], "vector": c["embedding"], "payload": {
        #         k: v for k, v in c.items() if k != "embedding"
        #     }} for c in chunks
        # ])
        print("Qdrant upsert stubbed -- uncomment and configure once qdrant-client is installed.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    if not RAW_DOCS_DIR.exists():
        print(f"Create {RAW_DOCS_DIR} and drop new product documents into it, then re-run.")
        sys.exit(1)

    scratch_dir = Path("./_scratch")
    scratch_dir.mkdir(exist_ok=True)

    all_chunks = []
    for path in sorted(RAW_DOCS_DIR.iterdir()):
        if path.name not in FILE_CONFIG:
            print(f"SKIP {path.name} -- not in FILE_CONFIG, add an entry to classify it")
            continue
        cfg = FILE_CONFIG[path.name]
        print(f"Extracting {path.name} ...")
        raw_text = extract_document(path, scratch_dir)
        chunks = chunk_by_section(raw_text, path.name, cfg["product_id"], cfg["doc_type"])
        all_chunks.extend(chunks)
        print(f"  -> {len(chunks)} chunks")

    all_chunks = flag_chunks_needing_review(all_chunks)

    with open(OUTPUT_CHUNKS_PATH, "w") as f:
        json.dump(all_chunks, f, indent=2, ensure_ascii=False)

    needing_review = [c for c in all_chunks if c["needs_review"]]
    print(f"\nWrote {len(all_chunks)} chunks to {OUTPUT_CHUNKS_PATH}")
    print(f"{len(needing_review)} chunks flagged for manual review before embedding.")

    # Once chunks.json looks right:
    # embed_and_upsert(all_chunks, deepseek_api_key="...", qdrant_url="http://localhost:6333")


if __name__ == "__main__":
    main()
