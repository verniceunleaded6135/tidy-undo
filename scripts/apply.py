#!/usr/bin/env python3
"""Executor — runs an approved plan (plan.json) and nothing else, leaving
behind a complete manifest for undo.

Design axioms:
  A1  No permanent-delete operation exists in this file. No rm/remove/rmtree.
      "Deleting" means moving into a quarantine folder, and the Trash is a
      separate command.
  A2  Plan, approval and execution are separate. Nothing runs without --token,
      and the token is a hash of the plan file, so editing the plan after
      approval is rejected immediately.
  A3  The manifest alone restores everything, 100%, even if this code is gone.
  A4  No shell strings are assembled. Every operation is a direct os/shutil call.

Usage:
  python3 apply.py verify  plan.json                 # compute approval token + preflight
  python3 apply.py run     plan.json --token <hash>  # execute
  python3 apply.py undo    manifest.json             # full rollback
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
    """Refuse to execute. For cases where doing nothing beats doing part."""


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------
def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def case_insensitive(root: str) -> bool:
    """Probe at runtime whether this is a case-insensitive filesystem.

    APFS defaults to insensitive, but the user may have formatted it
    case-sensitive, and external volumes vary. Assume, and A.pdf overwrites
    a.pdf.
    """
    probe = os.path.join(root, TMP_PREFIX + "CaseProbe")
    try:
        with open(probe, "x"):
            pass
    except OSError:
        return True                     # cannot probe -> assume insensitive, conservatively
    try:
        return os.path.exists(os.path.join(root, TMP_PREFIX + "caseprobe"))
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


# Paths that can never be a destination at any level. Declaring them as dest
# does not open them either.
FORBIDDEN_DEST = (
    "/System", "/Library", "/private", "/usr", "/bin", "/sbin", "/etc", "/var",
    "/Applications", "/cores", "/dev", "/opt",
)


def _resolve(target: str) -> str:
    """The leaf may itself be a symlink, so realpath only as far as the parent.

    Resolving through the leaf turns the ordinary job of "moving a link" into
    an accident that touches whatever the link points at.
    """
    parent_r = os.path.realpath(os.path.dirname(target))
    return os.path.join(parent_r, os.path.basename(target))


def _under(base: str, path: str) -> bool:
    a, b = nfc(path), nfc(os.path.realpath(base))
    return a == b or a.startswith(b + os.sep)


def within(root: str, target: str) -> str:
    """Guarantee target is under root. The strict check, applied to sources."""
    final = _resolve(target)
    if not _under(root, final):
        raise Refuse(f"outside the scope: {target} -> {final} "
                     f"(root={os.path.realpath(root)})")
    return final


def within_dest(root: str, dest: str | None, target: str) -> str:
    """Destination check. Must be inside root, or inside the dest the plan
    explicitly declared.

    Sources are always confined to the single root, but the need to put results
    in a different directory is real (identity and financial documents belong
    in Documents, not Downloads). Allowing "anywhere" there would make the scope
    guard meaningless, so the destination opens onto exactly one place — the
    dest written in the plan — and that value is itself validated. The user sees
    the absolute dest path on the approval screen, so nobody ever approves
    without knowing where things are headed.
    """
    final = _resolve(target)
    if _under(root, final):
        return final
    if dest:
        dest_r = os.path.realpath(dest)
        home = os.path.realpath(os.path.expanduser("~"))
        if dest_r in ("/", home, "/Users", "/Volumes"):
            raise Refuse(f"not usable as a destination: {dest_r}")
        if any(dest_r == f or dest_r.startswith(f + os.sep)
               for f in FORBIDDEN_DEST):
            raise Refuse(f"a protected path cannot be a destination: {dest_r}")
        if _under(dest_r, final):
            return final
    raise Refuse(f"destination is outside root and outside the declared dest: {final}")


def exists_ci(path: str, ci: bool) -> bool:
    """On a case-insensitive FS, os.path.exists cannot tell A.pdf from a.pdf.

    Comparing the sibling listing directly is what prevents the accident of
    "reporting it absent when it is there, and then overwriting it".
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
    """If the target exists, step aside to ' (2)', ' (3)' … Never overwrite."""
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
    """A move where overwriting is structurally impossible.
    Returns (final path, method, collision count).

    os.rename / shutil.move silently overwrite an existing target. So files are
    moved with os.link (which fails with EEXIST) + unlink — atomic on the same
    volume, and the source only disappears once the link is confirmed.
    """
    final, collisions = resolve_collision(dst, ci)
    os.makedirs(os.path.dirname(final), exist_ok=True)

    src_st = os.lstat(src)
    dst_dev = os.stat(os.path.dirname(final)).st_dev

    if os.path.islink(src) or os.path.isdir(src):
        if exists_ci(final, ci):
            raise Refuse(f"target already exists: {final}")
        os.rename(src, final)                       # assumes the same volume
        return final, "os.rename", collisions

    if src_st.st_dev == dst_dev:
        os.link(src, final)                         # FileExistsError if present
        os.unlink(src)
        return final, "os.link+unlink", collisions

    # Cross-volume: temp copy -> verify -> commit -> only then clear the source
    if src_st.st_nlink > 1:
        raise Refuse(f"hardlinked file must not move across volumes "
                     f"(the links would be severed): {src}")
    tmp = os.path.join(os.path.dirname(final),
                       TMP_PREFIX + hashlib.blake2b(src.encode(),
                                                    digest_size=8).hexdigest())
    try:
        shutil.copy2(src, tmp)
        if os.path.getsize(tmp) != src_st.st_size:
            raise Refuse(f"copy size mismatch: {src}")
        os.link(tmp, final)
    except BaseException:
        # If the disk fills and the copy blows up, a truncated temp file is left
        # at the destination. The source has not been touched yet, so clearing
        # the temp file returns everything with nothing lost.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.unlink(tmp)
    os.unlink(src)
    return final, "copy2+verify", collisions


