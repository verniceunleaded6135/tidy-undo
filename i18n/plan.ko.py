#!/usr/bin/env python3
"""계획 생성기 — 인벤토리·중복·귀속 결과를 하나의 실행 계획으로 합친다.

계획은 사람이 읽고 승인하는 문서이자 실행기의 유일한 입력이다. 그래서 모든
항목에 '왜 이렇게 옮기는가'를 남긴다. 근거 없는 이동은 계획에 넣지 않는다.

작업 종류:
  archive   프로젝트 귀속 자료를 <목적지>/<프로젝트>/<종류>/ 로
  quarantine 중복본을 <root>/_cleaner_quarantine/<run>/ 로 (삭제 아님)
  version   같은 문서의 옛 버전을 형제 폴더 _archive/ 로
"""
from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata

# dedup.py 와 같은 규칙. 복제 표식이 있는 쪽이 사본이다.
COPY_MARK_RE = re.compile(
    r"(?: \d{1,2}(?=\.[^./]{1,8}$|/|$)|\s*\(\d+\)"
    r"|[_\-\s]사본|[_\-\s]copy|[_\-\s]복사본)")
from collections import defaultdict

KIND_DIR = {
    "doc": "문서", "text": "메모", "deck": "발표", "sheet": "표",
    "data": "데이터", "image": "이미지", "video": "영상", "audio": "음성",
    "archive": "압축", "installer": "설치", "code": "코드",
    "config": "설정", "web": "웹", "other": "기타",
}
QUAR = "_cleaner_quarantine"
UNSORTED = "_미분류"


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def load_jsonl(p: str) -> list[dict]:
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def safe_component(name: str) -> str:
    """경로 한 칸으로 쓸 수 있게 다듬는다. 슬래시는 폴더를 쪼개므로 치명적."""
    s = nfc(name).replace("/", "／").replace("\0", "").strip(" .")
    return s[:60] or "_"


