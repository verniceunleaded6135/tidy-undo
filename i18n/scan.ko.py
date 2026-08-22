#!/usr/bin/env python3
"""인벤토리 스캐너 — 대상 디렉토리를 순회해 JSONL 인벤토리를 만든다.

LLM을 태우지 않는 결정적 단계. 파일을 절대 수정하지 않는다(읽기 전용).
출력 1줄 = 항목 1개의 사실(fact). 분류·판단은 이후 단계의 몫이다.

경로 표기 규약 (중요):
  path      : os.scandir가 준 문자열 그대로. 파일을 여는 데 쓰는 유일한 값.
  path_nfc  : 비교·표시·중복탐지용 NFC 정규화본. 이걸로 파일을 열면 안 된다.
  macOS는 한글 파일명을 NFD(자소분리)로 저장한다. APFS는 정규화를 무시하고
  비교해주지만 exFAT·SMB·일부 외장 볼륨은 그렇지 않다. 원본을 잃지 않는다.
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

# --- 무조건 건너뛰는 디렉토리 (이름만으로 기계 생성물이 확실한 것) --------
ALWAYS_SKIP = {
    ".git", ".svn", ".hg", ".jj", "node_modules", ".venv", "venv",
    "__pycache__", ".Trash", "site-packages", ".terraform", ".gradle",
    ".m2", ".pnpm-store", ".next", ".nuxt", ".svelte-kit", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", "Pods", ".stack-work",
    ".fseventsd", ".Spotlight-V100", ".TemporaryItems", ".DocumentRevisions-V100",
}

# --- 조건부 skip: 형제로 프로젝트 마커가 있을 때만 빌드 산출물로 본다 ------
# "build", "dist", "Library" 는 사용자가 만든 평범한 폴더 이름이기도 하다.
# 무조건 건너뛰면 ~/Downloads/build 같은 실제 자료 폴더가 조용히 누락된다.
CONTEXTUAL_SKIP = {"build", "dist", "out", "target", "Library", ".cache",
                   "coverage", "vendor", "env"}

# 이 마커를 가진 디렉토리는 '불가분 단위'다. 안으로 들어가 파일을 흩뿌리면
# git 워킹트리나 빌드 설정이 깨진다. 통째로 1행만 기록하고 진입하지 않는다.
PROJECT_MARKERS = {
    ".git", ".hg", ".svn", "package.json", "pyproject.toml", "setup.py",
    "Cargo.toml", "go.mod", "Gemfile", "requirements.txt", "CLAUDE.md",
    ".claude", "Makefile", "CMakeLists.txt", "tsconfig.json", "build.gradle",
    "pom.xml",
}

# --- macOS 패키지: 디렉토리처럼 보이지만 문서/앱 1개다 --------------------
BUNDLE_SUFFIXES = {
    ".app", ".framework", ".bundle", ".plugin", ".kext", ".prefpane",
    ".qlgenerator", ".mdimporter", ".component", ".saver", ".wdgt", ".xpc",
    ".rtfd", ".pages", ".numbers", ".key", ".band", ".logicx", ".fcpbundle",
    ".imovielibrary", ".theater", ".photoslibrary", ".aplibrary",
    ".musiclibrary", ".tvlibrary", ".abbu", ".mpkg", ".pkg", ".sparsebundle",
    ".sparseimage", ".lrcat", ".xcodeproj", ".xcworkspace", ".playground",
    ".scptd", ".download", ".workflow", ".migpkg",
}

# --- 인벤토리에서 제외할 시스템 잔여물 ------------------------------------
NOISE_NAMES = {".DS_Store", ".localized", "Icon\r", "Thumbs.db", "desktop.ini",
               ".apdisk", ".VolumeIcon.icns"}
# 작업 중일 가능성이 높아 손대면 안 되는 확장자
INFLIGHT_SUFFIXES = {".crdownload", ".download", ".part", ".partial", ".tmp",
                     ".!ut", ".swp", ".swo"}

SF_DATALESS = 0x40000000       # <sys/stat.h> — iCloud 미다운로드 placeholder
INFLIGHT_WINDOW = 90           # 초. 이보다 최근에 쓰인 파일은 진행 중으로 본다

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
    """앞 64KB + 크기로 만드는 값싼 지문. 중복 후보를 좁히는 데만 쓴다."""
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    try:
        with open(path, "rb") as f:
            h.update(f.read(nbytes))
    except OSError:
        return ""
    return h.hexdigest()


def is_bundle(entry_path: str, name: str, is_dir: bool) -> bool:
    """확장자 또는 Info.plist 구조로 macOS 패키지를 판별한다.

    확장자 목록만으로는 부족하다. 서드파티 앱이 만든 패키지는 임의 확장자를
    쓰면서 Contents/Info.plist 를 갖는다. 여기서 놓치면 스캐너가 문서 하나를
    수백 개 리소스 파일로 분해해 인벤토리에 올린다.
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
    """번들·프로젝트를 불가분 단위로, 보호 디렉토리는 통째로 건너뛰며 순회.

    심볼릭 링크는 따라가지 않는다(링크 자체를 1항목으로 기록). 순환 링크와
    스코프 밖 실체 접근을 동시에 막는다.
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
    ap = argparse.ArgumentParser(description="읽기 전용 파일 인벤토리 스캐너")
    ap.add_argument("roots", nargs="+", help="스캔할 디렉토리")
    ap.add_argument("-o", "--out", required=True, help="출력 JSONL 경로")
    ap.add_argument("--max-depth", type=int, default=6,
                    help="하위 탐색 깊이 (-1이면 무제한, 기본 6)")
    ap.add_argument("--min-size", type=int, default=0, help="이 크기 미만 제외(byte)")
    ap.add_argument("--no-hash", action="store_true", help="지문 계산 생략")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    now = int(__import__("time").time())

    with out.open("w", encoding="utf-8") as fh:
        for root in args.roots:
            rp = os.path.realpath(os.path.expanduser(root))
            if not os.path.isdir(rp):
                print(f"[!] 디렉토리가 아님: {rp}", file=sys.stderr)
                continue
            for item in walk(rp, args.max_depth):
                kind, p = item["kind"], item["path"]
                counts[kind] = counts.get(kind, 0) + 1
                if kind == "error":
                    # 권한 거부(TCC)로 못 읽은 디렉토리를 카운트에만 남기면,
                    # 후속 단계는 JSONL 만 읽으므로 그 폴더가 통째로 사라진
                    # 사실을 아무도 모른다. 한 줄로 남겨 하류가 볼 수 있게 한다.
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

                # 디렉토리는 st_blocks가 0인 것이 정상이므로 일반 파일에만 적용한다.
                dataless = bool(getattr(st, "st_flags", 0) & SF_DATALESS) or (
                    kind == "file" and st.st_size > 0
                    and getattr(st, "st_blocks", 1) == 0)

                rec = {
                    "path": p,                       # 원본 그대로 — 파일을 여는 값
                    "path_nfc": nfc(p),              # 비교·표시 전용
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
                    "nlink": st.st_nlink,            # >1이면 하드링크. 복사 금지
                    "mode": stat.filemode(st.st_mode),
                    "writable": os.access(p, os.W_OK),
                    "nfd_name": nfc(name) != name,   # 디스크가 NFD로 보관 중
                    "weird_name": any(c in name for c in "\n\r\t"),
                    "dataless": dataless,            # iCloud 미다운로드. 열지 말 것
                    "inflight": (os.path.splitext(name_n)[1].lower() in INFLIGHT_SUFFIXES
                                 or name_n.startswith("~$")
                                 or (now - int(st.st_mtime)) < INFLIGHT_WINDOW),
                }
                if kind == "symlink":
                    try:
                        rec["link_target"] = os.readlink(p)
                    except OSError:
                        rec["link_target"] = None

                # dataless 파일을 읽으면 iCloud가 실제 다운로드를 시작한다.
                # 수십 GB를 유발할 수 있으므로 절대 열지 않는다.
                if (not args.no_hash and kind == "file" and size > 0
                        and not dataless and not rec["inflight"]):
                    rec["hhash"] = head_hash(p, size)

                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(json.dumps({"inventory": str(out), "counts": counts},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
