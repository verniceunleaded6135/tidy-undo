#!/usr/bin/env python3
"""Text head extractor — pulls only the leading text of a file, cheaply.

Content-based classification is needed for just 3-4% of all files (filenames
settle the other 95%). So the goal here is not complete text extraction but
"enough of the beginning to decide", at minimum cost. Full extraction is the
job of each format's dedicated tool.

Spotlight (mdfind) does not index HWP/HWPX at all (measured: 0/144, 0/86).
That is why an in-house extractor is irreplaceable in an environment that
uses Hangul documents.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TAG = re.compile(r"<[^>]+>")
PARA = re.compile(r"</a:p>|</w:p>|</hp:p>|</p>")
WS = re.compile(r"[ \t ]+")


def _zip_xml(path: str, patterns: list[str], limit: int) -> str:
    """The OOXML/OWPML family (docx, pptx, xlsx, hwpx) is all XML inside a ZIP."""
    out: list[str] = []
    total = 0
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for pat in patterns:
            for n in sorted(x for x in names if re.fullmatch(pat, x)):
                raw = z.read(n).decode("utf-8", "ignore")
                txt = TAG.sub(" ", PARA.sub("\n", raw))
                out.append(txt)
                total += len(txt)
                if total > limit * 4:
                    break
            if total > limit * 4:
                break
    return WS.sub(" ", " ".join(out))


def _pdf(path: str, limit: int) -> str:
    from pypdf import PdfReader
    s = ""
    for page in PdfReader(path).pages[:3]:
        s += page.extract_text() or ""
        if len(s) > limit:
            break
    return s


def _hwp(path: str, limit: int) -> str:
    from hwp_stdlib import extract_text          # in-house CFB parser, no deps
    return extract_text(path, 1) or ""


def _plain(path: str, limit: int) -> str:
    with open(path, "rb") as f:
        return f.read(limit * 3).decode("utf-8", "ignore")


HANDLERS = {
    ".pdf": _pdf, ".hwp": _hwp,
    ".hwpx": lambda p, n: _zip_xml(p, [r"Contents/section\d+\.xml"], n),
    ".pptx": lambda p, n: _zip_xml(p, [r"ppt/slides/slide\d+\.xml"], n),
    ".docx": lambda p, n: _zip_xml(p, [r"word/document\.xml"], n),
    ".xlsx": lambda p, n: _zip_xml(p, [r"xl/sharedStrings\.xml"], n),
    ".md": _plain, ".txt": _plain, ".csv": _plain, ".json": _plain,
    ".html": _plain, ".py": _plain, ".sh": _plain, ".yaml": _plain,
    ".yml": _plain, ".xml": _plain, ".tsv": _plain,
}


def head(path: str, limit: int = 1200) -> str | None:
    """Leading text. None if the format is unsupported or extraction fails."""
    fn = HANDLERS.get(os.path.splitext(path)[1].lower())
    if fn is None:
        return None
    try:
        text = fn(path, limit) or ""
    except Exception:                              # noqa: BLE001
        return None
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text[:limit] or None


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract the leading text of a file")
    ap.add_argument("paths", nargs="*", help="target files (omit to use --from-inventory)")
    ap.add_argument("--from-inventory", help="read targets from a scan.py JSONL")
    ap.add_argument("--only", help="only these extensions (comma-separated)")
    ap.add_argument("--limit", type=int, default=1200, help="character cap")
    ap.add_argument("--jsonl", action="store_true", help="emit JSONL")
    args = ap.parse_args()

    paths = list(args.paths)
    if args.from_inventory:
        only = {e.strip().lstrip(".").lower()
                for e in (args.only or "").split(",") if e.strip()}
        with open(args.from_inventory, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                # Opening a dataless (not-yet-downloaded iCloud) file kicks off
                # an actual download.
                if r.get("kind") != "file" or r.get("dataless"):
                    continue
                if only and r.get("ext") not in only:
                    continue
                paths.append(r["path"])

    ok = 0
    for p in paths:
        t = head(p, args.limit)
        if t:
            ok += 1
        if args.jsonl:
            print(json.dumps({"path": p, "text": t}, ensure_ascii=False))
        else:
            print(f"--- {os.path.basename(p)}\n{t or '(extraction failed)'}\n")
    if args.jsonl:
        print(f"[{ok}/{len(paths)} extracted]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
