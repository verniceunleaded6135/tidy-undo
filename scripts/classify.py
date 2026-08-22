#!/usr/bin/env python3
"""Project attribution classifier — decides by rule which project a file
belongs to, before any LLM is involved.

This user's filenames are descriptive (`제22대국회 제437회 법제사법위원회.pdf`,
`이슈리포트_초안_v15_국가반도체인프라리밸런싱.docx`), so the great majority are
settled by the name alone. The LLM therefore runs only on the remainder that
falls through here — that is the one place where spending tokens is worth it.

Korean forms compound words with no spaces between them
(`국가반도체인프라리밸런싱`), which makes word segmentation hard. So alongside
whitespace-delimited tokens we use character 3-grams. A 3-gram catches
`반도체` even when it sits inside `국가반도체인프라`.
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

# Tokens that turn up everywhere and so discriminate nothing. Without removing
# them every file scores a little against every project and the scores become
# meaningless. The Korean entries are literal filename tokens (자료 "material",
# 문서 "document", 파일 "file", 최종 "final", 수정 "revised", 초안 "draft",
# 보고/보고서 "report", 정리 "cleanup", 관련 "related", 첨부/붙임 "attachment",
# 복사본/사본 "copy", 제출 "submission", 결과 "result", 내용 "contents",
# 기타 "other") — functional matching data, not translatable UI text.
STOP = {
    "final", "draft", "copy", "temp", "test", "new", "old", "backup", "doc",
    "docs", "file", "files", "data", "report", "version", "download",
    "workspace", "output", "assets", "src", "main", "index", "readme",
    "자료", "문서", "파일", "최종", "수정", "초안", "보고", "보고서", "정리",
    "관련", "첨부", "붙임", "복사본", "사본", "제출", "결과", "내용", "기타",
}
# Things that sit at the top level of the home directory but are not projects.
# The Korean names below are literal directory names on disk, not UI text.
NOT_PROJECT = {
    "Applications", "Desktop", "Documents", "Downloads", "Library", "Movies",
    "Music", "Pictures", "Public", "Sites", "opt", "bin", "go",
    # Output directories created by this skill. If they were picked up as
    # project candidates, the result of one cleanup would become the
    # classification basis of the next, and the whole thing would loop.
    "Archive", "_자료", "_민감자료", "_정리됨", "_cleaner_quarantine",
}


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def tokens(text: str) -> set[str]:
    """Whitespace-delimited tokens plus Hangul n-grams, all lowercased."""
    t = nfc(text).lower()
    out: set[str] = set()
    for w in SPLIT.split(t):
        if len(w) >= 3 and w not in STOP:
            out.add(w)
    for m in HANGUL.finditer(t):
        s = m.group()
        for n in (3, 4):                     # use 3-grams and 4-grams together
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
    """Treat top-level directories in the home folder as project candidates.

    The first heading of CLAUDE.md is read in as an alias. Even when the
    directory name is short or opaque, like `test` or `n2sf`, the title inside
    its CLAUDE.md reveals the real subject.

    With scan_home=False the home directory is not read at all
    (`--no-home-scan`). By default this classifier builds its vocabulary from
    every top-level directory name in the home folder plus up to 600 filenames
    inside each — that is, it looks at material well outside the target
    directory (~/Downloads, say). On a shared Mac, or for a user uncomfortable
    with that reach, this option turns it off and restricts the vocabulary
    sources to the directories named with `--project-dir`. The rule-based
    resolution rate then drops and more files end up unclassified — that is the
    exact trade-off.
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
        # File and folder names inside the project feed the vocabulary too. The
        # subject words a project actually deals with live in its internal
        # filenames far more accurately than in the directory name. Looking at
        # the top level only would leave the vocabulary of any project whose
        # output sits under something like _workspace/ completely empty — so the
        # projects with the most material would be exactly the ones nothing gets
        # attributed to.
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
    """Rate a token's discriminating power by how many projects it appears in.

    A token like `보고서` ("report") shows up in nearly every project and so
    points at none of them. Conversely, if `반도체인프라` occurs in exactly one
    project, that single token settles the attribution. Ignore this difference
    and count tokens equally, and the longer a filename is the more common
    tokens it drags in, burying the right answer — which is why the resolution
    rate of the earlier attempt was so low.
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
    """Sum of the discriminating power of the matched tokens. Deliberately not
    divided by filename length.

    Dividing would penalise exactly the descriptive, long filenames we want.
    Instead the absolute score and the margin over the runner-up decide whether
    the attribution is confirmed.
    """
    hit = file_tokens & proj["vocab"]
    if not hit:
        return 0.0, []
    total = 0.0
    for h in hit:
        w = idf.get(h, 0.0)
        if w <= 0:                       # a token present in every project tells us nothing
            continue
        total += w * min(len(h), 6) / 3   # a gentle bonus for longer tokens
    ranked = sorted(hit, key=lambda h: -idf.get(h, 0.0) * len(h))
    return total, [h for h in ranked if idf.get(h, 0) > 0][:6]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Produce project attribution candidates for each file")
    ap.add_argument("inventory", help="scan.py JSONL")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--home", default=os.path.expanduser("~"))
    ap.add_argument("--project-dir", action="append", default=[],
                    help="name an additional project outside the home directory")
    ap.add_argument("--min-score", type=float, default=6.0,
                    help="leave anything below this score unclassified")
    ap.add_argument("--margin", type=float, default=1.25,
                    help="confirm only when the top score is this many times the runner-up")
    ap.add_argument("--no-home-scan", action="store_true",
                    help="do not sweep the whole home directory (privacy-conservative "
                         "mode). Only the directories given with --project-dir are used "
                         "as vocabulary sources. The rule-based confirmation rate drops "
                         "and more files stay unclassified")
    args = ap.parse_args()

    projects = discover_projects(args.home, args.project_dir,
                                 scan_home=not args.no_home_scan)
    idf = build_idf(projects)
    print(f"{len(projects)} project candidates · "
          f"{sum(1 for v in idf.values() if v > 0)} discriminating tokens",
          file=sys.stderr)

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
                stats["confirmed"] += 1
            elif top and top[0] >= args.min_score:
                verdict, project, conf, evidence = "ambiguous", None, top[0], top[2]
                stats["contested"] += 1
            else:
                verdict, project, conf, evidence = "unresolved", None, 0.0, []
                stats["unclassified"] += 1

            results.append({
                "path": r["path"], "name": r["name"], "ext": r["ext"],
                "category": r["category"], "size": r["size"], "mtime": r["mtime"],
                # Carry the parent path along so the later stages (judgement,
                # planning) can look at sibling files and stay consistent.
                # Without it they would have to re-join the inventory each time.
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
        "rule_rate": round(stats["confirmed"] / max(len(results), 1), 3),
        "top_projects": dict(sorted(by_proj.items(), key=lambda x: -x[1])[:12]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
