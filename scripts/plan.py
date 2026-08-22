#!/usr/bin/env python3
"""Plan builder — merges the inventory, duplicate and attribution results into
a single execution plan.

The plan is both the document a human reads and approves, and the executor's
only input. That is why every entry carries a "why is this being moved" reason.
A move with no evidence never enters the plan.

Operation kinds:
  archive    project-attributed material -> <dest>/<project>/<kind>/
  quarantine duplicates -> <root>/_cleaner_quarantine/<run>/ (NOT a delete)
  version    older revisions of the same document -> sibling _archive/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata

# Same rule as dedup.py: whichever side carries a copy marker is the copy.
# The Korean alternatives here (사본 / 복사본) are filename-matching data —
# they are how macOS and Korean users actually name duplicates.
COPY_MARK_RE = re.compile(
    r"(?: \d{1,2}(?=\.[^./]{1,8}$|/|$)|\s*\(\d+\)"
    r"|[_\-\s]사본|[_\-\s]copy|[_\-\s]복사본)")
from collections import defaultdict

# Literal directory names, not UI text. These strings become real folders on
# the user's disk, so translating them would break the folder tree of everyone
# who has already run a cleanup.
KIND_DIR = {
    "doc": "문서", "text": "메모", "deck": "발표", "sheet": "표",
    "data": "데이터", "image": "이미지", "video": "영상", "audio": "음성",
    "archive": "압축", "installer": "설치", "code": "코드",
    "config": "설정", "web": "웹", "other": "기타",
}
QUAR = "_cleaner_quarantine"
UNSORTED = "_미분류"          # literal directory name ("unsorted")


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def load_jsonl(p: str) -> list[dict]:
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def safe_component(name: str) -> str:
    """Make a string usable as one path segment. A slash is the fatal one —
    it would split the name into extra folders."""
    s = nfc(name).replace("/", "／").replace("\0", "").strip(" .")
    return s[:60] or "_"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build a cleanup plan (does not execute it)")
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--classified", help="classify.py output JSONL")
    ap.add_argument("--dupes", help="dedup.py output JSON")
    ap.add_argument("--root", required=True,
                    help="scope root (nothing may leave it)")
    ap.add_argument("--dest", help="archive destination (default: <root>/_정리됨)")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--do", default="archive,quarantine,version",
                    help="operation kinds to perform (comma-separated)")
    ap.add_argument("--min-confidence", type=float, default=0.5,
                    help="anything below this confidence goes to _미분류")
    ap.add_argument("--trust-rules", action="store_true",
                    help="also move files the rules alone attributed "
                         "(not recommended)")
    ap.add_argument("--include-unsorted", action="store_true",
                    help="also move unattributed files into _미분류/ "
                         "(default: leave them where they are)")
    ap.add_argument("--sensitive", help="sensitive.py output JSONL")
    ap.add_argument("--retitle", help="retitle.py output JSONL")
    ap.add_argument("--exclude",
                    help="paths in this JSONL are excluded from the plan")
    ap.add_argument("--exclude-op", action="append", default=[],
                    help="exclude this path from the plan (repeatable)")
    args = ap.parse_args()

    root = os.path.realpath(os.path.expanduser(args.root))
    dest = os.path.realpath(os.path.expanduser(args.dest)) if args.dest \
        else os.path.join(root, "_정리됨")     # literal directory name ("tidied")

    def under_root(path: str) -> bool:
        """Is path under root? Compared in NFC.

        A root arriving from the shell is NFC while the paths the disk hands
        back are NFD, so a raw string comparison declares the very same folder
        to be "out of scope". This is why a plan over a Korean-named path used
        to come out completely empty.
        """
        a, b = nfc(path), nfc(root)
        return a == b or a.startswith(b + os.sep)

    def rel_to_root(path: str) -> str | None:
        """Path relative to root. None if it is outside."""
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
    claimed: set[str] = set()          # never let two operations claim one file

    # --- 1. Quarantine duplicates -----------------------------------------
    # Exactly one canonical copy always stays. Quarantine is not a delete: it
    # is a move into a folder that reproduces the original structure verbatim,
    # so it can be reversed by eye even without a manifest.
    if "quarantine" in do and args.dupes:
        d = json.load(open(args.dupes, encoding="utf-8"))
        for g in d.get("exact_groups", []):
            for dup in g["duplicates"]:
                if dup in claimed or dup not in inv:
                    continue
                # Leave alone any file that sits in a different folder from the
                # canonical copy AND carries no copy marker. Those are archives
                # the user stacked up by department or by topic, and pulling one
                # version out punches a hole in them. Marked mirror copies get
                # quarantined anyway, so no space saving is lost.
                same_parent = os.path.dirname(dup) == os.path.dirname(g["keep"])
                if not same_parent and not COPY_MARK_RE.search(nfc(dup)):
                    skipped_cross.append(
                        {"path": dup, "keep": g["keep"],
                         "why": "different folder from the canonical copy, "
                                "no copy marker"})
                    continue
                rel = rel_to_root(dup)
                if rel is None:
                    continue           # out of scope never enters the plan
                ops.append({
                    "action": "quarantine", "src": dup,
                    "dst": os.path.join(root, QUAR, rel),
                    "reason": f"exact duplicate — keeping canonical: "
                              f"{os.path.basename(g['keep'])}",
                    "evidence": {"hash": g["hash"], "group_size": g["count"],
                                 "keep": g["keep"], "bytes_freed": g["size"]},
                    "snapshot": {"size": inv[dup]["size"],
                                 "mtime": inv[dup]["mtime"]},
                })
                claimed.add(dup)

    # --- 2. Fold away older versions ---------------------------------------
    # The newest revision stays put; only older ones drop into a sibling
    # _archive/. Nothing disappeared — it moved one level down, where the user
    # can still find it.
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
                    "reason": f"older revision — newest: "
                              f"{os.path.basename(c['newest'])}",
                    "evidence": {"cluster": c["canon"], "members": c["count"]},
                    "snapshot": {"size": inv[old]["size"],
                                 "mtime": inv[old]["mtime"]},
                })
                claimed.add(old)

    # --- 3. Archive by project attribution ---------------------------------
    if "archive" in do and args.classified:
        for r in load_jsonl(args.classified):
            p = r["path"]
            if p in claimed or p not in inv:
                continue
            if not under_root(p):
                continue
            proj, conf = r.get("project"), r.get("confidence", 0)
            # The rule-based verdict only looks at token overlap. In the field,
            # all 20 misattributions the auditor caught came from rules and 0
            # came from the LLM — e.g. a design-pack folder named
            # 'ppt-samsung-ir-restrained' swallowing Samsung SDS presentations.
            # So rules are used only to narrow the candidates; the decision to
            # actually move a file is left to a verdict that read the filename
            # (llm/audited/user).
            trusted = r.get("verdict") in ("llm", "audited", "user") or args.trust_rules
            if proj and conf >= args.min_confidence and trusted:
                bucket = os.path.join(dest, safe_component(proj),
                                      KIND_DIR.get(r["category"], "기타"))
                reason = (f"judged to be {proj} material (confidence {conf:.2f}"
                          f", evidence: {', '.join(r.get('evidence', [])[:3])})")
            elif proj and not trusted:
                rule_only.append({"path": p, "project": proj,
                                  "why": "rule-only verdict — needs a "
                                         "filename reading"})
                continue
            elif args.include_unsorted:
                bucket = os.path.join(dest, UNSORTED,
                                      KIND_DIR.get(r["category"], "기타"))
                reason = "cannot be attributed — needs user confirmation"
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

    # --- 3.2 Rename --------------------------------------------------------
    # Renames stay inside the same folder. A rename breaks the user's memory of
    # where things are, so the targets are limited to names that say nothing
    # whatsoever about the contents.
    if "rename" in do and args.retitle:
        for r in load_jsonl(args.retitle):
            p_ = r["path"]
            if p_ in claimed or p_ not in inv:
                continue
            # The plan only ever handles files inside the scope root. Without
            # this check, files from other directories that leaked into the
            # inventory end up in the plan and the executor rejects every one
            # of them, killing the whole plan.
            if not under_root(p_):
                continue
            ops.append({
                "action": "rename", "src": p_,
                "dst": os.path.join(os.path.dirname(p_), r["proposed"]),
                "reason": f"renamed to its content title "
                          f"(source: {r.get('method','?')})",
                "evidence": {"was": r["current"], "title": r["title"],
                             "method": r.get("method")},
                "snapshot": {"size": inv[p_]["size"], "mtime": inv[p_]["mtime"]},
            })
            claimed.add(p_)

    # --- 3.5 Relocate a whole tree -----------------------------------------
    # Moves an already-tidy folder somewhere else with its structure intact.
    # This is not a reclassification, only a change of location, so the path
    # relative to root is preserved exactly.
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
                "reason": f"relocated — structure preserved "
                          f"({os.path.dirname(rel) or '.'})",
                "evidence": {"relative": rel},
                "snapshot": {"size": r["size"], "mtime": r["mtime"]},
            })
            claimed.add(p_)

    # --- 4. Set sensitive files aside --------------------------------------
    # This is an escalation, not a classification. Split by level and kind, the
    # folders are easy to move wholesale onto an encrypted disk or into a
    # password manager later.
    if "sensitive" in do and args.sensitive:
        for r in load_jsonl(args.sensitive):
            p = r["path"]
            if p in claimed or p not in inv:
                continue
            # sens_kind is Korean by design — it is part of the folder name
            # this line builds (e.g. "2_신원"), not UI text. See sensitive.py.
            bucket = os.path.join(dest, f"{r['sensitivity']}_{r['sens_kind']}")
            ops.append({
                "action": "sensitive", "src": p,
                "dst": os.path.join(bucket, os.path.basename(p)),
                "reason": f"level {r['sensitivity']} {r['sens_kind']} — "
                          f"on the evidence of '{r['matched']}' in the filename",
                "evidence": {"level": r["sensitivity"], "kind": r["sens_kind"]},
                "snapshot": {"size": inv[p]["size"], "mtime": inv[p]["mtime"]},
            })
            claimed.add(p)

    # --- Exclusion filter --------------------------------------------------
    manual = {os.path.realpath(os.path.expanduser(x)) for x in args.exclude_op}
    kept = []
    for o in ops:
        if o["src"] in sensitive_paths:
            excluded_sensitive.append(
                {"path": o["src"], "why": "sensitive file — handled separately"})
        elif o["src"] in manual:
            excluded_sensitive.append(
                {"path": o["src"], "why": "excluded by the user"})
        else:
            kept.append(o)
    ops = kept

    # --- Summary -----------------------------------------------------------
    by_action = defaultdict(lambda: {"count": 0, "bytes": 0})
    new_dirs = set()
    for o in ops:
        a = by_action[o["action"]]
        a["count"] += 1
        a["bytes"] += o["snapshot"]["size"]
        new_dirs.add(os.path.dirname(o["dst"]))
    new_dirs = {d for d in new_dirs if not os.path.isdir(d)}

    plan = {
        # Declare dest only when an archive actually exists. Recording a
        # destination that is never written to would let undo follow a path
        # that has nothing to do with this run.
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
