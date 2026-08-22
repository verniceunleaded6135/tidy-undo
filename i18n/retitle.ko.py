#!/usr/bin/env python3
"""PDF 제목 추출 — 문서 안의 실제 제목을 뽑아 개명 후보를 만든다.

개명은 되돌리기 쉬운 작업이지만 사용자의 기억을 깨뜨린다. 그래서 이 도구는
'현재 이름이 내용을 얼마나 설명하는가'를 먼저 판정하고, 불투명한 것만
개명 후보로 올린다. 사람이 지은 이름은 목록으로만 제시한다.

제목은 세 경로로 찾는다.
  1. PDF 메타데이터 /Title — 있으면 가장 싸다. 다만 제작 도구가 넣은
     쓰레기값(파일경로, 'Microsoft Word - xxx.doc')이 많아 걸러야 한다.
  2. 첫 페이지에서 가장 큰 글씨 — 논문·보고서 표지의 제목은 거의 언제나
     본문보다 크다. 글자 크기 정보를 주는 pdfplumber 로 뽑는다.
  3. 첫 페이지 첫 줄들 — 위 둘이 실패했을 때의 마지막 수단.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

# 현재 이름이 이 꼴이면 내용을 전혀 설명하지 못한다 → 개명 후보
OPAQUE_RE = re.compile(
    r"^(?:"
    r"\d{4}\.\d{4,5}(v\d+)?"                 # arXiv ID  2508.09736v1
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]+"   # UUID
    r"|[0-9a-f]{16,}"                        # 긴 16진수
    r"|\d{9,}(_\d+)*"                        # SNS 숫자 파일명
    r"|(download|untitled|unnamed|document|doc|scan|file|무제|제목없음)"
    r"(\s*\(?\d+\)?)?"
    r"|ssrn[-_]?id\d+"
    r"|[0-9a-f]{6,}[-_](en|ko|fr|de|es)"     # OECD·기관 배포본  02f73362-en
    r"|\d{1,3}"                              # "1.pdf"
    r")$", re.I)

# PDF 메타데이터 /Title 에 흔히 들어가는 쓰레기값
BAD_TITLE_RE = re.compile(
    r"^(microsoft word|microsoft powerpoint|powerpoint 프레젠테이션|"
    r"untitled|무제|slide \d|hwp|한글|.*\.(doc|docx|hwp|hwpx|pptx?|indd|tex)$|"
    r"[a-z]:\\|/users/|/var/|print job|adobe)", re.I)

NOISE_LINE_RE = re.compile(
    r"^(?:\s*|\d+|page \d+|www\.|https?://|[-=_*·•]+|"
    r"arxiv:\S+|doi:\S+|issn.*|isbn.*)$", re.I)


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def clean(t: str) -> str:
    t = nfc(re.sub(r"\s+", " ", t)).strip(" .-–—_:;,\n\t")
    return t


def is_opaque(stem: str) -> bool:
    return bool(OPAQUE_RE.match(nfc(stem).strip()))


def usable(t: str | None) -> bool:
    if not t:
        return False
    t = clean(t)
    return (4 <= len(t) <= 200 and not BAD_TITLE_RE.match(t)
            and not t.startswith("/") and len(t.split()) >= 1)


def from_metadata(path: str) -> str | None:
    try:
        from pypdf import PdfReader
        md = PdfReader(path).metadata or {}
        return clean(md.get("/Title") or "")
    except Exception:                                     # noqa: BLE001
        return None


def from_largest_text(path: str) -> str | None:
    """첫 페이지에서 글자 크기가 가장 큰 줄. 표지 제목은 거의 언제나 가장 크다."""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                return None
            words = pdf.pages[0].extract_words(extra_attrs=["size"])
    except Exception:                                     # noqa: BLE001
        return None
    if not words:
        return None
    # 같은 줄(top 좌표가 비슷한 것)끼리 묶고, 줄의 최대 글자 크기를 기록
    lines: dict[int, list] = {}
    for w in words:
        key = round(w["top"] / 4)
        lines.setdefault(key, []).append(w)
    scored = []
    for key in sorted(lines):
        ws = sorted(lines[key], key=lambda x: x["x0"])
        # 여기서는 clean() 을 쓰지 않는다. clean() 이 줄 끝 하이픈을 지워버리면
        # 아래의 하이픈 이어붙이기가 영영 작동하지 않는다("A Pre-" → "A Pre").
        text = re.sub(r"\s+", " ", nfc(" ".join(w["text"] for w in ws))).strip()
        size = max(w.get("size", 0) for w in ws)
        if text and not NOISE_LINE_RE.match(text):
            scored.append((size, key, text))
    if not scored:
        return None
    top_size = max(s for s, _, _ in scored)
    # 제목이 두 줄로 나뉜 경우가 흔하므로 최대 크기에 가까운 연속 줄을 잇는다
    parts = [(k, t) for s, k, t in scored if s >= top_size * 0.88]
    parts.sort()
    merged, prev = [], None
    for k, t in parts:
        # 줄 묶음 키는 top/4 라서 통상 행간이 4~5 만큼 벌어진다. 허용치를
        # 3 으로 두면 두 줄짜리 제목이 첫 줄에서 끊긴다(실측 사례 있음).
        if prev is not None and k - prev > 6:
            break
        # 조판이 단어를 줄 끝에서 하이픈으로 끊는다("A Pre-" / "training").
        # 그대로 공백으로 이으면 단어가 깨지고 제목이 잘린 것처럼 보인다.
        if merged and merged[-1].endswith("-"):
            merged[-1] = merged[-1][:-1] + t
        else:
            merged.append(t)
        prev = k
    return clean(" ".join(merged)) or None


def from_first_lines(path: str, limit: int = 4) -> str | None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from extract import head
    t = head(path, 600) or ""
    for line in t.split("\n"):
        line = clean(line)
        if len(line) >= 8 and not NOISE_LINE_RE.match(line):
            return line
    return None


ARXIV_ID_RE = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")


def arxiv_titles(ids: list[str], batch: int = 50) -> dict[str, str]:
    """arXiv API 로 정본 제목을 받아온다. PDF 에서 뽑는 것보다 압도적으로 정확하다.

    표지에서 글자 크기로 제목을 추정하는 방식은 2단 조판·수식 폰트·자간 깨짐에
    쉽게 무너진다(실측: 79편 중 약 20편이 잘리거나 본문을 집었다). 파일명이
    arXiv ID 라면 출처에 직접 묻는 편이 옳다.

    id_list 로 한 번에 여러 개를 조회한다. arXiv 는 짧은 간격의 연속 요청을
    자제해 달라고 안내하므로 배치 사이에 간격을 둔다.
    """
    import time
    import urllib.parse
    import urllib.request

    out: dict[str, str] = {}
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        url = ("https://export.arxiv.org/api/query?max_results=%d&id_list=%s"
               % (len(chunk), urllib.parse.quote(",".join(chunk))))
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                xml = resp.read().decode("utf-8", "ignore")
        except Exception:                                  # noqa: BLE001
            continue                                       # 실패하면 추출로 폴백
        for m in re.finditer(
                r"<entry>.*?<id>[^<]*?abs/([\d.]+)v?\d*</id>.*?<title>(.*?)</title>",
                xml, re.S):
            out[m.group(1)] = clean(" ".join(m.group(2).split()))
        if i + batch < len(ids):
            time.sleep(3)
    return out


def _words(t: str) -> set[str]:
    return {w for w in re.split(r"[^0-9A-Za-z가-힣]+", nfc(t).lower())
            if len(w) >= 4}


def title_matches_body(title: str, path: str) -> bool:
    """제목이 이 PDF 의 것이 맞는지 첫 페이지 본문과 대조한다.

    외부에서 받아온 제목은 출처가 정확해도 '이 파일의' 제목이라는 보장이
    없다. 표지에 제목이 그대로 인쇄돼 있으므로, 제목의 특징적인 단어가
    첫 페이지에 실제로 나타나는지 보면 짝이 맞는지 알 수 있다.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from extract import head
    body = head(path, 1500) or ""
    if not body:
        return True                      # 본문을 못 읽으면 판단 보류 → 통과
    tw = _words(title)
    if not tw:
        return False
    bw = _words(body)
    hit = len(tw & bw) / len(tw)
    return hit >= 0.5


