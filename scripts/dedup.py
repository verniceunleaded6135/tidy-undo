#!/usr/bin/env python3
"""Duplicate and version-lineage detection — takes the inventory JSONL and
finds the relationships in it.

It produces three things:
  1. exact   : groups of byte-for-byte duplicates
  2. folder  : cloned folder pairs (how much of A is contained in B)
  3. version : version-lineage clusters of one original (v3/v7/최종/3교본/(1))

Three-stage hashing (size -> first 64KB -> full file) keeps full-file hashing
to a minimum. It never modifies a file.
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

# --- Rules for stripping version markers (including Korean user conventions) --
# Order matters: longer patterns are removed first.
# NOTE: the Korean literals below (사본 = "copy", 교본 = "proof/revision",
# 최종본/최종 = "final", 수정본/수정 = "revised", 보완 = "supplemented",
# 개정 = "amended", 초안/초고 = "draft") are matched against real Korean
# filenames. They are functional patterns, not translatable UI text.
# Korean tokens are delimited only by spaces, underscores or hyphens. Cutting
# mid-word turns "과보완료" into "과료", producing a bogus key that clusters
# unrelated files into one lineage. So every rule anchors to "after a
# separator" or "at end of string".
_SEP = r"(?:[_\-\s]|^)"
_END = r"(?=[_\-\s]|$)"

VERSION_PATTERNS = [
    r"\s*\(\d+\)" + _END,                        # "보고서 (1)"  re-download
    _SEP + r"copy(?:\s*\d+)?" + _END,
    _SEP + r"사본(?:\s*\d+)?" + _END,
    r"\s+\d{1,2}$",                                # "kit 2"  Finder duplicate
    _SEP + r"v\.?\s*\d+(?:\.\d+)*" + _END,        # v7, v1.2
    _SEP + r"\d+\s*교본" + _END,                     # 3교본
    _SEP + r"(?:최종본|최종|파이널|final|FINAL|Final)" + _END,
    _SEP + r"(?:수정본|수정|보완|개정|초안|초고|draft|DRAFT)" + _END,
    _SEP + r"\d{6,8}" + _END,                        # 260812 date stamp
    _SEP + r"\d{4}[-.]\d{2}[-.]\d{2}" + _END,
    _SEP + r"(?:rev|REV)\.?\s*\d+" + _END,
]
VERSION_RE = [re.compile(p) for p in VERSION_PATTERNS]

# Extension sets to ignore in lineage judgement (format variants of one document)
FORMAT_SIBLINGS = [
    {"hwp", "hwpx", "docx", "doc", "pdf"},
    {"pptx", "ppt", "pdf", "key"},
    {"xlsx", "xls", "csv"},
    {"md", "txt"},
]


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def canon_stem(stem: str) -> str:
    """The "work title" with version markers stripped off. This becomes the
    key of a lineage cluster."""
    s = nfc(stem)
    prev = None
    while prev != s:               # strip nested markers (v2 (1) 최종) repeatedly
        prev = s
        for r in VERSION_RE:
            s = r.sub(" ", s)
    s = re.sub(r"[_\-\s]+", " ", s).strip(" _-.")
    return s.lower()


# A path carrying one of these marks is a copy — the trace of a Finder
# duplicate or a browser re-download.
# Finder appends " 2" to the end of the name, and for a file the extension
# follows it. Looking only at the end of the path or at a slash would miss
# `보고서 2.pdf`, swapping the original and the copy.
# Underscore+digit (`_v5_1.hwp`) is excluded — Finder always uses a space, and
# a digit after an underscore is nearly always part of a version number.
# (사본 = "copy", 복사본 = "duplicate": literal Korean filename patterns.)
COPY_MARK_RE = re.compile(
    r"(?: \d{1,2}(?=\.[^./]{1,8}$|/|$)"   # "혁신국 2/", "보고서 2.pdf"
    r"|\s*\(\d+\)"                          # "보고서 (1)"  re-download
    r"|[_\-\s]사본"
    r"|[_\-\s]copy"
    r"|[_\-\s]복사본)"
)


def copy_marks(path: str) -> int:
    """How many times a duplicate mark appears across the whole path
    (parent folders included)."""
    return len(COPY_MARK_RE.findall(nfc(path)))


# Names generated automatically by a device or an app, not chosen by a person.
# (스크린샷 = "screenshot", 화면 캡처 = "screen capture",
#  제목 없음 = "untitled": literal Korean filename patterns.)
AUTO_NAME_RE = re.compile(
    r"^(kakaotalk[_-]|img[_-]?\d|dsc[_-]?\d|photo[_-]?\d|image[_-]?\d|"
    r"screenshot|스크린샷|화면\s?캡처|download(\s?\(\d+\))?$|"
    r"unnamed|untitled|제목\s?없음|[0-9a-f]{16,}$|\d{9,}(_\d+)*$)", re.I)

# The date-prefix convention this user follows: 260812_ or 20260812_
DATE_PREFIX_RE = re.compile(r"^(\d{6}|\d{8})(?=[_\-\s])")


def is_auto_name(stem: str) -> bool:
    return bool(AUTO_NAME_RE.match(nfc(stem).strip()))


# Names such as an arXiv ID, a UUID or a hash, which say nothing about content.
OPAQUE_NAME_RE = re.compile(
    r"^(?:\d{4}\.\d{4,5}(v\d+)?|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]+"
    r"|[0-9a-f]{16,}|[0-9a-f]{6,}[-_](en|ko|fr|de|es)|ssrn[-_]?id\d+"
    r"|\d{1,3})$", re.I)


def is_opaque_name(name: str) -> bool:
    """This name does not tell you what the file is.

    When `2508.09736v1` and `Seeing, Listening, Remembering...` hold the same
    content, keeping the shorter one throws away a title someone took the
    trouble to write. Descriptive power outranks length.

    Takes the whole name, not the stem. For a name like `2508.09736v1` the dot
    inside the name is mistaken for an extension separator, the stem is cut
    down to `2508`, and the judgement collapses.
    """
    n = nfc(name).strip()
    if OPAQUE_NAME_RE.match(n):
        return True
    m = re.match(r"^(.*)\.[0-9A-Za-z]{1,5}$", n)   # peel off only a real extension
    return bool(m and OPAQUE_NAME_RE.match(m.group(1)))


def date_prefix(stem: str) -> int:
    """The date prefix as a comparable integer. 0 when there is none."""
    m = DATE_PREFIX_RE.match(nfc(stem))
    if not m:
        return 0
    d = m.group(1)
    return int("20" + d) if len(d) == 6 else int(d)


def canonical_rank(r: dict) -> tuple:
    """Whatever sorts first is the canonical file (keep). Lower = more canonical.

    Each priority is there for a reason.
    1) Duplicate marks — `프로젝트자료` and `~ 2` have identical mtime
       and depth, so without looking at the mark we would mistake the original
       for the copy.
    2) Auto-generated names — when `KakaoTalk_Photo_2026-04-02….jpeg` and
       `프로필사진.jpeg` are the same file, the human-chosen name has to
       survive. Pick by mtime instead and the KakaoTalk original, received
       first, wins — so after the cleanup, searching for "프로필" finds nothing.
    3) Date prefix — under this user's `260812_` convention, the later date is
       the newer copy.
    """
    p = r["path"]
    stem = r.get("stem") or os.path.splitext(os.path.basename(p))[0]
    return (
        copy_marks(p),                 # the one without a duplicate mark is the original
        is_auto_name(stem),            # keep the human-chosen name
        is_opaque_name(os.path.basename(p)),  # keep the name that describes the content
        -date_prefix(stem),            # for date prefixes, later is newer
        p.count("/"),                  # prefer the shallower path
        r["mtime"],                    # still tied: the one created first
        len(r["name"]),
        p,                             # deterministic order on a total tie
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
    """Three stages: size -> head hash -> full hash.
    Returns (list of groups, path -> full hash)."""
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
    """Cloned folder pairs. What share of A's files exist in B under the same hash."""
    # Build the per-folder hash set from files whose hash we know
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
            if max(cov_a, cov_b) < 0.8:      # only when one side is >=80% inside the other
                continue
            # Which side is the copy: decide by duplicate mark -> containment
            # -> path depth, in that order.
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
    """Pull the version number out of the filename. v2.1 -> (2,1); empty tuple
    when there is none.

    This outranks mtime. A file's timestamp is refreshed by a re-download, a
    format conversion, or even just opening it, whereas the `v7` in the name is
    an ordering someone wrote by hand. In practice `_v7.hwpx` often has an
    earlier mtime than `_v4.pdf`.
    """
    s = nfc(stem)
    # Return them padded to a fixed width. Comparing (2,) against (2,1)
    # directly makes Python order the shorter tuple first, so v2 would come out
    # newer than v2.1.
    def pad(t):
        return tuple(t) + (0,) * (4 - len(t))

    m = VERSION_NUM_RE.search(s)
    if m:
        return pad([int(x) for x in re.split(r"[._]", m.group(1))][:4])
    m = SEQ_WORD_RE.search(s)
    if m:
        return pad([int(m.group(1))])
    if re.search(r"(?:^|[_\-\s])(최종본|최종|final)(?=[_\-\s.]|$)", s, re.I):
        return pad([9999])                 # "최종" (final) sorts after unnumbered ones
    return ()