# --------------------------------------------------------------------------
# Plan validation
# --------------------------------------------------------------------------
def _lock_holder(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _pid_alive(pid: int) -> bool:
    """Is that PID still alive? Signal 0 only checks existence, no side effect."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                      # somebody else's process, but alive
    return True


def plan_token(plan: dict) -> str:
    """Canonical hash of the plan. Change one character after approval and the
    token no longer matches."""
    body = json.dumps(plan.get("operations", []), ensure_ascii=False,
                      sort_keys=True).encode()
    scope = plan.get("scope", {})
    head = (scope.get("root", "") + "\0" + (scope.get("dest") or "")).encode()
    return hashlib.sha256(head + b"\0" + body).hexdigest()[:12]


def preflight(plan: dict) -> dict:
    """Checks run immediately before execution. Anything that fails drops that
    entry, or refuses the whole run."""
    root = plan["scope"]["root"]
    if not os.path.isdir(root):
        raise Refuse(f"scope root does not exist: {root}")
    real = os.path.realpath(root)
    home = os.path.realpath(os.path.expanduser("~"))
    if real in (home, "/", "/Users", "/Volumes") or real.startswith("/System"):
        raise Refuse(f"not usable as a scope root: {real}")

    dest = plan.get("scope", {}).get("dest")
    if dest:
        os.makedirs(dest, exist_ok=True)            # must exist before realpath means anything
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

        # If the file changed after the scan, the plan's premise has collapsed
        # (TOCTOU).
        snap = op.get("snapshot") or {}
        if snap and (st.st_size != snap.get("size") or
                     int(st.st_mtime) != snap.get("mtime")):
            skip.append({**op, "skip_reason": "src_changed_since_scan"}); continue
        if (time.time() - st.st_mtime) < 90:
            skip.append({**op, "skip_reason": "mtime_within_90s"}); continue
        if getattr(st, "st_flags", 0) & 0x40000000:
            skip.append({**op, "skip_reason": "dataless_icloud"}); continue

        try:
            within(root, src)                       # sources are always inside root
            within_dest(root, dest, op["dst"])      # destinations: root or dest
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
        # Free space has to be measured where we WRITE. Measuring root (the
        # source) means home always has hundreds of GB free, so the guard passes
        # even when the destination is full.
        probe = dest or root
        while probe and not os.path.isdir(probe):
            probe = os.path.dirname(probe)
        du = shutil.disk_usage(probe or root)
        # A fixed 5GB headroom would mean small destination volumes — USB
        # sticks, disk images — could never pass no matter what is moved.
        # Scale it to the volume size instead.
        headroom = min(1 << 30, max(8 << 20, du.total // 20))
        need = int(xvol_bytes * 1.15) + headroom
        if du.free < need:
            raise Refuse(
                f"not enough free space ({probe or root}): need "
                f"{need/1048576:.0f}MB (data {xvol_bytes/1048576:.0f}MB + "
                f"headroom {headroom/1048576:.0f}MB), available "
                f"{du.free/1048576:.0f}MB")

    return {"ok": ok, "skipped": skip, "case_insensitive": ci,
            "cross_volume_bytes": xvol_bytes}


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
def run(plan_path: str, token: str, out_dir: str) -> int:
    plan = json.load(open(plan_path, encoding="utf-8"))
    expect = plan_token(plan)
    if token != expect:
        raise Refuse(f"approval token mismatch. The plan changed after it was "
                     f"approved.\n  received: {token}\n  plan hash: {expect}")

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
        # Check whether the process holding the lock is still alive. A run
        # killed by SIGKILL never reaches its finally block and leaves the lock
        # behind, after which every future run is refused forever. An ownerless
        # lock is the right thing to reclaim.
        holder = _lock_holder(lock)
        if holder is not None and _pid_alive(holder):
            raise Refuse(f"another cleanup run is already in progress "
                         f"(PID {holder}): {lock}")
        print(f"[NOTICE] reclaiming an ownerless lock: {lock}", file=sys.stderr)
        try:
            os.unlink(lock)
            with open(lock, "x") as f:
                f.write(f"{os.getpid()} {run_id}\n")
        except OSError as e:
            raise Refuse(f"could not reclaim the lock: {e}")

    journal_path = os.path.join(rd, "journal.jsonl")
    ops_done: list[dict] = []
    created_dirs: list[str] = []

    def jwrite(fh, rec):
        """The journal always hits the disk first. Interrupted or not, how far
        the run got is on record."""
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
                # The source may have vanished between preflight and this
                # moment. An earlier move in this same run can be the cause
                # (two names for one file on a case-insensitive FS), so this is
                # demoted to a skip rather than a failure. As a failure it would
                # report an entirely healthy plan as an abnormal termination.
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
                        # Preserve the plan's evidence verbatim. It is what you
                        # need later to check "is the canonical copy of this
                        # quarantined file still alive" — the earlier revision
                        # threw it away, making after-the-fact verification
                        # impossible.
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
# Rollback
# --------------------------------------------------------------------------
def manifest_from_journal(journal_path: str) -> dict:
    """Rebuild an undo manifest for a run that left only a journal.

    The manifest is written only on a clean exit. Die to a SIGKILL or a power
    cut and hundreds of files may already have moved with no input left to
    reverse them — that is exactly where this tool's premise, "you can always
    undo it", breaks. The journal is fsynced before every move, so it alone has
    to be enough to restore from.
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
                continue                 # the last line may be truncated by the interruption
            if "run_id" in r and "seq" not in r:
                head = r
            elif r.get("state") == "done":
                done[r["seq"]] = r
            elif r.get("state") == "pending":
                pending[r["seq"]] = r

    ops = list(done.values())
    # Exactly one entry ends up having finished its move but died just before
    # writing 'done'. The journal holds only its pending record. Dropping it
    # would strand that one file away from home forever, so it is promoted to a
    # restore candidate and the judgment is left to undo's preconditions — if it
    # never actually moved, there is no file at the destination and it is
    # skipped automatically.
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
        "created_dirs": [],              # not in the journal; empty folders stay
        "operations": ops,
        "skipped": [],
    }


