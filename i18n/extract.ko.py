#!/usr/bin/env python3
"""텍스트 헤드 추출기 — 파일 앞부분 텍스트만 싸게 뽑는다.

내용 분류는 전체 파일의 3~4%에만 필요하다(파일명으로 95%가 판별된다).
그래서 이 모듈의 목표는 완전한 텍스트 추출이 아니라 '판단에 충분한 앞부분'을
최소 비용으로 얻는 것이다. 전문 추출은 각 포맷 전용 도구의 몫이다.

Spotlight(mdfind)는 HWP·HWPX를 전혀 색인하지 않는다(실측 0/144, 0/86).
한글 문서를 쓰는 환경에서 자체 추출기가 대체 불가능한 이유가 이것이다.
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
    """OOXML/OWPML 계열(docx·pptx·xlsx·hwpx)은 전부 ZIP 안의 XML이다."""
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
    from hwp_stdlib import extract_text          # 의존성 없는 자체 CFB 파서
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
    """앞부분 텍스트. 포맷 미지원이거나 실패하면 None."""
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
    ap = argparse.ArgumentParser(description="파일 앞부분 텍스트 추출")
    ap.add_argument("paths", nargs="*", help="대상 파일 (없으면 --from-inventory)")
    ap.add_argument("--from-inventory", help="scan.py JSONL에서 대상을 읽는다")
    ap.add_argument("--only", help="이 확장자만 (쉼표 구분)")
    ap.add_argument("--limit", type=int, default=1200, help="문자 수 상한")
    ap.add_argument("--jsonl", action="store_true", help="JSONL로 출력")
    args = ap.parse_args()

    paths = list(args.paths)
    if args.from_inventory:
        only = {e.strip().lstrip(".").lower()
                for e in (args.only or "").split(",") if e.strip()}
        with open(args.from_inventory, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                # dataless(iCloud 미다운로드) 파일을 열면 실제 다운로드가 시작된다
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
            print(f"--- {os.path.basename(p)}\n{t or '(추출 불가)'}\n")
    if args.jsonl:
        print(f"[{ok}/{len(paths)} 추출]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
