#!/usr/bin/env python3
"""프로젝트 귀속 분류기 — 파일이 어느 프로젝트 자료인지 규칙으로 먼저 가른다.

이 사용자의 파일명은 서술적이라(`제22대국회 제437회 법제사법위원회.pdf`,
`이슈리포트_초안_v15_국가반도체인프라리밸런싱.docx`) 파일명만으로 대다수가
판별된다. 따라서 LLM은 여기서 걸러지지 않은 잔여분에만 쓴다 — 그게 토큰을
쓸 가치가 있는 유일한 지점이다.

한국어는 공백 없이 붙는 복합어가 많아(`국가반도체인프라리밸런싱`) 단어 분리가
어렵다. 그래서 어절 토큰과 함께 문자 3-gram을 쓴다. 3-gram은 `반도체`가
`국가반도체인프라` 안에 있어도 잡아낸다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict

HANGUL = re.compile(r"[가-힣]{2,}")
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
SPLIT = re.compile(r"[^0-9A-Za-z가-힣]+")

# 어디에나 나와서 변별력이 없는 토큰. 이걸 빼지 않으면 모든 파일이
# 모든 프로젝트에 조금씩 걸려 점수가 무의미해진다.
STOP = {
    "final", "draft", "copy", "temp", "test", "new", "old", "backup", "doc",
    "docs", "file", "files", "data", "report", "version", "download",
    "workspace", "output", "assets", "src", "main", "index", "readme",
    "자료", "문서", "파일", "최종", "수정", "초안", "보고", "보고서", "정리",
    "관련", "첨부", "붙임", "복사본", "사본", "제출", "결과", "내용", "기타",
}
# 홈 최상위에 있지만 프로젝트가 아닌 것
NOT_PROJECT = {
    "Applications", "Desktop", "Documents", "Downloads", "Library", "Movies",
    "Music", "Pictures", "Public", "Sites", "opt", "bin", "go",
    # 이 스킬이 만든 산출물 디렉토리. 프로젝트 후보로 잡히면 정리 결과가
    # 다음 실행의 분류 기준이 되어 순환한다.
    "Archive", "_자료", "_민감자료", "_정리됨", "_cleaner_quarantine",
}


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def tokens(text: str) -> set[str]:
    """어절 토큰 + 한글 3-gram. 소문자로 통일한다."""
    t = nfc(text).lower()
    out: set[str] = set()
    for w in SPLIT.split(t):
        if len(w) >= 3 and w not in STOP:
            out.add(w)
    for m in HANGUL.finditer(t):
        s = m.group()
        for n in (3, 4):                     # 3-gram과 4-gram을 함께 쓴다
            for i in range(len(s) - n + 1):
                g = s[i:i + n]
                if g not in STOP:
                    out.add(g)
    for m in LATIN.finditer(t):
        w = m.group().lower()
        if w not in STOP:
            out.add(w)
    return out


def discover_projects(home: str, extra_dirs: list[str],
                      scan_home: bool = True) -> list[dict]:
    """홈 최상위 디렉토리를 프로젝트 후보로 삼는다.

    CLAUDE.md 첫 제목을 별명으로 함께 읽는다. 디렉토리명이 `test`, `n2sf`처럼
    짧거나 불투명해도, 그 안의 CLAUDE.md 제목이 진짜 주제를 알려준다.

    scan_home=False면 홈 전체를 읽지 않는다(`--no-home-scan`). 이 분류기는
    기본값으로 홈의 모든 최상위 디렉토리 이름과 그 안의 파일명을 최대 600개까지
    읽어 어휘를 만든다 — 대상 디렉토리(예: ~/Downloads) 밖의 자료까지 훑는
    셈이다. 공유 Mac이거나 그 범위가 부담스러운 사용자는 이 옵션으로 끄고
    `--project-dir`로 명시한 디렉토리만 어휘원으로 쓸 수 있다. 그 경우 프로젝트
    귀속 규칙의 확정률은 낮아지고 미분류가 늘어난다 — 정확한 트레이드오프다.
    """
    projects = []
    for name in sorted(os.listdir(home)) if scan_home else []:
        p = os.path.join(home, name)
        if (name.startswith(".") or name in NOT_PROJECT
                or not os.path.isdir(p) or os.path.islink(p)):
            continue
        aliases = [name]
        for marker in ("CLAUDE.md", "README.md"):
            mp = os.path.join(p, marker)
            if os.path.isfile(mp):
                try:
                    with open(mp, encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.startswith("#"):
                                aliases.append(line.lstrip("# ").strip())
                                break
                except OSError:
                    pass
                break
        vocab = set()
        for a in aliases:
            vocab |= tokens(a)
        # 프로젝트 안의 파일·폴더 이름도 어휘로 쓴다. 그 프로젝트가 실제로
        # 다루는 주제어는 디렉토리명보다 내부 파일명에 더 정확히 들어 있다.
        # 최상위만 보면 산출물이 _workspace/ 같은 하위에 있는 프로젝트의 어휘가
        # 통째로 비어, 정작 자료가 많은 프로젝트일수록 귀속이 안 된다.
        seen_names = 0
        stack = [(p, 0)]
        while stack and seen_names < 600:
            cur, depth = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < 2 and entry.name not in NOT_PROJECT:
                        vocab |= tokens(entry.name)
                        stack.append((entry.path, depth + 1))
                else:
                    vocab |= tokens(os.path.splitext(entry.name)[0])
                    seen_names += 1
                    if seen_names >= 600:
                        break
        if vocab:
            projects.append({"name": name, "path": p,
                             "aliases": aliases, "vocab": vocab})

    for d in extra_dirs:
        d = os.path.expanduser(d)
        if os.path.isdir(d):
            projects.append({"name": os.path.basename(d), "path": d,
                             "aliases": [os.path.basename(d)],
                             "vocab": tokens(os.path.basename(d))})
    return projects


def build_idf(projects: list[dict]) -> dict[str, float]:
    """토큰이 몇 개 프로젝트에 등장하는지로 변별력을 매긴다.

    `보고서`처럼 거의 모든 프로젝트에 나오는 토큰은 어느 프로젝트를 가리키지도
    못한다. 반대로 `반도체인프라`가 한 프로젝트에만 있다면 그 토큰 하나로
    귀속이 결정된다. 이 차이를 무시하고 토큰을 동등하게 세면, 파일명이 길수록
    흔한 토큰이 많이 섞여 정답이 묻힌다 — 앞선 시도의 귀속률이 낮았던 이유다.
    """
    df: dict[str, int] = {}
    for proj in projects:
        for t in proj["vocab"]:
            df[t] = df.get(t, 0) + 1
    n = len(projects) or 1
    import math
    return {t: math.log(n / c) for t, c in df.items()}


def score(file_tokens: set[str], proj: dict,
          idf: dict[str, float]) -> tuple[float, list[str]]:
    """일치한 토큰의 변별력 총합. 파일명 길이로 나누지 않는다.

    나누면 서술적인 긴 파일명이 오히려 불리해진다. 대신 절대 점수와 2위 대비
    격차로 확정 여부를 가른다.
    """
    hit = file_tokens & proj["vocab"]
    if not hit:
        return 0.0, []
    total = 0.0
    for h in hit:
        w = idf.get(h, 0.0)
        if w <= 0:                       # 모든 프로젝트에 있는 토큰은 무시
            continue
        total += w * min(len(h), 6) / 3   # 긴 토큰에 완만한 가산
    ranked = sorted(hit, key=lambda h: -idf.get(h, 0.0) * len(h))
    return total, [h for h in ranked if idf.get(h, 0) > 0][:6]


def main() -> int:
    ap = argparse.ArgumentParser(description="파일의 프로젝트 귀속 후보 산출")
    ap.add_argument("inventory", help="scan.py JSONL")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--home", default=os.path.expanduser("~"))
    ap.add_argument("--project-dir", action="append", default=[],
                    help="홈 밖의 프로젝트를 추가로 지정")
    ap.add_argument("--min-score", type=float, default=6.0,
                    help="이 점수 미만이면 미분류로 남긴다")
    ap.add_argument("--margin", type=float, default=1.25,
                    help="1위가 2위의 이 배수 이상일 때만 확정")
    ap.add_argument("--no-home-scan", action="store_true",
                    help="홈 전체를 훑지 않는다(개인정보 보수적 모드). "
                         "--project-dir로 지정한 디렉토리만 어휘원으로 쓴다. "
                         "규칙 확정률이 낮아지고 미분류가 늘어난다")
    args = ap.parse_args()

    projects = discover_projects(args.home, args.project_dir,
                                 scan_home=not args.no_home_scan)
    idf = build_idf(projects)
    print(f"프로젝트 후보 {len(projects)}개 · 변별 토큰 "
          f"{sum(1 for v in idf.values() if v > 0)}개", file=sys.stderr)

    results, stats = [], defaultdict(int)
    with open(args.inventory, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("kind") != "file":
                continue
            ft = tokens(r["stem"])
            scored = []
            for proj in projects:
                s, hits = score(ft, proj, idf)
                if s > 0:
                    scored.append((s, proj["name"], hits))
            scored.sort(reverse=True)

            top = scored[0] if scored else None
            second = scored[1][0] if len(scored) > 1 else 0.0
            if top and top[0] >= args.min_score and top[0] >= second * args.margin:
                verdict, project, conf, evidence = "rule", top[1], top[0], top[2]
                stats["확정"] += 1
            elif top and top[0] >= args.min_score:
                verdict, project, conf, evidence = "ambiguous", None, top[0], top[2]
                stats["경합"] += 1
            else:
                verdict, project, conf, evidence = "unresolved", None, 0.0, []
                stats["미분류"] += 1

            results.append({
                "path": r["path"], "name": r["name"], "ext": r["ext"],
                "category": r["category"], "size": r["size"], "mtime": r["mtime"],
                # 후속 단계(판정·계획)가 형제 파일을 보고 일관성을 맞출 수 있게
                # 부모 경로를 함께 남긴다. 없으면 매번 인벤토리를 다시 조인해야 한다.
                "parent": r["parent"],
                "verdict": verdict, "project": project,
                "confidence": round(conf, 3), "evidence": evidence,
                "runner_up": [{"project": n, "score": round(s, 3)}
                              for s, n, _ in scored[1:4]],
            })

    with open(args.out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_proj = defaultdict(int)
    for r in results:
        if r["project"]:
            by_proj[r["project"]] += 1
    print(json.dumps({
        "total": len(results), "breakdown": dict(stats),
        "rule_rate": round(stats["확정"] / max(len(results), 1), 3),
        "top_projects": dict(sorted(by_proj.items(), key=lambda x: -x[1])[:12]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
