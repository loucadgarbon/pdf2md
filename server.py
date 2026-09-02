"""pdf2md MCP server: convert PDFs (PCIe/CXL specs etc.) to Markdown with docling.

Run with: uv run --directory <path-to-this-repo> server.py
Tools: convert_pdf, resplit, list_converted, get_conversion_info
Uses MCP Python SDK 2.x (MCPServer, formerly FastMCP).
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

import truststore
from mcp.server import MCPServer

# This network intercepts TLS; trust the Windows certificate store so
# docling's HuggingFace model downloads work.
truststore.inject_into_ssl()

# stdio transport: nothing may write to stdout except the MCP protocol.
logging.basicConfig(level=logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"

mcp = MCPServer("pdf2md")

_converters: dict[bool, object] = {}


def _get_converter(ocr: bool):
    """One DocumentConverter per OCR setting, models loaded once per process."""
    if ocr not in _converters:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            AcceleratorDevice,
            AcceleratorOptions,
            PdfPipelineOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_ocr = ocr
        opts.do_table_structure = True
        opts.generate_picture_images = True
        opts.images_scale = 2.0  # 144 DPI; the 72 DPI default is unreadable for spec figures
        opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.AUTO)
        _converters[ocr] = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
        )
    return _converters[ocr]


def _resolve_pdf(pdf_path: str) -> Path:
    p = Path(pdf_path)
    if not p.is_absolute():
        candidate = INPUT_DIR / p
        p = candidate if candidate.exists() else Path.cwd() / p
    if not p.exists():
        raise FileNotFoundError(f"PDF not found: {p} (drop files into {INPUT_DIR})")
    return p


def _parse_pages(pages: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", pages)
    if not m:
        raise ValueError(f'pages must look like "37" or "1-50", got: {pages!r}')
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if start < 1 or end < start:
        raise ValueError(f"invalid page range: {start}-{end}")
    return start, end


def _slug(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\- ]", "", text).strip().replace(" ", "-")
    return s[:max_len].rstrip("-") or "section"


_IMG_LINK = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)(\))")


def _rewrite_image_links(md: str, prefix: str = "") -> str:
    """Normalize image links to forward slashes and optionally re-anchor them.

    docling writes Windows backslash paths into markdown links, and chapter
    files live one directory deeper than the single-file export the links are
    relative to.
    """

    def sub(m: re.Match) -> str:
        path = m.group(2).replace("\\", "/")
        if path.startswith(("http:", "https:", "data:", "/", "#")) or re.match(r"^[A-Za-z]:", path):
            return m.group(0)
        return f"{m.group(1)}{prefix}{path}{m.group(3)}"

    return _IMG_LINK.sub(sub, md)


def _anchor_starts(markdown: str) -> list[int]:
    """Offsets of headings that start a real (sub)chapter.

    Two numbering styles: plain numeric ("3 Title", "3.2 Title") and Arm's
    letter-prefixed ("Chapter A1 Title", "Part A Title", "A1.1 Title").
    Depth is capped at 2 in both styles; "A1.2.1" stays inside "A1.2".
    """
    starts: set[int] = set()
    for m in re.finditer(r"(?m)^#{1,3} (\d+(?:\.\d+)*)\.?\s", markdown):
        if m.group(1).count(".") < 2:
            starts.add(m.start())
    for pat in (
        r"(?m)^#{1,3} (?:Chapter|Appendix) [A-Z]\d*\s",
        r"(?m)^#{1,3} Part [A-Z]\s",
        r"(?m)^#{1,3} [A-Z]\d+\.\d+ ",
    ):
        starts.update(m.start() for m in re.finditer(pat, markdown))

    # TOC / chapter-summary pages repeat the real headings before the body
    # (docling promotes them to headings too); keep only the last occurrence
    # of each title so those references merge away instead of splitting.
    last_by_title: dict[str, int] = {}
    for s in sorted(starts):
        eol = markdown.find("\n", s)
        title = markdown[s : eol if eol != -1 else len(markdown)].lstrip("# ").strip().lower()
        last_by_title[title] = s
    return sorted(last_by_title.values())


# Chunks with less body than this merge into the previous chunk. Docling
# promotes TOC entries of Arm specs to headings; without merging, every TOC
# line would become its own near-empty chapter file.
_MIN_CHUNK_BODY = 200


def _merge_small_chunks(chunks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    for title, content in chunks:
        body = "\n".join(content.splitlines()[1:]).strip()
        if merged and len(body) < _MIN_CHUNK_BODY:
            prev_title, prev_content = merged[-1]
            merged[-1] = (prev_title, prev_content + content)
        else:
            merged.append((title, content))
    return merged


def _split_by_chapter(markdown: str) -> list[tuple[str, str]]:
    """Split at chapter/section headings of depth <= 2 (numeric or Arm style).

    Docling emits table/figure captions and page-header artifacts as headings
    too, so splitting on heading level alone over-fragments spec PDFs;
    anchoring on section numbers keeps one file per real (sub)chapter. Falls
    back to heading-level splitting for documents without numbered sections.
    """
    starts = _anchor_starts(markdown)
    if len(starts) < 2:
        return _merge_small_chunks(_split_by_heading_level(markdown))

    chunks: list[tuple[str, str]] = []
    if starts[0] > 0 and markdown[: starts[0]].strip():
        chunks.append(("front-matter", markdown[: starts[0]]))
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(markdown)
        chunk = markdown[start:end]
        lines = chunk.splitlines()
        title = lines[0].lstrip("# ").strip()
        # A chapter title split across a page break leaves a bare number
        # heading ("2.0"); pull the title from the next heading.
        if re.fullmatch(r"\d+(\.\d+)*\.?", title):
            nxt = next((ln for ln in lines[1:] if ln.startswith("#")), None)
            if nxt:
                title = f"{title} {nxt.lstrip('# ').strip()}"
        chunks.append((title, chunk))
    return _merge_small_chunks(chunks)


def _split_by_heading_level(markdown: str) -> list[tuple[str, str]]:
    """Split markdown at the shallowest heading level that occurs at least twice.

    Returns (title, content) pairs; a leading chunk before the first heading
    becomes ("front-matter", ...).
    """
    headings = re.findall(r"(?m)^(#{1,3}) (.+)$", markdown)
    split_level = None
    for level in (1, 2, 3):
        if sum(1 for h, _ in headings if len(h) == level) >= 2:
            split_level = level
            break
    if split_level is None:
        return [("document", markdown)]

    pattern = re.compile(rf"(?m)^#{{{split_level}}} (?!#)")
    starts = [m.start() for m in pattern.finditer(markdown)]
    chunks: list[tuple[str, str]] = []
    if starts and starts[0] > 0 and markdown[: starts[0]].strip():
        chunks.append(("front-matter", markdown[: starts[0]]))
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(markdown)
        chunk = markdown[start:end]
        title = chunk.splitlines()[0].lstrip("# ").strip()
        chunks.append((title, chunk))
    return chunks


def _write_chapters(markdown: str, doc_dir: Path, suffix: str, stem: str) -> list[str]:
    """Rebuild chapters<suffix>/ from a single-file export; returns written paths."""
    chapter_dir = doc_dir / f"chapters{suffix}"
    if chapter_dir.exists():
        shutil.rmtree(chapter_dir)
    chapter_dir.mkdir()
    written: list[str] = []
    index_lines = [f"# {stem}{suffix} — chapter index", ""]
    for i, (title, content) in enumerate(_split_by_chapter(markdown), 1):
        fname = f"{i:02d}-{_slug(title)}.md"
        # Chapter files sit one directory deeper than the links are relative to.
        (chapter_dir / fname).write_text(
            _rewrite_image_links(content, "../"), encoding="utf-8"
        )
        written.append(str(chapter_dir / fname))
        safe_title = re.sub(r"([\[\]])", r"\\\1", title)
        index_lines.append(f"- [{safe_title}]({fname})")
    index_path = chapter_dir / "_index.md"
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    written.append(str(index_path))
    return written


@mcp.tool()
def convert_pdf(
    pdf_path: str,
    pages: str | None = None,
    split_by_chapter: bool = False,
    ocr: bool = False,
) -> dict:
    """Convert a PDF to Markdown with docling (GPU-accelerated when available).

    Args:
        pdf_path: Absolute path, or a filename inside the input/ folder.
        pages: Optional 1-based page range like "1-50" or "37". For very large
            specs (1000+ pages) convert in segments: it keeps calls short, and
            figure extraction holds every page bitmap in RAM (~5 MB/page at
            2x scale), so a full 1000-page run needs 5+ GB.
        split_by_chapter: Additionally write one file per top-level chapter
            plus an _index.md. Recommended for specs.
        ocr: Enable OCR for scanned PDFs. Leave off for digital-text PDFs
            (much faster).

    First call downloads docling models (a few hundred MB) — it is slow once.
    Output goes to output/<pdf-name>/. Figures are extracted as PNG files into
    output/<pdf-name>/artifacts<range>/ and referenced from the markdown —
    read a PNG directly to view a figure.
    """
    src = _resolve_pdf(pdf_path)
    page_range = _parse_pages(pages) if pages else None

    t0 = time.monotonic()
    converter = _get_converter(ocr)
    kwargs = {"page_range": page_range} if page_range else {}
    result = converter.convert(str(src), **kwargs)

    from docling.datamodel.base_models import ConversionStatus
    from docling_core.types.doc import ImageRefMode  # pulls pandas; keep startup fast

    if result.status != ConversionStatus.SUCCESS:
        # PARTIAL_SUCCESS (e.g. page timeouts) must not silently overwrite a
        # good previous run with truncated output.
        errors = "; ".join(e.error_message for e in result.errors[:3])
        raise RuntimeError(
            f"conversion did not fully succeed ({result.status.value}): "
            f"{errors or 'no error detail'}"
        )

    doc_dir = OUTPUT_DIR / src.stem
    doc_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_p{page_range[0]}-{page_range[1]}" if page_range else ""

    artifacts_dir = doc_dir / f"artifacts{suffix}"
    if artifacts_dir.exists():
        shutil.rmtree(artifacts_dir)
    md_path = doc_dir / f"{src.stem}{suffix}.md"
    # artifacts_dir must stay relative: an absolute path bakes C:\ paths into the links.
    result.document.save_as_markdown(
        md_path,
        artifacts_dir=Path(f"artifacts{suffix}"),
        image_mode=ImageRefMode.REFERENCED,
    )
    markdown = _rewrite_image_links(md_path.read_text(encoding="utf-8"))
    md_path.write_text(markdown, encoding="utf-8")
    elapsed = round(time.monotonic() - t0, 1)
    image_count = len(list(artifacts_dir.glob("*.png"))) if artifacts_dir.exists() else 0
    if image_count == 0 and artifacts_dir.exists():
        artifacts_dir.rmdir()  # docling creates it even for figure-less PDFs

    # Clean stale chapters unconditionally: after regeneration their image
    # links (and content) would silently contradict the new artifacts.
    chapter_dir = doc_dir / f"chapters{suffix}"
    if chapter_dir.exists():
        shutil.rmtree(chapter_dir)

    written: list[str] = [str(md_path)]
    if split_by_chapter:
        written += _write_chapters(markdown, doc_dir, suffix, src.stem)

    return {
        "source": str(src),
        "pages_converted": len(result.document.pages),
        "seconds": elapsed,
        "image_count": image_count,
        "images_dir": str(artifacts_dir) if image_count else None,
        "output_files": written,
    }


@mcp.tool()
def resplit(name: str) -> dict:
    """Re-run chapter splitting for an already-converted document.

    Rebuilds chapters<range>/ from the existing single-file markdown exports —
    no PDF re-conversion, so it is fast. Use after splitting rules improve.
    Use list_converted first to see available names.
    """
    doc_dir = OUTPUT_DIR / name
    if not doc_dir.is_dir():
        raise FileNotFoundError(f"No converted document named {name!r} in {OUTPUT_DIR}")
    results: dict[str, int] = {}
    for md_path in sorted(doc_dir.glob("*.md")):
        if not md_path.stem.startswith(name):
            continue
        suffix = md_path.stem[len(name):]
        if not re.fullmatch(r"(_p\d+-\d+)?", suffix):
            continue
        markdown = md_path.read_text(encoding="utf-8")
        written = _write_chapters(markdown, doc_dir, suffix, name)
        results[md_path.name] = len(written) - 1  # excluding _index.md
    if not results:
        raise FileNotFoundError(f"No top-level markdown exports found in {doc_dir}")
    return {"document": name, "chapter_files_per_range": results}


@mcp.tool()
def list_converted() -> dict:
    """List already-converted documents under output/ with their files."""
    docs = {}
    if OUTPUT_DIR.exists():
        for doc_dir in sorted(d for d in OUTPUT_DIR.iterdir() if d.is_dir()):
            files = sorted(str(f.relative_to(doc_dir)) for f in doc_dir.rglob("*.md"))
            docs[doc_dir.name] = files
    return {"output_dir": str(OUTPUT_DIR), "documents": docs}


@mcp.tool()
def get_conversion_info(name: str) -> dict:
    """Return the chapter index for a converted document (by output/ folder name).

    Use list_converted first to see available names.
    """
    doc_dir = OUTPUT_DIR / name
    if not doc_dir.is_dir():
        raise FileNotFoundError(f"No converted document named {name!r} in {OUTPUT_DIR}")
    indexes = {
        str(p.parent.relative_to(doc_dir)): p.read_text(encoding="utf-8")
        for p in doc_dir.rglob("_index.md")
    }
    files = {
        str(f.relative_to(doc_dir)): f.stat().st_size for f in sorted(doc_dir.rglob("*.md"))
    }
    images = {
        d.name: len(list(d.glob("*.png")))
        for d in sorted(doc_dir.glob("artifacts*"))
        if d.is_dir()
    }
    return {
        "document": name,
        "path": str(doc_dir),
        "files": files,
        "images": images,
        "indexes": indexes,
    }


if __name__ == "__main__":
    mcp.run()