def arxiv_search(fragment: str, path: str) -> str | None:
    """제목 조각으로 arXiv 를 검색해 정본 제목을 찾는다.

    추출본이 잘렸을 때 쓴다. 찾은 제목도 반드시 본문과 대조한다 — 검색은
    비슷한 다른 논문을 1위로 올릴 수 있고, 그걸 그대로 쓰면 잘린 제목보다
    나쁜 결과(엉뚱한 논문 제목)가 된다.
    """
    import urllib.parse
    import urllib.request

    q = re.sub(r"[^0-9A-Za-z ]+", " ", fragment)
    q = " ".join(q.split()[:10])
    if len(q) < 8:
        return None
    url = ("https://export.arxiv.org/api/query?max_results=5&search_query="
           + urllib.parse.quote(f'ti:"{q}"'))
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            xml = resp.read().decode("utf-8", "ignore")
    except Exception:                                      # noqa: BLE001
        return None
    for m in re.finditer(r"<entry>.*?<title>(.*?)</title>", xml, re.S):
        cand = clean(" ".join(m.group(1).split()))
        if usable(cand) and title_matches_body(cand, path):
            return cand
    return None


def title_of(path: str) -> tuple[str | None, str]:
    for fn, how in ((from_metadata, "metadata"),
                    (from_largest_text, "largest_text"),
                    (from_first_lines, "first_lines")):
        t = fn(path)
        if usable(t):
            return clean(t), how
    return None, "none"


