"""Batch-convert large PDFs in page-range chunks via server.convert_pdf.

Usage: uv run python batch_convert.py <pdf> [<pdf> ...]
Re-running skips ranges whose markdown and chapter index already exist.
"""

import sys
import time
from pathlib import Path

import pypdfium2 as pdfium

import server

CHUNK = 200  # pages per conversion; bitmaps for a chunk stay in RAM (~5 MB/page at 2x)


def _range_done(doc_dir: Path, stem: str, a: int, b: int) -> bool:
    return (doc_dir / f"{stem}_p{a}-{b}.md").exists() and (
        doc_dir / f"chapters_p{a}-{b}" / "_index.md"
    ).exists()


def main(paths: list[str]) -> None:
    t0 = time.monotonic()
    failures = []
    for p in paths:
        src = Path(p)
        n_pages = len(pdfium.PdfDocument(str(src)))
        doc_dir = server.OUTPUT_DIR / src.stem
        print(f"=== {src.name}: {n_pages} pages ===", flush=True)
        for a in range(1, n_pages + 1, CHUNK):
            b = min(a + CHUNK - 1, n_pages)
            if _range_done(doc_dir, src.stem, a, b):
                print(f"  p{a}-{b}: already done, skip", flush=True)
                continue
            try:
                r = server.convert_pdf(str(src), pages=f"{a}-{b}", split_by_chapter=True)
                print(
                    f"  p{a}-{b}: ok, {r['image_count']} images, {r['seconds']}s",
                    flush=True,
                )
            except Exception as e:  # keep going; failed ranges retry on rerun
                failures.append((src.name, f"p{a}-{b}", repr(e)))
                print(f"  p{a}-{b}: FAILED: {e!r}", flush=True)
    print(
        f"=== batch done in {round((time.monotonic() - t0) / 60, 1)} min, "
        f"{len(failures)} failed ranges ===",
        flush=True,
    )
    for name, rng, err in failures:
        print(f"  FAILED {name} {rng}: {err}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