def newest_first(r: dict) -> tuple:
    """Sort so the newest comes first. Lower = newer."""
    stem = r.get("stem") or os.path.splitext(os.path.basename(r["path"]))[0]
    v = version_tuple(stem)
    return (
        copy_marks(r["path"]) > 0,     # a '(1)' copy can never be the newest
        -(len(v) > 0),                 # look at the side carrying a version marker first
        tuple(-x for x in v),          # the higher version is newer
        -date_prefix(stem),            # then the date prefix
        -r["mtime"],                   # the file timestamp only as a last resort
    )


def find_versions(recs: list[dict], min_members: int = 2) -> list[dict]:
    """Version-lineage clusters: names that become identical once the version
    markers are stripped."""
    clusters: dict[tuple, list[dict]] = defaultdict(list)
    for r in recs:
        if r.get("kind") != "file":
            continue
        c = canon_stem(r["stem"])
        if len(c) < 4:                      # very short keys produce many false positives
            continue
        clusters[(r["parent"], c)].append(r)

    out = []
    for (parent, key), members in clusters.items():
        if len(members) < min_members:
            continue
        # Same name + different extension is one document in two formats, not
        # an old version. Sweep away the pdf you exported from the hwp as an
        # "old version" and the user loses the copy meant for distribution.
        # So group by stem first and keep only a representative of each.
        by_stem: dict[str, list[dict]] = defaultdict(list)
        for m in members:
            by_stem[nfc(m["stem"])].append(m)
        reps = [sorted(g, key=newest_first)[0] for g in by_stem.values()]
        siblings = [m for g in by_stem.values() for m in sorted(g, key=newest_first)[1:]]

        exts = {m["ext"] for m in members}
        if len(by_stem) == 1:
            kind = "format_variants"        # only one name — this is not version sprawl
            ms = sorted(members, key=newest_first)
            older = []                      # format variants are never folded away
        else:
            kind = "version_lineage"
            ms = sorted(reps, key=newest_first)
            # When folding an old version away, take its other format editions along
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
    ap = argparse.ArgumentParser(
        description="Duplicate and version-lineage detection (read-only)")
    ap.add_argument("inventory", help="the JSONL produced by scan.py")
    ap.add_argument("-o", "--out", required=True, help="result JSON path")
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