def safe_name(title: str, ext: str, maxlen: int = 90) -> str:
    """파일명으로 쓸 수 있게 다듬는다. 슬래시는 폴더를 쪼개므로 반드시 치환."""
    t = nfc(title).replace("/", "／").replace(":", "：").replace("\\", "＼")
    t = re.sub(r'[<>"|?*\x00-\x1f]', "", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if len(t) > maxlen:
        cut = t[:maxlen]
        t = cut[:cut.rfind(" ")] if " " in cut[maxlen // 2:] else cut
    return f"{t}.{ext}" if ext else t


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF 제목 추출 (읽기 전용)")
    ap.add_argument("inventory")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--only-opaque", action="store_true",
                    help="이름이 불투명한 파일만 (권장)")
    ap.add_argument("--ext", default="pdf")
    ap.add_argument("--no-network", action="store_true",
                    help="arXiv 조회 없이 PDF 내부에서만 추출")
    args = ap.parse_args()

    exts = {e.strip().lower() for e in args.ext.split(",")}
    rows, targets = [], []
    stats = {"대상": 0, "제목찾음": 0, "실패": 0, "이름동일": 0}
    with open(args.inventory, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("kind") != "file" or r.get("ext") not in exts:
                continue
            if r.get("dataless"):
                continue
            opaque = is_opaque(r["stem"])
            if args.only_opaque and not opaque:
                continue
            targets.append((r, opaque))

    # arXiv ID 인 것들은 먼저 한꺼번에 조회한다
    arx = {}
    if not args.no_network:
        ids = []
        for r, _ in targets:
            m = ARXIV_ID_RE.match(nfc(r["stem"]).strip())
            if m:
                ids.append(m.group(1))
        if ids:
            print(f"arXiv 조회 {len(ids)}건...", file=sys.stderr)
            arx = arxiv_titles(sorted(set(ids)))
            print(f"  응답 {len(arx)}건", file=sys.stderr)

    for r, opaque in targets:
            stats["대상"] += 1
            m = ARXIV_ID_RE.match(nfc(r["stem"]).strip())
            title = how = None
            if m and m.group(1) in arx:
                cand = arx[m.group(1)]
                # 파일명의 arXiv ID 가 내용과 다른 경우가 실제로 있다. 다른
                # 논문을 받아 이름만 그 ID 로 저장된 파일이 그렇다. 검증 없이
                # 믿으면 전혀 다른 제목을 붙이게 되므로, 본문과 대조한다.
                if title_matches_body(cand, r["path"]):
                    title, how = cand, "arxiv"
                else:
                    stats["ID불일치"] = stats.get("ID불일치", 0) + 1
            if title is None:
                title, how = title_of(r["path"])
                # 추출한 제목은 2단 조판·자간 깨짐 탓에 잘리기 쉽다. arXiv 논문임을
                # 아는 상황이라면 그 조각으로 역검색해 정본 제목을 되찾을 수 있다.
                if title and m and not args.no_network:
                    found = arxiv_search(title, r["path"])
                    if found:
                        title, how = found, "arxiv_search"
            if not title:
                stats["실패"] += 1
                continue
            new = safe_name(title, r["ext"])
            if nfc(new) == nfc(r["name"]):
                stats["이름동일"] += 1
                continue
            stats["제목찾음"] += 1
            rows.append({"path": r["path"], "current": nfc(r["name"]),
                         "title": title, "proposed": new, "method": how,
                         "opaque": opaque, "size": r["size"]})

    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