def main() -> int:
    ap = argparse.ArgumentParser(description="정리 계획 생성 (실행하지 않음)")
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--classified", help="classify.py 결과 JSONL")
    ap.add_argument("--dupes", help="dedup.py 결과 JSON")
    ap.add_argument("--root", required=True, help="스코프 root (여기 밖으로 못 나간다)")
    ap.add_argument("--dest", help="아카이브 목적지 (기본: <root>/_정리됨)")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--do", default="archive,quarantine,version",
                    help="수행할 작업 종류 (쉼표 구분)")
    ap.add_argument("--min-confidence", type=float, default=0.5,
                    help="이 값 미만 신뢰도는 _미분류로 보낸다")
    ap.add_argument("--trust-rules", action="store_true",
                    help="규칙 단독 판정도 이동 대상으로 삼는다 (권장하지 않음)")
    ap.add_argument("--include-unsorted", action="store_true",
                    help="귀속 실패분도 _미분류/ 로 옮긴다 (기본은 그 자리에 둠)")
    ap.add_argument("--sensitive", help="sensitive.py 결과 JSONL")
    ap.add_argument("--retitle", help="retitle.py 결과 JSONL")
    ap.add_argument("--exclude", help="이 JSONL의 path는 계획에서 제외한다")
    ap.add_argument("--exclude-op", action="append", default=[],
                    help="이 경로는 계획에서 제외한다 (여러 번 지정 가능)")
    args = ap.parse_args()

    root = os.path.realpath(os.path.expanduser(args.root))
    dest = os.path.realpath(os.path.expanduser(args.dest)) if args.dest \
        else os.path.join(root, "_정리됨")

    def under_root(path: str) -> bool:
        """path 가 root 하위인가. NFC 로 맞춰 비교한다.

        셸에서 받은 root 는 NFC 이고 디스크가 준 경로는 NFD 라, 문자열을 그대로
        비교하면 같은 폴더인데도 '스코프 밖'으로 판정된다. 한글 경로에서 계획이
        통째로 비는 원인이 이것이다.
        """
        a, b = nfc(path), nfc(root)
        return a == b or a.startswith(b + os.sep)

    def rel_to_root(path: str) -> str | None:
        """root 기준 상대경로. 밖이면 None."""
        if not under_root(path):
            return None
        return nfc(path)[len(nfc(root)):].lstrip(os.sep)
    do = {x.strip() for x in args.do.split(",") if x.strip()}

    inv = {r["path"]: r for r in load_jsonl(args.inventory)}
    ops: list[dict] = []
    skipped_cross: list[dict] = []
    rule_only: list[dict] = []
    excluded_sensitive: list[dict] = []
    sensitive_paths: set[str] = set()
    if args.exclude:
        for r in load_jsonl(args.exclude):
            sensitive_paths.add(r["path"])
    claimed: set[str] = set()          # 한 파일에 두 작업이 걸리지 않게 한다

    # --- 1. 중복 격리 ------------------------------------------------------
    # 정본 1부는 반드시 남긴다. 격리는 삭제가 아니라 원래 구조를 그대로 재현한
    # 폴더로의 이동이라, 매니페스트가 없어도 눈으로 되돌릴 수 있다.
    if "quarantine" in do and args.dupes:
        d = json.load(open(args.dupes, encoding="utf-8"))
        for g in d.get("exact_groups", []):
            for dup in g["duplicates"]:
                if dup in claimed or dup not in inv:
                    continue
                # 정본과 다른 폴더에 있으면서 복제 표식도 없는 파일은 건드리지
                # 않는다. 사용자가 부서별·주제별로 손수 쌓아둔 아카이브에서
                # 한 버전만 사라져 구멍이 생기기 때문이다. 표식이 있는 미러본은
                # 어차피 따로 격리되므로 절약 효과도 잃지 않는다.
                same_parent = os.path.dirname(dup) == os.path.dirname(g["keep"])
                if not same_parent and not COPY_MARK_RE.search(nfc(dup)):
                    skipped_cross.append(
                        {"path": dup, "keep": g["keep"],
                         "why": "정본과 다른 폴더, 복제 표식 없음"})
                    continue
                rel = rel_to_root(dup)
                if rel is None:
                    continue           # 스코프 밖은 계획에 넣지 않는다
                ops.append({
                    "action": "quarantine", "src": dup,
                    "dst": os.path.join(root, QUAR, rel),
                    "reason": f"완전중복 — 정본 유지: {os.path.basename(g['keep'])}",
                    "evidence": {"hash": g["hash"], "group_size": g["count"],
                                 "keep": g["keep"], "bytes_freed": g["size"]},
                    "snapshot": {"size": inv[dup]["size"],
                                 "mtime": inv[dup]["mtime"]},
                })
                claimed.add(dup)

    # --- 2. 옛 버전 접기 ---------------------------------------------------
    # 최신본은 제자리에 두고 옛 버전만 형제 _archive/ 로 내린다. 파일이 사라진
    # 게 아니라 한 칸 아래로 내려간 것이라 사용자가 찾기 쉽다.
    if "version" in do and args.dupes:
        d = json.load(open(args.dupes, encoding="utf-8"))
        for c in d.get("version_clusters", []):
            if c["type"] != "version_lineage":
                continue
            for old in c["older"]:
                if old in claimed or old not in inv:
                    continue
                parent = os.path.dirname(old)
                if not under_root(parent):
                    continue
                ops.append({
                    "action": "version", "src": old,
                    "dst": os.path.join(parent, "_archive",
                                        os.path.basename(old)),
                    "reason": f"구버전 — 최신: {os.path.basename(c['newest'])}",
                    "evidence": {"cluster": c["canon"], "members": c["count"]},
                    "snapshot": {"size": inv[old]["size"],
                                 "mtime": inv[old]["mtime"]},
                })
                claimed.add(old)

    # --- 3. 프로젝트 귀속 아카이브 -----------------------------------------
    if "archive" in do and args.classified:
        for r in load_jsonl(args.classified):
            p = r["path"]
            if p in claimed or p not in inv:
                continue
            if not under_root(p):
                continue
            proj, conf = r.get("project"), r.get("confidence", 0)
            # 규칙 판정은 토큰 겹침만 본다. 실측에서 감사가 잡아낸 오귀속 20건이
            # 전부 규칙 판정이었고 LLM 판정은 0건이었다 — 디자인팩 폴더명
            # 'ppt-samsung-ir-restrained' 가 삼성SDS 발표자료를 빨아들이는 식이다.
            # 그래서 규칙은 후보를 좁히는 데까지만 쓰고, 파일을 옮기는 결정은
            # 파일명을 읽은 판정(llm/audited/user)에만 맡긴다.
            trusted = r.get("verdict") in ("llm", "audited", "user") or args.trust_rules
            if proj and conf >= args.min_confidence and trusted:
                bucket = os.path.join(dest, safe_component(proj),
                                      KIND_DIR.get(r["category"], "기타"))
                reason = (f"{proj} 자료로 판정 (신뢰도 {conf:.2f}"
                          f", 근거: {', '.join(r.get('evidence', [])[:3])})")
            elif proj and not trusted:
                rule_only.append({"path": p, "project": proj,
                                  "why": "규칙 단독 판정 — 파일명 판독 필요"})
                continue
            elif args.include_unsorted:
                bucket = os.path.join(dest, UNSORTED,
                                      KIND_DIR.get(r["category"], "기타"))
                reason = "귀속 판단 불가 — 사용자 확인 필요"
            else:
                continue
            ops.append({
                "action": "archive", "src": p,
                "dst": os.path.join(bucket, os.path.basename(p)),
                "reason": reason,
                "evidence": {"project": proj, "confidence": conf,
                             "verdict": r.get("verdict"),
                             "runner_up": r.get("runner_up", [])[:2]},
                "snapshot": {"size": inv[p]["size"], "mtime": inv[p]["mtime"]},
            })
            claimed.add(p)

    # --- 3.2 개명 --------------------------------------------------------
    # 같은 폴더 안에서 이름만 바꾼다. 개명은 사용자의 기억을 깨뜨리므로
    # 대상은 '현재 이름이 내용을 전혀 설명하지 못하는 것'으로 한정한다.
    if "rename" in do and args.retitle:
        for r in load_jsonl(args.retitle):
            p_ = r["path"]
            if p_ in claimed or p_ not in inv:
                continue
            # 계획은 스코프 root 안의 파일만 다룬다. 이 검사가 없으면 인벤토리에
            # 섞여 들어온 다른 디렉토리의 파일까지 계획에 들어가고, 실행기가
            # 전량 거부해 계획이 통째로 무산된다.
            if not under_root(p_):
                continue
            ops.append({
                "action": "rename", "src": p_,
                "dst": os.path.join(os.path.dirname(p_), r["proposed"]),
                "reason": f"내용 제목으로 개명 (출처: {r.get('method','?')})",
                "evidence": {"was": r["current"], "title": r["title"],
                             "method": r.get("method")},
                "snapshot": {"size": inv[p_]["size"], "mtime": inv[p_]["mtime"]},
            })
            claimed.add(p_)

    # --- 3.5 트리 통째 이전 -------------------------------------------------
    # 이미 정리해 둔 폴더를 구조 그대로 다른 곳으로 옮긴다. 분류를 다시 하는
    # 게 아니라 위치만 바꾸는 것이므로, root 기준 상대경로를 그대로 보존한다.
    if "relocate" in do:
        for p_, r in inv.items():
            if r.get("kind") != "file" or p_ in claimed:
                continue
            rel = rel_to_root(p_)
            if rel is None:
                continue
            ops.append({
                "action": "relocate", "src": p_,
                "dst": os.path.join(dest, rel),
                "reason": f"보관 위치 이전 — 구조 유지 ({os.path.dirname(rel) or '.'})",
                "evidence": {"relative": rel},
                "snapshot": {"size": r["size"], "mtime": r["mtime"]},
            })
            claimed.add(p_)

    # --- 4. 민감 파일 별도 보관 ---------------------------------------------
    # 분류가 아니라 격상이다. 등급·종류별로 나눠 두면 나중에 폴더째 암호화
    # 디스크나 비밀번호 관리자로 옮기기 쉽다.
    if "sensitive" in do and args.sensitive:
        for r in load_jsonl(args.sensitive):
            p = r["path"]
            if p in claimed or p not in inv:
                continue
            bucket = os.path.join(dest, f"{r['sensitivity']}_{r['sens_kind']}")
            ops.append({
                "action": "sensitive", "src": p,
                "dst": os.path.join(bucket, os.path.basename(p)),
                "reason": f"{r['sensitivity']}등급 {r['sens_kind']} — "
                          f"파일명 '{r['matched']}' 근거",
                "evidence": {"level": r["sensitivity"], "kind": r["sens_kind"]},
                "snapshot": {"size": inv[p]["size"], "mtime": inv[p]["mtime"]},
            })
            claimed.add(p)

    # --- 제외 필터 ---------------------------------------------------------
    manual = {os.path.realpath(os.path.expanduser(x)) for x in args.exclude_op}
    kept = []
    for o in ops:
        if o["src"] in sensitive_paths:
            excluded_sensitive.append({"path": o["src"], "why": "민감 파일 — 별도 처리"})
        elif o["src"] in manual:
            excluded_sensitive.append({"path": o["src"], "why": "사용자 지정 제외"})
        else:
            kept.append(o)
    ops = kept

    # --- 요약 --------------------------------------------------------------
    by_action = defaultdict(lambda: {"count": 0, "bytes": 0})
    new_dirs = set()
    for o in ops:
        a = by_action[o["action"]]
        a["count"] += 1
        a["bytes"] += o["snapshot"]["size"]
        new_dirs.add(os.path.dirname(o["dst"]))
    new_dirs = {d for d in new_dirs if not os.path.isdir(d)}

    plan = {
        # dest 는 실제로 아카이브가 있을 때만 선언한다. 쓰지도 않는 목적지를
        # 매니페스트에 남기면 undo 가 엉뚱한 경로를 참조할 수 있다.
        "scope": ({"root": root, "dest": dest}
                  if (by_action.get("archive") or by_action.get("sensitive")
                      or by_action.get("relocate"))
                  else {"root": root}),
        "summary": {
            "total_operations": len(ops),
            "by_action": {k: {"count": v["count"],
                              "mb": round(v["bytes"] / 1048576, 1)}
                          for k, v in sorted(by_action.items())},
            "new_directories": len(new_dirs),
            "total_mb": round(sum(o["snapshot"]["size"] for o in ops) / 1048576, 1),
        },
        "new_directories": sorted(new_dirs),
        "not_actioned": {"cross_folder_duplicates": skipped_cross,
                         "excluded": excluded_sensitive,
                         "rule_only_needs_judgment": rule_only},
        "operations": ops,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    print(json.dumps(plan["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
