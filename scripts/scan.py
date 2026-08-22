#!/usr/bin/env python3
"""Inventory scanner — walks the target directories and builds a JSONL inventory.

A deterministic stage that burns no LLM tokens. It never modifies a file
(read-only). One output line = one fact about one entry. Classification and
judgement belong to later stages.

Path conventions (important):
  path      : exactly the string os.scandir gave us. The only value used to
              open a file.
  path_nfc  : an NFC-normalized copy for comparison, display and duplicate
              detection. Never open a file with this one.
  macOS stores Hangul filenames in NFD (decomposed jamo). APFS ignores
  normalization when comparing, but exFAT, SMB and some external volumes do
  not. Keeping both means we never lose the original.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import unicodedata
from pathlib import Path

# --- Directories skipped unconditionally (the name alone proves they are
#     machine-generated) ------------------------------------------------------
ALWAYS_SKIP = {
    ".git", ".svn", ".hg", ".jj", "node_modules", ".venv", "venv",
    "__pycache__", ".Trash", "site-packages", ".terraform", ".gradle",
    ".m2", ".pnpm-store", ".next", ".nuxt", ".svelte-kit", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", "Pods", ".stack-work",
    ".fseventsd", ".Spotlight-V100", ".TemporaryItems", ".DocumentRevisions-V100",
}

# --- Conditional skip: treated as build output only when a project marker
#     sits alongside them --------------------------------------------------
# "build", "dist" and "Library" are also perfectly ordinary folder names a
# person might create. Skipping them unconditionally would silently drop a
# real content folder such as ~/Downloads/build.
CONTEXTUAL_SKIP = {"build", "dist", "out", "target", "Library", ".cache",
                   "coverage", "vendor", "env"}

# A directory carrying one of these markers is an indivisible unit. Descending
# into it and scattering its files would break the git working tree or the
# build configuration. We record it as a single line and do not enter.
PROJECT_MARKERS = {
    ".git", ".hg", ".svn", "package.json", "pyproject.toml", "setup.py",
    "Cargo.toml", "go.mod", "Gemfile", "requirements.txt", "CLAUDE.md",
    ".claude", "Makefile", "CMakeLists.txt", "tsconfig.json", "build.gradle",
    "pom.xml",
}

# --- macOS packages: they look like directories but are one document or app --
BUNDLE_SUFFIXES = {
    ".app", ".framework", ".bundle", ".plugin", ".kext", ".prefpane",
    ".qlgenerator", ".mdimporter", ".component", ".saver", ".wdgt", ".xpc",
    ".rtfd", ".pages", ".numbers", ".key", ".band", ".logicx", ".fcpbundle",
    ".imovielibrary", ".theater", ".photoslibrary", ".aplibrary",
    ".musiclibrary", ".tvlibrary", ".abbu", ".mpkg", ".pkg", ".sparsebundle",
    ".sparseimage", ".lrcat", ".xcodeproj", ".xcworkspace", ".playground",
    ".scptd", ".download", ".workflow", ".migpkg",
}

# --- System leftovers excluded from the inventory -------------------------
NOISE_NAMES = {".DS_Store", ".localized", "Icon\r", "Thumbs.db", "desktop.ini",
               ".apdisk", ".VolumeIcon.icns"}
# Extensions that most likely mean work in progress — do not touch these.
INFLIGHT_SUFFIXES = {".crdownload", ".download", ".part", ".partial", ".tmp",
                     ".!ut", ".swp", ".swo"}

SF_DATALESS = 0x40000000       # <sys/stat.h> — iCloud not-yet-downloaded placeholder
INFLIGHT_WINDOW = 90           # seconds. Written more recently than this = in progress

CATEGORY_BY_EXT = {
    "pdf": "doc", "hwp": "doc", "hwpx": "doc", "docx": "doc", "doc": "doc",
    "rtf": "doc", "odt": "doc", "pages": "doc", "epub": "doc",
    "txt": "text", "md": "text", "markdown": "text",
    "csv": "data", "tsv": "data", "json": "config", "yaml": "config",
    "yml": "config", "toml": "config", "xml": "config",
    "xlsx": "sheet", "xls": "sheet", "numbers": "sheet",
    "pptx": "deck", "ppt": "deck", "key": "deck",
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image",
    "heic": "image", "webp": "image", "svg": "image", "tiff": "image",
    "bmp": "image", "psd": "image", "ai": "image",
    "mp4": "video", "mov": "video", "avi": "video", "mkv": "video",
    "webm": "video", "mp3": "audio", "wav": "audio", "m4a": "audio",
    "flac": "audio", "aac": "audio",
    "zip": "archive", "tar": "archive", "gz": "archive", "tgz": "archive",
    "bz2": "archive", "xz": "archive", "7z": "archive", "rar": "archive",
    "dmg": "installer", "pkg": "installer", "iso": "installer",
    "py": "code", "js": "code", "ts": "code", "tsx": "code", "jsx": "code",
    "sh": "code", "rb": "code", "go": "code", "rs": "code", "java": "code",
    "c": "code", "cpp": "code", "h": "code", "swift": "code", "sql": "code",
    "html": "web", "css": "web",
}


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def head_hash(path: str, size: int, nbytes: int = 65536) -> str:
    """Cheap fingerprint from the first 64KB plus the size. Used only to
    narrow down duplicate candidates."""
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    try:
        with open(path, "rb") as f:
            h.update(f.read(nbytes))
    except OSError:
        return ""
    return h.hexdigest()


def is_bundle(entry_path: str, name: str, is_dir: bool) -> bool:
    """Identify a macOS package by extension or by Info.plist structure.

    The extension list alone is not enough. Packages created by third-party
    apps use arbitrary extensions while still carrying Contents/Info.plist.
    Miss one here and the scanner shreds a single document into hundreds of
    resource files in the inventory.
    """
    if not is_dir:
        return False
    if os.path.splitext(name)[1].lower() in BUNDLE_SUFFIXES:
        return True
    return (os.path.exists(os.path.join(entry_path, "Contents", "Info.plist"))
            or os.path.exists(os.path.join(entry_path, "Info.plist")))


def has_project_marker(dir_path: str) -> bool:
    try:
        names = {e.name for e in os.scandir(dir_path)}
    except OSError:
        return False
    return bool(names & PROJECT_MARKERS)


def walk(root: str, max_depth: int):
    """Walk, treating bundles and projects as indivisible and skipping
    protected directories whole.

    Symlinks are never followed (the link itself is recorded as one entry).
    That blocks symlink cycles and out-of-scope access to the real target at
    the same time.
    """
    seen: set[tuple[int, int]] = set()
    stack = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except (PermissionError, OSError) as e:
            yield {"kind": "error", "path": cur, "note": str(e)}
            continue
        for e in entries:
            p, name = e.path, e.name
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                is_link = e.is_symlink()
            except OSError:
                continue

            if is_link:
                yield {"kind": "symlink", "path": p}
                continue

            if is_dir:
                if is_bundle(p, name, True):
                    yield {"kind": "bundle", "path": p}
                elif name in ALWAYS_SKIP or name.startswith("."):
                    yield {"kind": "skipped_dir", "path": p, "note": "always_skip"}
                elif name in CONTEXTUAL_SKIP and has_project_marker(cur):
                    yield {"kind": "skipped_dir", "path": p, "note": "build_output"}
                elif has_project_marker(p):
                    yield {"kind": "project", "path": p, "note": "project_marker"}
                elif max_depth >= 0 and depth >= max_depth:
                    yield {"kind": "depth_limit", "path": p}
                else:
                    try:
                        st = e.stat(follow_symlinks=False)
                        key = (st.st_dev, st.st_ino)
                    except OSError:
                        continue
                    if key in seen:
                        continue
                    seen.add(key)
                    stack.append((p, depth + 1))
            else:
                if name in NOISE_NAMES or name.startswith("._"):
                    yield {"kind": "noise", "path": p}
                else:
                    yield {"kind": "file", "path": p}


def dir_size(p: str) -> int:
    total = 0
    for dirpath, _d, filenames in os.walk(p, followlinks=False):
        for fn in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, fn)).st_size
            except OSError:
                pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only file inventory scanner")
    ap.add_argument("roots", nargs="+", help="directories to scan")
    ap.add_argument("-o", "--out", required=True, help="output JSONL path")
    ap.add_argument("--max-depth", type=int, default=6,
                    help="descent depth (-1 for unlimited, default 6)")
    ap.add_argument("--min-size", type=int, default=0,
                    help="exclude files smaller than this (bytes)")
    ap.add_argument("--no-hash", action="store_true", help="skip fingerprinting")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    now = int(__import__("time").time())

    with out.open("w", encoding="utf-8") as fh:
        for root in args.roots:
            rp = os.path.realpath(os.path.expanduser(root))
            if not os.path.isdir(rp):
                print(f"[!] not a directory: {rp}", file=sys.stderr)
                continue
            for item in walk(rp, args.max_depth):
                kind, p = item["kind"], item["path"]
                counts[kind] = counts.get(kind, 0) + 1
                if kind == "error":
                    # If a directory we could not read (TCC denial) survived
                    # only as a counter, nobody downstream would ever learn
                    # that the folder vanished wholesale — later stages read
                    # the JSONL only. Leave a line so they can see it.
                    fh.write(json.dumps({
                        "path": p, "path_nfc": nfc(p), "kind": "error",
                        "name": nfc(os.path.basename(p)), "note": item.get("note"),
                        "unreadable": True,
                    }, ensure_ascii=False) + "\n")
                    continue
                if kind in ("noise", "depth_limit"):
                    continue

                try:
                    st = os.lstat(p)
                except OSError:
                    counts["error"] = counts.get("error", 0) + 1
                    continue

                name = os.path.basename(p)
                name_n = nfc(name)
                ext = name_n.rsplit(".", 1)[-1].lower() if "." in name_n[1:] else ""
                size = dir_size(p) if kind in ("bundle", "project") else st.st_size

                if kind == "file" and size < args.min_size:
                    counts["too_small"] = counts.get("too_small", 0) + 1
                    continue

                # st_blocks == 0 is normal for a directory, so apply this test
                # to regular files only.
                dataless = bool(getattr(st, "st_flags", 0) & SF_DATALESS) or (
                    kind == "file" and st.st_size > 0
                    and getattr(st, "st_blocks", 1) == 0)

                rec = {
                    "path": p,                       # verbatim — the value used to open
                    "path_nfc": nfc(p),              # comparison and display only
                    "name": name_n,
                    "stem": nfc(os.path.splitext(name)[0]),
                    "ext": ext,
                    "kind": kind,                    # file|bundle|project|symlink
                    "category": CATEGORY_BY_EXT.get(ext, "other") if kind == "file"
                                else kind,
                    "size": size,
                    "mtime": int(st.st_mtime),
                    "ctime": int(st.st_ctime),
                    "atime": int(st.st_atime),
                    "parent": os.path.dirname(p),
                    "parent_nfc": nfc(os.path.dirname(p)),
                    "dev": st.st_dev,
                    "ino": st.st_ino,
                    "nlink": st.st_nlink,            # >1 means a hard link. Never copy
                    "mode": stat.filemode(st.st_mode),
                    "writable": os.access(p, os.W_OK),
                    "nfd_name": nfc(name) != name,   # the disk holds it as NFD
                    "weird_name": any(c in name for c in "\n\r\t"),
                    "dataless": dataless,            # iCloud, not downloaded. Do not open
                    "inflight": (os.path.splitext(name_n)[1].lower() in INFLIGHT_SUFFIXES
                                 or name_n.startswith("~$")
                                 or (now - int(st.st_mtime)) < INFLIGHT_WINDOW),
                }
                if kind == "symlink":
                    try:
                        rec["link_target"] = os.readlink(p)
                    except OSError:
                        rec["link_target"] = None

                # Reading a dataless file makes iCloud start the real download.
                # That can pull down tens of GB, so we never open one.
                if (not args.no_hash and kind == "file" and size > 0
                        and not dataless and not rec["inflight"]):
                    rec["hhash"] = head_hash(p, size)

                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(json.dumps({"inventory": str(out), "counts": counts},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