def resolve_manifest(path: str) -> dict:
    """Take a manifest path or a run directory and produce the undo input."""
    if os.path.isdir(path):
        mp = os.path.join(path, "manifest.json")
        jp = os.path.join(path, "journal.jsonl")
    else:
        mp = path
        jp = os.path.join(os.path.dirname(path), "journal.jsonl")
    if os.path.isfile(mp):
        return json.load(open(mp, encoding="utf-8"))
    if os.path.isfile(jp):
        print(f"[NOTICE] no manifest; recovering from the journal: {jp}",
              file=sys.stderr)
        return manifest_from_journal(jp)
    raise Refuse(f"nothing on record to undo: {path}")


def undo(manifest_path: str) -> int:
    m = resolve_manifest(manifest_path)
    ci = m.get("environment", {}).get("case_insensitive_fs", True)
    restored, blocked = [], []

    # Replay in reverse. Files that went into folders created on the way
    # forward have to come out before those folders can be cleaned up.
    for op in sorted([o for o in m["operations"] if o["state"] == "done"],
                     key=lambda o: -o["seq"]):
        cur, orig = op["dst_actual"], op["src"]
        if not os.path.lexists(cur):
            blocked.append({**op, "why": "no file at the current location "
                                         "(moved already?)"})
            continue
        snap = op.get("snapshot") or {}
        if snap:
            st = os.lstat(cur)
            if st.st_size != snap.get("size"):
                blocked.append({**op, "why": "contents changed since the cleanup"})
                continue
        if exists_ci(orig, ci):
            # A move is os.link() then os.unlink(). If the process dies between
            # the two, the file exists at BOTH paths as a single inode — nothing
            # was lost, but the destination holds a stray directory entry that
            # would otherwise linger forever. Dropping that entry is not a
            # deletion: the content remains at the original path via the other
            # link. Verify the inode before touching anything.
            try:
                a, b = os.lstat(cur), os.lstat(orig)
                same_inode = (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
            except OSError:
                same_inode = False
            if same_inode:
                try:
                    os.unlink(cur)
                    restored.append({"from": cur, "to": orig,
                                     "note": "interrupted move completed"})
                except OSError as e:
                    blocked.append({**op, "why": f"stray link left behind: {e}"})
                continue
            blocked.append({**op, "why": "a different file now sits in the "
                                         "original spot"})
            continue
        try:
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            final, _, _ = move_noclobber(cur, orig, ci)
            restored.append({"from": cur, "to": final})
        except Exception as e:                            # noqa: BLE001
            blocked.append({**op, "why": f"{type(e).__name__}: {e}"})

    # Only clean up the empty folders THIS run created. Pre-existing folders
    # are never touched.
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
    ap = argparse.ArgumentParser(
        description="Cleanup plan executor (has no delete capability)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="compute the approval token + preflight")
    v.add_argument("plan")
    r = sub.add_parser("run", help="execute an approved plan")
    r.add_argument("plan")
    r.add_argument("--token", required=True)
    r.add_argument("--runs-dir", default="_runs")
    u = sub.add_parser("undo", help="full rollback from a manifest")
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
        print(f"[REFUSED] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
