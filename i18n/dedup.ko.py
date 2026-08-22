#!/usr/bin/env python3
"""중복·버전계보 탐지 — 인벤토리 JSONL을 받아 관계를 찾는다.

세 가지를 낸다:
  1. exact   : 바이트 단위 완전중복 그룹
  2. folder  : 폴더 단위 복제쌍 (A의 파일이 B에 얼마나 포함되는가)
  3. version : 같은 원본의 버전 계보 클러스터 (v3/v7/최종/3교본/(1))

세 단계 해싱(크기 → 앞 64KB → 전문)으로 전문 해시 계산을 최소화한다.
파일을 절대 수정하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

# --- 버전 표기 제거 규칙 (한국어 사용자 관습 포함) -------------------------
# 순서 중요: 긴 패턴부터 지운다.
# 한국어 토큰은 공백/밑줄/하이픈으로만 끊긴다. 단어 중간을 자르면
# "과보완료" → "과료" 처럼 엉뚱한 키가 생겨 무관한 파일이 한 계보로 묶인다.
# 따라서 모든 규칙은 "구분자 뒤 또는 문자열 끝"에서만 적용한다.
_SEP = r"(?:[_\-\s]|^)"
_END = r"(?=[_\-\s]|$)"

VERSION_PATTERNS = [
    r"\s*\(\d+\)" + _END,                        # "보고서 (1)"  재다운로드
    _SEP + r"copy(?:\s*\d+)?" + _END,
    _SEP + r"사본(?:\s*\d+)?" + _END,
    r"\s+\d{1,2}$",                                # "kit 2"  Finder 복제
    _SEP + r"v\.?\s*\d+(?:\.\d+)*" + _END,        # v7, v1.2
    _SEP + r"\d+\s*교본" + _END,                     # 3교본
    _SEP + r"(?:최종본|최종|파이널|final|FINAL|Final)" + _END,
    _SEP + r"(?:수정본|수정|보완|개정|초안|초고|draft|DRAFT)" + _END,
    _SEP + r"\d{6,8}" + _END,                        # 260812 날짜 스탬프
    _SEP + r"\d{4}[-.]\d{2}[-.]\d{2}" + _END,
    _SEP + r"(?:rev|REV)\.?\s*\d+" + _END,
]
VERSION_RE = [re.compile(p) for p in VERSION_PATTERNS]

# 계보 판정에서 무시할 확장자 짝 (같은 문서의 포맷 변형)
FORMAT_SIBLINGS = [
    {"hwp", "hwpx", "docx", "doc", "pdf"},
    {"pptx", "ppt", "pdf", "key"},
    {"xlsx", "xls", "csv"},
    {"md", "txt"},
]


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def canon_stem(stem: str) -> str:
    """버전 표기를 벗겨낸 '작품명'. 계보 클러스터의 키가 된다."""
    s = nfc(stem)
    prev = None
    while prev != s:                       # 중첩 표기(v2 (1) 최종)를 반복 제거
        prev = s
        for r in VERSION_RE:
            s = r.sub(" ", s)
    s = re.sub(r"[_\-\s]+", " ", s).strip(" _-.")
    return s.lower()


# 경로에 이 표식이 있으면 '사본'이다. Finder 복제·브라우저 재다운로드의 흔적.
# Finder 복제본은 이름 끝에 " 2" 를 붙이는데, 파일이면 그 뒤에 확장자가 온다.
# 경로 끝이나 슬래시만 보면 `보고서 2.pdf` 를 놓쳐 원본과 사본이 뒤바뀐다.
# 밑줄+숫자(`_v5_1.hwp`)는 제외한다 — Finder 는 항상 공백을 쓰고,
# 밑줄 뒤 숫자는 거의 언제나 버전 번호의 일부다.
COPY_MARK_RE = re.compile(
    r"(?: \d{1,2}(?=\.[^./]{1,8}$|/|$)"   # "혁신국 2/", "보고서 2.pdf"
    r"|\s*\(\d+\)"                          # "보고서 (1)"  재다운로드
    r"|[_\-\s]사본"
    r"|[_\-\s]copy"
    r"|[_\-\s]복사본)"
)


def copy_marks(path: str) -> int:
    """경로 전체(상위 폴더 포함)에서 복제 표식이 몇 번 나타나는가."""
    return len(COPY_MARK_RE.findall(nfc(path)))


# 사람이 붙인 이름이 아니라 기기·앱이 자동으로 만든 이름들.
AUTO_NAME_RE = re.compile(
    r"^(kakaotalk[_-]|img[_-]?\d|dsc[_-]?\d|photo[_-]?\d|image[_-]?\d|"
    r"screenshot|스크린샷|화면\s?캡처|download(\s?\(\d+\))?$|"
    r"unnamed|untitled|제목\s?없음|[0-9a-f]{16,}$|\d{9,}(_\d+)*$)", re.I)

# 사용자가 쓰는 날짜 접두 규칙: 260812_ 또는 20260812_
DATE_PREFIX_RE = re.compile(r"^(\d{6}|\d{8})(?=[_\-\s])")


def is_auto_name(stem: str) -> bool:
    return bool(AUTO_NAME_RE.match(nfc(stem).strip()))


# arXiv ID·UUID·해시처럼 내용을 전혀 설명하지 못하는 이름.
OPAQUE_NAME_RE = re.compile(
    r"^(?:\d{4}\.\d{4,5}(v\d+)?|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]+"
    r"|[0-9a-f]{16,}|[0-9a-f]{6,}[-_](en|ko|fr|de|es)|ssrn[-_]?id\d+"
    r"|\d{1,3})$", re.I)


def is_opaque_name(name: str) -> bool:
    """이 이름은 파일이 무엇인지 알려주지 않는다.

    `2508.09736v1` 과 `Seeing, Listening, Remembering...` 이 같은 내용일 때
    짧은 쪽을 남기면 애써 붙인 제목이 사라진다. 길이보다 설명력이 먼저다.

    stem 이 아니라 이름 전체를 받는다. `2508.09736v1` 처럼 이름 안의 점이
    확장자 구분과 헷갈리는 경우 stem 이 `2508` 로 잘려 판정이 무너진다.
    """
    n = nfc(name).strip()
    if OPAQUE_NAME_RE.match(n):
        return True
    m = re.match(r"^(.*)\.[0-9A-Za-z]{1,5}$", n)   # 진짜 확장자만 떼어본다
    return bool(m and OPAQUE_NAME_RE.match(m.group(1)))


def date_prefix(stem: str) -> int:
    """날짜 접두사를 비교 가능한 정수로. 없으면 0."""
    m = DATE_PREFIX_RE.match(nfc(stem))
    if not m:
        return 0
    d = m.group(1)
    return int("20" + d) if len(d) == 6 else int(d)


def canonical_rank(r: dict) -> tuple:
    """정렬 시 맨 앞에 오는 것이 정본(keep). 낮을수록 정본답다.

    우선순위에는 이유가 있다.
    1) 복제 표식 — `프로젝트자료`과 `~ 2`는 mtime·깊이가 같으므로
       표식을 보지 않으면 원본을 사본으로 오판한다.
    2) 자동 생성 이름 — `KakaoTalk_Photo_2026-04-02…jpeg`와 `프로필사진.jpeg`가
       같은 파일일 때, 사람이 붙인 이름을 남겨야 한다. mtime으로 고르면 먼저
       받은 카톡 원본이 이겨서, 정리 후 '프로필'로 검색해도 안 나온다.
    3) 날짜 접두사 — 사용자의 `260812_` 규칙에서는 나중 날짜가 최신본이다.
    """
    p = r["path"]
    stem = r.get("stem") or os.path.splitext(os.path.basename(p))[0]
    return (
        copy_marks(p),                 # 복제 표식이 없는 쪽이 원본
        is_auto_name(stem),            # 사람이 붙인 이름을 남긴다
        is_opaque_name(os.path.basename(p)),  # 내용을 설명하는 이름을 남긴다
        -date_prefix(stem),            # 날짜 접두사는 나중 것이 최신
        p.count("/"),                  # 얕은 경로 우선
        r["mtime"],                    # 그래도 동률이면 먼저 생긴 쪽
        len(r["name"]),
        p,                             # 완전 동률 시 결정적 순서
    )


def full_hash(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
    except OSError:
        return ""
    return h.hexdigest()


def load(inv: Path) -> list[dict]:
    recs = []
    with inv.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def find_exact(recs: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """크기 → 앞해시 → 전문해시 3단계. 반환: (그룹목록, path→전문해시)."""
    by_size: dict[int, list[dict]] = defaultdict(list)
    for r in recs:
        if r.get("kind") == "file" and r.get("size", 0) > 0:
            by_size[r["size"]].append(r)

    candidates = []
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        by_head: dict[str, list[dict]] = defaultdict(list)
        for r in group:
            by_head[r.get("hhash", "")].append(r)
        for hh, g2 in by_head.items():
            if len(g2) >= 2:
                candidates.extend(g2)

    fh_map: dict[str, str] = {}
    for r in candidates:
        fh_map[r["path"]] = full_hash(r["path"])

    by_full: dict[str, list[dict]] = defaultdict(list)
    for r in candidates:
        fh = fh_map.get(r["path"], "")
        if fh:
            by_full[fh].append(r)

    groups = []
    for fh, g in by_full.items():
        if len(g) < 2:
            continue
        g_sorted = sorted(g, key=canonical_rank)
        groups.append({
            "hash": fh,
            "size": g[0]["size"],
            "count": len(g),
            "wasted_bytes": g[0]["size"] * (len(g) - 1),
            "keep": g_sorted[0]["path"],
            "duplicates": [r["path"] for r in g_sorted[1:]],
        })
    groups.sort(key=lambda x: -x["wasted_bytes"])
    return groups, fh_map


def find_folder_dupes(recs: list[dict], fh_map: dict[str, str],
                      min_files: int = 3) -> list[dict]:
    """폴더 단위 복제쌍. A의 파일 중 몇 %가 B에 동일 해시로 있는가."""
    # 해시를 아는 파일만으로 폴더별 해시집합을 만든다
    by_dir: dict[str, set[str]] = defaultdict(set)
    dir_count: dict[str, int] = defaultdict(int)
    for r in recs:
        if r.get("kind") != "file":
            continue
        dir_count[r["parent"]] += 1
        fh = fh_map.get(r["path"])
        if fh:
            by_dir[r["parent"]].add(fh)

    dirs = [d for d, s in by_dir.items() if len(s) >= min_files]
    pairs = []
    for i, a in enumerate(dirs):
        for b in dirs[i + 1:]:
            sa, sb = by_dir[a], by_dir[b]
            inter = sa & sb
            if len(inter) < min_files:
                continue
            cov_a = len(inter) / len(sa)
            cov_b = len(inter) / len(sb)
            if max(cov_a, cov_b) < 0.8:      # 한쪽이 다른 쪽에 8할 이상 포함될 때만
                continue
            # 어느 쪽이 사본인가: 복제 표식 → 포함관계 → 경로 깊이 순으로 판정
            ma, mb = copy_marks(a), copy_marks(b)
            if ma != mb:
                sub, sup = (a, b) if ma > mb else (b, a)
                cov = cov_a if sub == a else cov_b
            elif abs(cov_a - cov_b) > 1e-9:
                sub, sup, cov = (a, b, cov_a) if cov_a > cov_b else (b, a, cov_b)
            else:
                sub, sup = (a, b) if canonical_rank({"path": b, "mtime": 0, "name": b}) \
                    < canonical_rank({"path": a, "mtime": 0, "name": a}) else (b, a)
                cov = cov_a
            pairs.append({
                "subset": sub, "superset": sup,
                "subset_files": dir_count[sub], "superset_files": dir_count[sup],
                "shared_hashes": len(inter),
                "coverage": round(cov, 3),
                "verdict": "identical" if cov >= 0.999 else "contained",
            })
    pairs.sort(key=lambda p: -p["shared_hashes"])
    return pairs


VERSION_NUM_RE = re.compile(r"(?:^|[_\-\s])v\.?\s*(\d+(?:[._]\d+)*)", re.I)
SEQ_WORD_RE = re.compile(r"(?:^|[_\-\s])(\d+)\s*교본")


def version_tuple(stem: str) -> tuple:
    """파일명에서 버전 번호를 뽑는다. v2.1 → (2,1), 없으면 빈 튜플.

    mtime 보다 이것이 우선한다. 파일은 재다운로드·포맷변환·열람만으로도
    시각이 갱신되지만, 이름 속 `v7` 은 사람이 직접 붙인 순서 표시다.
    실제로 `_v7.hwpx` 가 `_v4.pdf` 보다 mtime 이 이른 경우가 흔하다.
    """
    s = nfc(stem)
    # 자릿수를 맞춰 돌려준다. (2,) 와 (2,1) 을 그대로 비교하면 파이썬은 짧은
    # 튜플을 앞에 놓아 v2 가 v2.1 보다 최신으로 잡힌다.
    def pad(t):
        return tuple(t) + (0,) * (4 - len(t))

    m = VERSION_NUM_RE.search(s)
    if m:
        return pad([int(x) for x in re.split(r"[._]", m.group(1))][:4])
    m = SEQ_WORD_RE.search(s)
    if m:
        return pad([int(m.group(1))])
    if re.search(r"(?:^|[_\-\s])(최종본|최종|final)(?=[_\-\s.]|$)", s, re.I):
        return pad([9999])                 # '최종'은 번호 없는 것들보다 뒤
    return ()


def newest_first(r: dict) -> tuple:
    """최신본이 앞에 오도록 정렬. 낮을수록 최신이다."""
    stem = r.get("stem") or os.path.splitext(os.path.basename(r["path"]))[0]
    v = version_tuple(stem)
    return (
        copy_marks(r["path"]) > 0,     # '(1)' 사본은 최신본이 될 수 없다
        -(len(v) > 0),                 # 버전 표기가 있는 쪽을 먼저 본다
        tuple(-x for x in v),          # 큰 버전이 최신
        -date_prefix(stem),            # 그다음 날짜 접두사
        -r["mtime"],                   # 마지막에야 파일 시각
    )


def find_versions(recs: list[dict], min_members: int = 2) -> list[dict]:
    """버전 계보 클러스터. 이름에서 버전 표기를 벗겨 같아지는 것들."""
    clusters: dict[tuple, list[dict]] = defaultdict(list)
    for r in recs:
        if r.get("kind") != "file":
            continue
        c = canon_stem(r["stem"])
        if len(c) < 4:                      # 너무 짧은 키는 오탐이 많다
            continue
        clusters[(r["parent"], c)].append(r)

    out = []
    for (parent, key), members in clusters.items():
        if len(members) < min_members:
            continue
        # 같은 이름 + 다른 확장자는 한 문서의 포맷 변형이지 구버전이 아니다.
        # hwp 로 쓰고 pdf 로 내보낸 짝을 '구버전'이라며 치우면, 사용자는
        # 배포용 pdf 를 잃는다. 그래서 stem 단위로 먼저 묶어 대표만 남긴다.
        by_stem: dict[str, list[dict]] = defaultdict(list)
        for m in members:
            by_stem[nfc(m["stem"])].append(m)
        reps = [sorted(g, key=newest_first)[0] for g in by_stem.values()]
        siblings = [m for g in by_stem.values() for m in sorted(g, key=newest_first)[1:]]

        exts = {m["ext"] for m in members}
        if len(by_stem) == 1:
            kind = "format_variants"        # 이름이 하나뿐 — 버전 난립이 아니다
            ms = sorted(members, key=newest_first)
            older = []                      # 포맷 변형은 접지 않는다
        else:
            kind = "version_lineage"
            ms = sorted(reps, key=newest_first)
            # 구버전으로 접을 때는 그 버전의 다른 포맷 판본도 함께 데려간다
            newest_stem = nfc(ms[0]["stem"])
            older = [m["path"] for m in ms[1:]]
            older += [m["path"] for m in siblings
                      if nfc(m["stem"]) != newest_stem]

        out.append({
            "canon": key,
            "parent": parent,
            "type": kind,
            "count": len(members),
            "total_bytes": sum(m["size"] for m in members),
            "newest": ms[0]["path"],
            "older": older,
            "exts": sorted(exts),
        })
    out.sort(key=lambda c: (-c["count"], -c["total_bytes"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="중복·버전계보 탐지 (읽기 전용)")
    ap.add_argument("inventory", help="scan.py가 만든 JSONL")
    ap.add_argument("-o", "--out", required=True, help="결과 JSON 경로")
    ap.add_argument("--min-folder-files", type=int, default=3)
    args = ap.parse_args()

    recs = load(Path(args.inventory))
    exact, fh_map = find_exact(recs)
    folders = find_folder_dupes(recs, fh_map, args.min_folder_files)
    versions = find_versions(recs)

    result = {
        "scanned_files": len(recs),
        "exact_groups": exact,
        "folder_duplicates": folders,
        "version_clusters": versions,
        "summary": {
            "exact_group_count": len(exact),
            "exact_excess_files": sum(g["count"] - 1 for g in exact),
            "exact_wasted_mb": round(sum(g["wasted_bytes"] for g in exact) / 1048576, 1),
            "folder_pair_count": len(folders),
            "version_cluster_count": sum(1 for v in versions if v["type"] == "version_lineage"),
            "format_variant_count": sum(1 for v in versions if v["type"] == "format_variants"),
        },
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
