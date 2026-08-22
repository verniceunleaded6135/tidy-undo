#!/usr/bin/env python3
"""실행기 — 승인된 계획(plan.json)만 실행하고, 완전한 원복 매니페스트를 남긴다.

설계 공리:
  A1  영구 삭제 연산이 이 파일에 존재하지 않는다. rm/remove/rmtree 없음.
      "삭제"는 격리 폴더로의 이동이며, 휴지통은 별도 명령이다.
  A2  계획 → 승인 → 실행이 분리된다. --token 없이는 아무것도 실행되지 않고,
      토큰은 계획 파일의 해시라서 승인 후 계획을 고치면 즉시 거부된다.
  A3  매니페스트만 있으면 이 코드가 없어도 100% 원복된다.
  A4  셸 문자열을 조립하지 않는다. 모든 연산은 os/shutil 직접 호출.

사용:
  python3 apply.py verify  plan.json                 # 승인 토큰 계산 + 사전검사
  python3 apply.py run     plan.json --token <hash>  # 실행
  python3 apply.py undo    manifest.json             # 완전 원복
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import unicodedata
from datetime import datetime, timezone

SCHEMA = "cleaner/manifest/1.0"
TMP_PREFIX = ".cleaner-tmp-"


class Refuse(Exception):
    """실행을 거부한다. 부분 실행보다 아무것도 안 하는 편이 안전한 경우."""


# --------------------------------------------------------------------------
# 경로 안전
# --------------------------------------------------------------------------
def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def case_insensitive(root: str) -> bool:
    """대소문자 무시 파일시스템인지 런타임 프로브로 판정한다.

    APFS 기본값은 무시지만 사용자가 대소문자 구분으로 포맷했을 수 있고,
    외장 볼륨은 제각각이다. 가정하면 A.pdf 가 a.pdf 를 덮어쓴다.
    """
    probe = os.path.join(root, TMP_PREFIX + "CaseProbe")
    try:
        with open(probe, "x"):
            pass
    except OSError:
        return True                     # 프로브 불가 시 보수적으로 '무시'로 본다
    try:
        return os.path.exists(os.path.join(root, TMP_PREFIX + "caseprobe"))
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


# 어떤 등급에서도 목적지가 될 수 없는 경로. dest 선언으로도 열리지 않는다.
FORBIDDEN_DEST = (
    "/System", "/Library", "/private", "/usr", "/bin", "/sbin", "/etc", "/var",
    "/Applications", "/cores", "/dev", "/opt",
)


def _resolve(target: str) -> str:
    """leaf 는 심링크일 수 있으므로 부모까지만 realpath 한다.

    leaf 까지 해소하면 "링크를 옮기는" 정상 작업이 링크 대상의 실체를 건드리는
    사고로 바뀐다.
    """
    parent_r = os.path.realpath(os.path.dirname(target))
    return os.path.join(parent_r, os.path.basename(target))


def _under(base: str, path: str) -> bool:
    a, b = nfc(path), nfc(os.path.realpath(base))
    return a == b or a.startswith(b + os.sep)


def within(root: str, target: str) -> str:
    """target이 root 하위임을 보장한다. 원본(src)에 쓰는 엄격한 검사."""
    final = _resolve(target)
    if not _under(root, final):
        raise Refuse(f"스코프 이탈: {target} → {final} "
                     f"(root={os.path.realpath(root)})")
    return final


def within_dest(root: str, dest: str | None, target: str) -> str:
    """목적지 검사. root 안이거나, 계획이 명시 선언한 dest 안이어야 한다.

    원본은 언제나 root 하나로 묶어두되, 결과물을 다른 디렉토리에 두고 싶은
    요구는 실재한다(예: 신원·금융 문서를 Downloads 가 아니라 Documents 로).
    그때도 '아무 데나'를 허용하면 스코프 가드가 무의미해지므로, 목적지는
    계획에 적힌 dest 한 곳으로만 열고 그 값 자체를 검증한다. 사용자는 승인
    화면에서 dest 절대경로를 보게 되므로, 어디로 나가는지 모르는 채 승인하는
    일이 생기지 않는다.
    """
    final = _resolve(target)
    if _under(root, final):
        return final
    if dest:
        dest_r = os.path.realpath(dest)
        home = os.path.realpath(os.path.expanduser("~"))
        if dest_r in ("/", home, "/Users", "/Volumes"):
            raise Refuse(f"목적지로 지정할 수 없는 경로: {dest_r}")
        if any(dest_r == f or dest_r.startswith(f + os.sep)
               for f in FORBIDDEN_DEST):
            raise Refuse(f"보호 경로는 목적지가 될 수 없다: {dest_r}")
        if _under(dest_r, final):
            return final
    raise Refuse(f"목적지가 root 밖이고 선언된 dest 안도 아니다: {final}")


def exists_ci(path: str, ci: bool) -> bool:
    """대소문자 무시 FS에서 os.path.exists는 A.pdf/a.pdf를 구분하지 못한다.

    형제 목록을 직접 비교해야 '이미 있는데 없다고 보고 덮어쓰는' 사고를 막는다.
    """
    if os.path.lexists(path):
        return True
    if not ci:
        return False
    parent, name = os.path.dirname(path), nfc(os.path.basename(path)).casefold()
    try:
        return any(nfc(e.name).casefold() == name for e in os.scandir(parent))
    except OSError:
        return False


def resolve_collision(dst: str, ci: bool) -> tuple[str, int]:
    """대상이 있으면 ' (2)', ' (3)' … 으로 비켜간다. 절대 덮어쓰지 않는다."""
    if not exists_ci(dst, ci):
        return dst, 0
    stem, ext = os.path.splitext(dst)
    for n in range(2, 1000):
        cand = f"{stem} ({n}){ext}"
        if not exists_ci(cand, ci):
            return cand, n
    h = hashlib.blake2b(dst.encode(), digest_size=4).hexdigest()
    return f"{stem}__{h}{ext}", -1


def move_noclobber(src: str, dst: str, ci: bool) -> tuple[str, str, int]:
    """덮어쓰기가 원천적으로 불가능한 이동. (최종경로, 방식, 충돌횟수) 반환.

    os.rename / shutil.move 는 대상이 있어도 조용히 덮어쓴다. 그래서 파일은
    os.link(EEXIST면 실패) + unlink 로 옮긴다 — 같은 볼륨에서 원자적이고,
    원본은 링크 성공이 확인된 뒤에만 사라진다.
    """
    final, collisions = resolve_collision(dst, ci)
    os.makedirs(os.path.dirname(final), exist_ok=True)

    src_st = os.lstat(src)
    dst_dev = os.stat(os.path.dirname(final)).st_dev

    if os.path.islink(src) or os.path.isdir(src):
        if exists_ci(final, ci):
            raise Refuse(f"대상이 이미 존재: {final}")
        os.rename(src, final)                       # 같은 볼륨 전제
        return final, "os.rename", collisions

    if src_st.st_dev == dst_dev:
        os.link(src, final)                         # 존재하면 FileExistsError
        os.unlink(src)
        return final, "os.link+unlink", collisions

    # 크로스볼륨: 임시본 → 검증 → 커밋 → 그제서야 원본 정리
    if src_st.st_nlink > 1:
        raise Refuse(f"하드링크 파일은 볼륨 간 이동 금지(링크가 끊긴다): {src}")
    tmp = os.path.join(os.path.dirname(final),
                       TMP_PREFIX + hashlib.blake2b(src.encode(),
                                                    digest_size=8).hexdigest())
    try:
        shutil.copy2(src, tmp)
        if os.path.getsize(tmp) != src_st.st_size:
            raise Refuse(f"복사 크기 불일치: {src}")
        os.link(tmp, final)
    except BaseException:
        # 디스크가 차서 복사가 터지면 잘린 임시본이 목적지에 남는다.
        # 원본은 아직 손대지 않았으므로 임시본만 치우면 무손실로 되돌아간다.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.unlink(tmp)
    os.unlink(src)
    return final, "copy2+verify", collisions


# --------------------------------------------------------------------------
# 계획 검증
# --------------------------------------------------------------------------
def _lock_holder(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _pid_alive(pid: int) -> bool:
    """그 PID 가 아직 살아 있는가. 신호 0 은 존재만 확인하고 아무 영향이 없다."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                      # 남의 프로세스지만 살아는 있다
    return True


def plan_token(plan: dict) -> str:
    """계획의 정본 해시. 승인 후 계획이 한 글자라도 바뀌면 토큰이 달라진다."""
    body = json.dumps(plan.get("operations", []), ensure_ascii=False,
                      sort_keys=True).encode()
    scope = plan.get("scope", {})
    head = (scope.get("root", "") + "\0" + (scope.get("dest") or "")).encode()
    return hashlib.sha256(head + b"\0" + body).hexdigest()[:12]


def preflight(plan: dict) -> dict:
    """실행 직전 검사. 하나라도 어긋나면 그 항목을 빼거나 전체를 거부한다."""
    root = plan["scope"]["root"]
    if not os.path.isdir(root):
        raise Refuse(f"스코프 root가 없다: {root}")
    real = os.path.realpath(root)
    home = os.path.realpath(os.path.expanduser("~"))
    if real in (home, "/", "/Users", "/Volumes") or real.startswith("/System"):
        raise Refuse(f"스코프 root로 지정할 수 없는 경로: {real}")

    dest = plan.get("scope", {}).get("dest")
    if dest:
        os.makedirs(dest, exist_ok=True)            # 검사 전에 존재해야 realpath 가 산다
    ci = case_insensitive(root)
    ok, skip = [], []
    xvol_bytes = 0
    for op in plan["operations"]:
        src = op["src"]
        if not os.path.lexists(src):
            skip.append({**op, "skip_reason": "src_missing"}); continue
        try:
            st = os.lstat(src)
        except OSError as e:
            skip.append({**op, "skip_reason": f"stat_failed:{e}"}); continue

        # 스캔 이후 파일이 바뀌었으면 계획의 전제가 무너진 것이다 (TOCTOU).
        snap = op.get("snapshot") or {}
        if snap and (st.st_size != snap.get("size") or
                     int(st.st_mtime) != snap.get("mtime")):
            skip.append({**op, "skip_reason": "src_changed_since_scan"}); continue
        if (time.time() - st.st_mtime) < 90:
            skip.append({**op, "skip_reason": "mtime_within_90s"}); continue
        if getattr(st, "st_flags", 0) & 0x40000000:
            skip.append({**op, "skip_reason": "dataless_icloud"}); continue

        try:
            within(root, src)                       # 원본은 언제나 root 안
            within_dest(root, dest, op["dst"])      # 목적지는 root 또는 dest
        except Refuse as e:
            skip.append({**op, "skip_reason": str(e)}); continue

        try:
            dst_parent = op["dst"]
            while not os.path.isdir(os.path.dirname(dst_parent)):
                dst_parent = os.path.dirname(dst_parent)
            if os.stat(os.path.dirname(dst_parent)).st_dev != st.st_dev:
                xvol_bytes += st.st_size
        except OSError:
            pass
        ok.append({**op, "snapshot": {"size": st.st_size,
                                      "mtime": int(st.st_mtime),
                                      "ino": st.st_ino}})

    if xvol_bytes:
        # 여유 공간은 '쓸 곳'을 봐야 한다. root(원본)를 보면 홈이 항상
        # 수백 GB 여유라 목적지가 꽉 차 있어도 가드가 통과해 버린다.
        probe = dest or root
        while probe and not os.path.isdir(probe):
            probe = os.path.dirname(probe)
        du = shutil.disk_usage(probe or root)
        # 여유분을 5GB 로 고정하면 USB·디스크이미지처럼 작은 목적지 볼륨은
        # 무엇을 옮기든 영원히 통과하지 못한다. 볼륨 크기에 비례시킨다.
        headroom = min(1 << 30, max(8 << 20, du.total // 20))
        need = int(xvol_bytes * 1.15) + headroom
        if du.free < need:
            raise Refuse(
                f"여유 공간 부족({probe or root}): 필요 "
                f"{need/1048576:.0f}MB(자료 {xvol_bytes/1048576:.0f}MB + "
                f"여유분 {headroom/1048576:.0f}MB), 가용 {du.free/1048576:.0f}MB")

    return {"ok": ok, "skipped": skip, "case_insensitive": ci,
            "cross_volume_bytes": xvol_bytes}


# --------------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------------
def run(plan_path: str, token: str, out_dir: str) -> int:
    plan = json.load(open(plan_path, encoding="utf-8"))
    expect = plan_token(plan)
    if token != expect:
        raise Refuse(f"승인 토큰 불일치. 계획이 승인 이후 변경되었다.\n"
                     f"  받은 값: {token}\n  계획 해시: {expect}")

    pre = preflight(plan)
    ci = pre["case_insensitive"]
    root = plan["scope"]["root"]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + expect[:6]
    rd = os.path.join(out_dir, run_id)
    os.makedirs(rd, exist_ok=True)

    lock = os.path.join(root, ".cleaner.lock")
    try:
        with open(lock, "x") as f:
            f.write(f"{os.getpid()} {run_id}\n")
    except FileExistsError:
        # 락을 잡은 프로세스가 아직 살아 있는지 확인한다. SIGKILL 로 죽은
        # 실행은 finally 를 돌지 못해 락을 남기는데, 그 뒤로 모든 실행이
        # 영구히 거부된다. 주인이 없는 락은 회수하는 것이 맞다.
        holder = _lock_holder(lock)
        if holder is not None and _pid_alive(holder):
            raise Refuse(f"다른 정리 작업이 진행 중이다 (PID {holder}): {lock}")
        print(f"[알림] 주인이 없는 락을 회수한다: {lock}", file=sys.stderr)
        try:
            os.unlink(lock)
            with open(lock, "x") as f:
                f.write(f"{os.getpid()} {run_id}\n")
        except OSError as e:
            raise Refuse(f"락을 회수하지 못했다: {e}")

    journal_path = os.path.join(rd, "journal.jsonl")
    ops_done: list[dict] = []
    created_dirs: list[str] = []

    def jwrite(fh, rec):
        """저널은 항상 디스크에 먼저 닿는다. 중단돼도 어디까지 갔는지 남는다."""
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())

    try:
        with open(journal_path, "w", encoding="utf-8") as jf:
            jwrite(jf, {"run_id": run_id, "plan": plan_path, "token": expect,
                        "root": root, "started": datetime.now().isoformat()})
            for seq, op in enumerate(pre["ok"], 1):
                src, dst = op["src"], op["dst"]
                jwrite(jf, {"seq": seq, "state": "pending",
                            "src": src, "dst_intended": dst})
                # 사전검사와 이 시점 사이에 원본이 사라졌을 수 있다. 앞선
                # 이동이 원인일 수도 있으므로(대소문자 무시 FS의 동일 파일 등)
                # 실패가 아니라 스킵으로 강등한다. 실패로 두면 정상 계획 전체가
                # 비정상 종료로 보고된다.
                if not os.path.lexists(src):
                    rec = {"seq": seq, "state": "skipped", "src": src,
                           "dst_intended": dst,
                           "skip_reason": "src_vanished_before_move"}
                    ops_done.append(rec)
                    jwrite(jf, rec)
                    continue
                try:
                    parent = os.path.dirname(dst)
                    if not os.path.isdir(parent):
                        os.makedirs(parent, exist_ok=True)
                        created_dirs.append(parent)
                    final, method, coll = move_noclobber(src, dst, ci)
                    rec = {
                        "seq": seq, "state": "done",
                        "action": op.get("action", "move"),
                        "src": src, "src_nfc": nfc(src),
                        "dst_intended": dst, "dst_actual": final,
                        "dst_actual_nfc": nfc(final),
                        "method": method, "collisions": coll,
                        "snapshot": op.get("snapshot"),
                        "reason": op.get("reason"),
                        # 계획의 근거를 그대로 보존한다. 나중에 "이 격리본의
                        # 정본이 아직 살아 있는가"를 확인하려면 필요한데,
                        # 앞선 판본은 이걸 버려서 사후 검증이 불가능했다.
                        "evidence": op.get("evidence"),
                        "at": datetime.now().isoformat(),
                    }
                    ops_done.append(rec)
                    jwrite(jf, rec)
                except Exception as e:                    # noqa: BLE001
                    rec = {"seq": seq, "state": "failed", "src": src,
                           "dst_intended": dst, "error": f"{type(e).__name__}: {e}",
                           "src_intact": os.path.lexists(src)}
                    ops_done.append(rec)
                    jwrite(jf, rec)
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass

    done = [o for o in ops_done if o["state"] == "done"]
    failed = [o for o in ops_done if o["state"] == "failed"]
    late_skips = [o for o in ops_done if o["state"] == "skipped"]
    manifest = {
        "$schema": SCHEMA, "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": plan["scope"], "plan_token": expect,
        "environment": {"case_insensitive_fs": ci, "python": sys.version.split()[0],
                        "platform": sys.platform},
        "totals": {"planned": len(plan["operations"]), "done": len(done),
                   "skipped": len(pre["skipped"]) + len(late_skips),
                   "failed": len(failed),
                   "bytes_moved": sum(o.get("snapshot", {}).get("size", 0)
                                      for o in done),
                   "dirs_created": len(created_dirs)},
        "created_dirs": created_dirs,
        "operations": ops_done,
        "skipped": pre["skipped"] + late_skips,
        "undo": {"command": f"python3 apply.py undo {rd}/manifest.json",
                 "order": "reverse_seq", "steps": len(done)},
    }
    mpath = os.path.join(rd, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(json.dumps({"run_id": run_id, "manifest": mpath,
                      "totals": manifest["totals"],
                      "undo": manifest["undo"]["command"]},
                     ensure_ascii=False, indent=2))
    return 0 if not failed else 1


# --------------------------------------------------------------------------
# 원복
# --------------------------------------------------------------------------
def manifest_from_journal(journal_path: str) -> dict:
    """저널만 남은 실행에서 되돌리기용 매니페스트를 복원한다.

    매니페스트는 정상 종료 시에만 쓰인다. 강제종료(SIGKILL)·전원 차단으로
    죽으면 이미 옮겨진 파일이 수백 건이어도 되돌릴 입력이 없어진다 —
    "언제나 되돌릴 수 있다"는 이 도구의 전제가 거기서 깨진다.
    저널은 이동 전마다 fsync 되므로, 그것만으로 복원할 수 있어야 한다.
    """
    head, done, pending = {}, {}, {}
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue                 # 중단 시 마지막 줄이 잘렸을 수 있다
            if "run_id" in r and "seq" not in r:
                head = r
            elif r.get("state") == "done":
                done[r["seq"]] = r
            elif r.get("state") == "pending":
                pending[r["seq"]] = r

    ops = list(done.values())
    # 이동은 마쳤는데 'done' 을 쓰기 직전에 죽은 항목이 정확히 한 건 생긴다.
    # 저널에는 pending 만 남는다. 버리면 그 파일 하나가 영영 제자리로
    # 돌아가지 못하므로, 복원 후보로 올리고 판단은 undo 의 사전조건에 맡긴다 —
    # 실제로 안 옮겨졌다면 목적지에 파일이 없어 자동으로 건너뛴다.
    for seq, r in pending.items():
        if seq in done:
            continue
        dst = r.get("dst_intended")
        if not dst:
            continue
        ops.append({**r, "state": "done", "dst_actual": dst,
                    "recovered_pending": True,
                    "snapshot": r.get("snapshot")})
    return {
        "$schema": SCHEMA,
        "run_id": head.get("run_id", "recovered"),
        "recovered_from_journal": True,
        "scope": {"root": head.get("root", "")},
        "environment": {"case_insensitive_fs": True},
        "totals": {"done": len(ops)},
        "created_dirs": [],              # 저널에 없다. 빈 폴더는 남겨둔다
        "operations": ops,
        "skipped": [],
    }


def resolve_manifest(path: str) -> dict:
    """매니페스트 경로 또는 실행 디렉토리를 받아 되돌리기 입력을 만든다."""
    if os.path.isdir(path):
        mp = os.path.join(path, "manifest.json")
        jp = os.path.join(path, "journal.jsonl")
    else:
        mp = path
        jp = os.path.join(os.path.dirname(path), "journal.jsonl")
    if os.path.isfile(mp):
        return json.load(open(mp, encoding="utf-8"))
    if os.path.isfile(jp):
        print(f"[알림] 매니페스트가 없어 저널에서 복원한다: {jp}", file=sys.stderr)
        return manifest_from_journal(jp)
    raise Refuse(f"되돌릴 기록이 없다: {path}")


def undo(manifest_path: str) -> int:
    m = resolve_manifest(manifest_path)
    ci = m.get("environment", {}).get("case_insensitive_fs", True)
    restored, blocked = [], []

    # 역순 재생. 순방향에서 만든 폴더 안으로 들어간 파일부터 빼내야
    # 폴더 정리가 성립한다.
    for op in sorted([o for o in m["operations"] if o["state"] == "done"],
                     key=lambda o: -o["seq"]):
        cur, orig = op["dst_actual"], op["src"]
        if not os.path.lexists(cur):
            blocked.append({**op, "why": "현재 위치에 파일이 없다(이미 옮겨졌나?)"})
            continue
        snap = op.get("snapshot") or {}
        if snap:
            st = os.lstat(cur)
            if st.st_size != snap.get("size"):
                blocked.append({**op, "why": "정리 이후 내용이 바뀌었다"})
                continue
        if exists_ci(orig, ci):
            blocked.append({**op, "why": "원래 자리에 다른 파일이 있다"})
            continue
        try:
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            final, _, _ = move_noclobber(cur, orig, ci)
            restored.append({"from": cur, "to": final})
        except Exception as e:                            # noqa: BLE001
            blocked.append({**op, "why": f"{type(e).__name__}: {e}"})

    # 이번 실행이 만든 빈 폴더만 정리한다. 원래 있던 폴더는 건드리지 않는다.
    removed = []
    for d in sorted(m.get("created_dirs", []), key=len, reverse=True):
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                removed.append(d)
        except OSError:
            pass

    print(json.dumps({"restored": len(restored), "blocked": len(blocked),
                      "dirs_removed": len(removed),
                      "blocked_detail": blocked[:20]},
                     ensure_ascii=False, indent=2))
    return 0 if not blocked else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="정리 계획 실행기 (삭제 기능 없음)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="승인 토큰 계산 + 사전 검사")
    v.add_argument("plan")
    r = sub.add_parser("run", help="승인된 계획 실행")
    r.add_argument("plan")
    r.add_argument("--token", required=True)
    r.add_argument("--runs-dir", default="_runs")
    u = sub.add_parser("undo", help="매니페스트로 완전 원복")
    u.add_argument("manifest")
    a = ap.parse_args()

    try:
        if a.cmd == "verify":
            plan = json.load(open(a.plan, encoding="utf-8"))
            pre = preflight(plan)
            print(json.dumps({
                "approval_token": plan_token(plan),
                "scope_root": plan["scope"]["root"],
                "scope_dest": plan["scope"].get("dest"),
                "will_move": len(pre["ok"]), "will_skip": len(pre["skipped"]),
                "cross_volume_bytes": pre["cross_volume_bytes"],
                "case_insensitive_fs": pre["case_insensitive"],
                "skip_reasons": sorted({s["skip_reason"] for s in pre["skipped"]}),
            }, ensure_ascii=False, indent=2))
            return 0
        if a.cmd == "run":
            return run(a.plan, a.token, a.runs_dir)
        return undo(a.manifest)
    except Refuse as e:
        print(f"[거부] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
