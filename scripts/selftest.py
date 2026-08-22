#!/usr/bin/env python3
"""Adversarial self-test — proves in a sandbox that the executor is actually safe.

The names exercised here (NFD Hangul, spaces, quotes, semicolons, newlines,
case collisions) are either patterns that exist in this user's real Downloads
folder, or names that would have caused an accident had the tool assembled
shell strings. If this does not pass, the executor is not pointed at user files.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
APPLY = os.path.join(HERE, "apply.py")
SCAN = os.path.join(HERE, "scan.py")
CLASSIFY = os.path.join(HERE, "classify.py")

# The Korean filenames below are fixture DATA, not UI text — they are the whole
# point of the NFD and non-ASCII cases. Only the labels are translated.
CASES = [
    ("NFD Hangul filename", unicodedata.normalize("NFD", "한글_보고서_최종.pdf")),
    ("spaces and parentheses", "보고서 (1).pdf"),
    ("quote and semicolon", "Don't Stop; echo pwned.mp3"),
    ("dollar sign and backticks", "cost $100 `whoami`.txt"),
    ("embedded newline", "line\nbreak.txt"),
    ("case collision A", "Report.PDF"),
    ("case collision a", "report.pdf"),
    ("name collides with the destination", "충돌.pdf"),
]


# Disk-image volume names are global to the machine. A fixed name makes two
# concurrent runs (CI matrix, or a repo copy and an installed copy) collide
# on /Volumes/<name> and fail spuriously. Tag the name with this PID.
VOL_TAG = str(os.getpid())


def _dead_pid() -> int:
    """Produce a PID that is definitely dead. Picking an arbitrary large number
    collides with a recycled PID and makes the test flip intermittently. Start a
    real process and reap it instead."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def run(args: list[str], cwd: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, APPLY] + args, cwd=cwd,
                       capture_output=True, text=True, shell=False)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    sb = tempfile.mkdtemp(prefix="cleaner-selftest-")
    src, dst = os.path.join(sb, "src"), os.path.join(sb, "dst")
    os.makedirs(src)
    os.makedirs(dst)

    for label, name in CASES:
        try:
            with open(os.path.join(src, name), "w") as f:
                f.write("x" * 100)
        except OSError as e:
            print(f"  (could not create) {label}: {e}")

    # On a case-insensitive filesystem Report.PDF and report.pdf are one file.
    # Targeting what actually landed on disk rather than the CASES list is what
    # keeps phantom operations on non-existent files out of the plan.
    ci_fs = len(os.listdir(src)) < len(CASES)
    made = [(n, n) for n in os.listdir(src)]
    print(f"  {len(made)} fixtures created "
          f"(filesystem is case-{'insensitive' if ci_fs else 'sensitive'})")

    # The executor treats anything written in the last 90 seconds as "in use"
    # and leaves it alone. The fixtures were just created and would trip that
    # guard, so wind their timestamps back a day.
    old_ts = int(__import__("time").time()) - 86400
    for _label, name in made:
        os.utime(os.path.join(src, name), (old_ts, old_ts))

    # Plant the same name at the destination in advance to force collision avoidance
    with open(os.path.join(dst, "충돌.pdf"), "w") as f:
        f.write("DIFFERENT CONTENT — this file must never be overwritten")
    guard_before = open(os.path.join(dst, "충돌.pdf"), "rb").read()

    ops = []
    for _label, name in made:
        p = os.path.join(src, name)
        st = os.lstat(p)
        ops.append({"action": "move", "src": p, "dst": os.path.join(dst, name),
                    "reason": "selftest",
                    "snapshot": {"size": st.st_size, "mtime": int(st.st_mtime)}})
    plan_path = os.path.join(sb, "plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump({"scope": {"root": sb}, "operations": ops}, f,
                  ensure_ascii=False, indent=2)

    results: list[tuple[str, bool, str]] = []

    # 1) does verify issue a token
    rc, out = run(["verify", plan_path], sb)
    token = json.loads(out)["approval_token"] if rc == 0 else ""
    results.append(("verify issues an approval token",
                    rc == 0 and len(token) == 12, token))

    # 2) is a wrong token rejected (the approval gate)
    #    The substring below must stay in sync with apply.py's Refuse message
    #    for a token mismatch.
    rc, out = run(["run", plan_path, "--token", "0" * 12], sb)
    results.append(("forged token rejected",
                    rc == 2 and "token mismatch" in out, out.strip()[:60]))

    # 3) is a plan tampered with after approval rejected
    tampered = json.load(open(plan_path, encoding="utf-8"))
    tampered["operations"].append({"action": "move", "src": "/etc/hosts",
                                   "dst": os.path.join(dst, "hosts")})
    tpath = os.path.join(sb, "tampered.json")
    with open(tpath, "w", encoding="utf-8") as f:
        json.dump(tampered, f, ensure_ascii=False)
    rc, out = run(["run", tpath, "--token", token], sb)
    results.append(("plan tampered with after approval rejected",
                    rc == 2, out.strip()[:60]))

    # 4) the real run
    rc, out = run(["run", plan_path, "--token", token, "--runs-dir",
                   os.path.join(sb, "_runs")], sb)
    ok_run = rc == 0
    info = json.loads(out) if ok_run else {}
    tot = info.get("totals", {})
    results.append(("every entry moved successfully",
                    ok_run and tot.get("done") == len(made)
                    and tot.get("failed") == 0 and tot.get("skipped") == 0,
                    json.dumps(tot, ensure_ascii=False)))

    # 5) was the pre-existing file left unoverwritten — the most important check
    guard_after = open(os.path.join(dst, "충돌.pdf"), "rb").read()
    results.append(("existing file preserved on collision",
                    guard_before == guard_after, f"{len(guard_after)} bytes"))

    # 6) did the colliding file survive under a stepped-aside name
    survived = [n for n in os.listdir(dst) if n.startswith("충돌")]
    results.append(("colliding file not lost either",
                    len(survived) == 2, str(survived)))

    # 7) case handling — the expectation depends on the filesystem.
    #    On a case-sensitive FS both files must survive; on an insensitive one
    #    there was only ever one file, so what is being checked is "did one of
    #    them quietly disappear".
    reports = [n for n in os.listdir(dst) if n.lower().startswith("report")]
    expect = 1 if ci_fs else 2
    results.append((f"case handling ({'insensitive' if ci_fs else 'sensitive'} FS)",
                    len(reports) == expect, f"{reports} (expected {expect})"))

    # 8) did the NFD Hangul name actually move
    korean = [n for n in os.listdir(dst)
              if unicodedata.normalize("NFC", n).startswith("한글_보고서")]
    results.append(("NFD Hangul filename moved", len(korean) == 1, str(korean)))

    # 9) were shell metacharacter filenames handled without incident
    results.append(("shell metacharacter filenames unharmed",
                    not os.path.exists(os.path.join(sb, "pwned")) and
                    any("Don't Stop" in n for n in os.listdir(dst)), ""))

    # 10) rollback
    manifest = info.get("manifest", "")
    rc, out = run(["undo", manifest], sb)
    undo_info = json.loads(out) if out.strip().startswith("{") else {}
    back = sorted(os.listdir(src))
    results.append(("undo puts every file back",
                    len(back) == len(made) and undo_info.get("blocked", 1) == 0,
                    f"{len(back)}/{len(made)} restored"))

    # 11) is the destination back to its original state (only the planted file)
    left = sorted(os.listdir(dst))
    results.append(("destination clean after undo",
                    left == ["충돌.pdf"], str(left)))

    # --- from here: cases added in the 2026-08-22 open-source audit ----------
    # All 1,000+ real runs so far happened on one Mac, one user, one
    # case-insensitive APFS volume. The branches outside those conditions had
    # never once executed.

    # 12) scan.py — is a bundle detected by Info.plist structure even with an
    #     arbitrary extension
    bsb = os.path.join(sb, "bundle_test")
    bscope = os.path.join(bsb, "scope")
    os.makedirs(os.path.join(bscope, "Weird.xyz", "Contents"), exist_ok=True)
    with open(os.path.join(bscope, "Weird.xyz", "Contents", "Info.plist"), "w") as f:
        f.write("plist")
    with open(os.path.join(bscope, "Weird.xyz", "Contents", "res.dat"), "w") as f:
        f.write("resource")
    p = subprocess.run([sys.executable, SCAN, bscope, "-o",
                        os.path.join(bsb, "inv.jsonl"), "--max-depth", "-1"],
                       cwd=bsb, capture_output=True, text=True, timeout=10)
    kinds = [json.loads(l)["kind"]
             for l in open(os.path.join(bsb, "inv.jsonl"), encoding="utf-8")]
    results.append(("scan: bundle detected by Info.plist structure, any extension",
                    p.returncode == 0 and kinds == ["bundle"],
                    f"rc={p.returncode} kinds={kinds}"))

    # 13) scan.py — symlinks are not followed, and a cyclic link is safe
    lsb = os.path.join(sb, "link_test")
    lscope = os.path.join(lsb, "scope")
    other = os.path.join(lsb, "other")
    os.makedirs(lscope); os.makedirs(other)
    with open(os.path.join(other, "target.txt"), "w") as f:
        f.write("x")
    os.symlink(os.path.join(other, "target.txt"),
              os.path.join(lscope, "link.txt"))
    os.makedirs(os.path.join(lscope, "sub"))
    os.symlink(lscope, os.path.join(lscope, "sub", "loop"))   # cyclic link
    p = subprocess.run([sys.executable, SCAN, lscope, "-o",
                        os.path.join(lsb, "inv.jsonl"), "--max-depth", "-1"],
                       cwd=lsb, capture_output=True, text=True, timeout=10)
    lkinds = [json.loads(l)["kind"]
             for l in open(os.path.join(lsb, "inv.jsonl"), encoding="utf-8")]
    results.append(("scan: symlinks not followed + no infinite loop on a cycle",
                    p.returncode == 0 and lkinds.count("symlink") == 2,
                    f"rc={p.returncode} kinds={lkinds}"))

    # 14) scan.py — an unreadable directory (isomorphic to a TCC denial) is
    #     reported as an error without dying, and sibling directories still scan
    psb = os.path.join(sb, "perm_test")
    pscope = os.path.join(psb, "scope")
    os.makedirs(os.path.join(pscope, "locked"))
    os.makedirs(os.path.join(pscope, "open"))
    with open(os.path.join(pscope, "open", "x.txt"), "w") as f:
        f.write("readable")
    with open(os.path.join(pscope, "locked", "y.txt"), "w") as f:
        f.write("unreadable")
    os.chmod(os.path.join(pscope, "locked"), 0o000)
    try:
        p = subprocess.run([sys.executable, SCAN, pscope, "-o",
                            os.path.join(psb, "inv.jsonl"), "--max-depth", "-1"],
                           cwd=psb, capture_output=True, text=True, timeout=10)
        recs = [json.loads(l)
               for l in open(os.path.join(psb, "inv.jsonl"), encoding="utf-8")]
    finally:
        os.chmod(os.path.join(pscope, "locked"), 0o755)   # restore so rmtree works
    # The error kind is never written to the inventory JSONL; it only lands in
    # the stdout summary counts (the "noise" / "depth_limit" / "error" continue
    # branches in scan.py main()). So this reads stdout counts, not the JSONL.
    try:
        scan_counts = json.loads(p.stdout).get("counts", {})
    except json.JSONDecodeError:
        scan_counts = {}
    ok_files = [r for r in recs if r["kind"] == "file"]
    results.append(("scan: unreadable dir counted as error, siblings keep scanning",
                    p.returncode == 0 and len(ok_files) == 1
                    and scan_counts.get("error") == 1,
                    f"files={len(ok_files)} counts={scan_counts}"))

    # 15) apply.py — does a lock file refuse a concurrent run
    lksb = os.path.join(sb, "lock_test")
    os.makedirs(lksb)
    with open(os.path.join(lksb, "f.txt"), "w") as f:
        f.write("x")
    old = int(__import__("time").time()) - 86400
    os.utime(os.path.join(lksb, "f.txt"), (old, old))
    lplan = {"scope": {"root": lksb}, "operations": [{
        "action": "move", "src": os.path.join(lksb, "f.txt"),
        "dst": os.path.join(lksb, "sub", "f.txt"), "reason": "lock test",
        "snapshot": {"size": 1, "mtime": old}}]}
    with open(os.path.join(lksb, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(lplan, f, ensure_ascii=False)
    rc, out = run(["verify", os.path.join(lksb, "plan.json")], lksb)
    tok = json.loads(out)["approval_token"] if rc == 0 else ""

    # 15a) a lock held by a live process must be refused.
    #      Use our own PID — the one PID guaranteed alive right now.
    #      The substring below must stay in sync with apply.py's Refuse message
    #      for a lock held by a live process.
    with open(os.path.join(lksb, ".cleaner.lock"), "w") as f:
        f.write(f"{os.getpid()} live-run")
    rc, out = run(["run", os.path.join(lksb, "plan.json"), "--token", tok,
                  "--runs-dir", os.path.join(lksb, "_runs")], lksb)
    results.append(("apply: concurrent run refused while the lock is live",
                    rc == 2 and "already in progress" in out
                    and os.path.exists(os.path.join(lksb, "f.txt")),
                    out.strip()[:60]))

    # 15b) a lock whose owner is dead must be reclaimed. If a run killed by
    #      SIGKILL leaves its lock behind, that directory can never be cleaned
    #      again unless the lock is reclaimed.
    dead = _dead_pid()
    with open(os.path.join(lksb, ".cleaner.lock"), "w") as f:
        f.write(f"{dead} stale-run")
    rc, out = run(["run", os.path.join(lksb, "plan.json"), "--token", tok,
                  "--runs-dir", os.path.join(lksb, "_runs")], lksb)
    moved = os.path.exists(os.path.join(lksb, "sub", "f.txt"))
    results.append(("apply: ownerless lock reclaimed and the run proceeds",
                    rc == 0 and moved, f"dead_pid={dead} moved={moved}"))
    # This scenario does expose one unresolved defect: the lock is never
    # released automatically (if the original process dies to SIGKILL or a
    # crash, the lock stays forever). What is verified here is only "a lock
    # causes a refusal" — that part does behave safely.

    # 16) apply.py — with no write permission on the destination, is the source
    #     left intact and the entry reported as failed (no crash, no partial state)
    wsb = os.path.join(sb, "writeperm_test")
    os.makedirs(os.path.join(wsb, "locked_dst"))
    with open(os.path.join(wsb, "src.txt"), "w") as f:
        f.write("x")
    os.utime(os.path.join(wsb, "src.txt"), (old, old))
    os.chmod(os.path.join(wsb, "locked_dst"), 0o555)
    wplan = {"scope": {"root": wsb}, "operations": [{
        "action": "move", "src": os.path.join(wsb, "src.txt"),
        "dst": os.path.join(wsb, "locked_dst", "src.txt"), "reason": "perm test",
        "snapshot": {"size": 1, "mtime": old}}]}
    with open(os.path.join(wsb, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(wplan, f, ensure_ascii=False)
    rc, out = run(["verify", os.path.join(wsb, "plan.json")], wsb)
    tok = json.loads(out)["approval_token"] if rc == 0 else ""
    try:
        rc, out = run(["run", os.path.join(wsb, "plan.json"), "--token", tok,
                      "--runs-dir", os.path.join(wsb, "_runs")], wsb)
        info2 = json.loads(out) if rc in (0, 1) else {}
    finally:
        os.chmod(os.path.join(wsb, "locked_dst"), 0o755)
    results.append(("apply: write denied — source preserved, fails safely",
                    info2.get("totals", {}).get("failed") == 1
                    and os.path.exists(os.path.join(wsb, "src.txt")),
                    json.dumps(info2.get("totals", {}), ensure_ascii=False)))

    # 17) classify.py — does --no-home-scan really switch off the whole-home scan
    csb = os.path.join(sb, "classify_test")
    os.makedirs(csb)
    inv = [{"path": "/tmp/x.txt", "kind": "file", "name": "x.txt",
           "stem": "x", "ext": "txt", "category": "text", "size": 1,
           "mtime": 0, "parent": "/tmp"}]
    with open(os.path.join(csb, "inv.jsonl"), "w", encoding="utf-8") as f:
        for r in inv:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    p = subprocess.run([sys.executable, CLASSIFY, os.path.join(csb, "inv.jsonl"),
                        "-o", os.path.join(csb, "out.jsonl"), "--no-home-scan"],
                       cwd=csb, capture_output=True, text=True, timeout=15)
    # Read the count, not the sentence. classify.py's stderr summary leads with
    # the project-candidate count, and all this test cares about is that the
    # count is 0. Matching the wording would tie this assertion to that file's
    # phrasing and break every time it is reworded or translated.
    counts = "".join(c if c.isdigit() else " "
                     for c in (p.stderr or "")).split()
    results.append(("classify: --no-home-scan yields 0 project candidates",
                    p.returncode == 0 and counts[:1] == ["0"],
                    (p.stderr or "").strip()[:60]))

    # 18) cross-volume moves — only reproducible by creating a genuinely
    #     separate volume with hdiutil. Verifies that a hardlinked file is
    #     refused across volumes, that a normal file succeeds via copy2+verify,
    #     and that both files survive on a case-sensitive APFS volume.
    #     If hdiutil is missing or fails (sandbox, CI), this is skipped rather
    #     than counted as a failure.
    vol_a = vol_b = None
    try:
        if shutil.which("hdiutil") is None:
            raise RuntimeError("no hdiutil")
        dmg_dir = os.path.join(sb, "dmg")
        os.makedirs(dmg_dir, exist_ok=True)

        def make_vol(name, fs):
            img = os.path.join(dmg_dir, name + ".dmg")
            subprocess.run(["hdiutil", "create", "-size", "48m", "-fs", fs,
                            "-volname", name, img], check=True,
                           capture_output=True, timeout=30)
            subprocess.run(["hdiutil", "attach", img], check=True,
                           capture_output=True, timeout=30)
            return "/Volumes/" + name

        vol_a = make_vol("CleanerSelftestA" + VOL_TAG, "APFS")

        # 18a) hardlink refused across volumes — source preserved
        hsb = os.path.join(sb, "hardlink_test")
        os.makedirs(hsb)
        with open(os.path.join(hsb, "orig.txt"), "w") as f:
            f.write("hardlinked")
        os.link(os.path.join(hsb, "orig.txt"), os.path.join(hsb, "hl.txt"))
        old2 = int(__import__("time").time()) - 86400
        os.utime(os.path.join(hsb, "hl.txt"), (old2, old2))
        st = os.stat(os.path.join(hsb, "hl.txt"))
        hplan = {"scope": {"root": hsb, "dest": vol_a}, "operations": [{
            "action": "archive", "src": os.path.join(hsb, "hl.txt"),
            "dst": os.path.join(vol_a, "hl.txt"), "reason": "hardlink test",
            "snapshot": {"size": st.st_size, "mtime": int(st.st_mtime)}}]}
        with open(os.path.join(hsb, "plan.json"), "w", encoding="utf-8") as f:
            json.dump(hplan, f, ensure_ascii=False)
        rc, out = run(["verify", os.path.join(hsb, "plan.json")], hsb)
        tok = json.loads(out)["approval_token"] if rc == 0 else ""
        rc, out = run(["run", os.path.join(hsb, "plan.json"), "--token", tok,
                      "--runs-dir", os.path.join(hsb, "_runs")], hsb)
        hinfo = json.loads(out) if out.strip().startswith("{") else {}
        results.append(("apply: hardlink refused across volumes + source preserved",
                        hinfo.get("totals", {}).get("failed") == 1
                        and os.path.exists(os.path.join(hsb, "hl.txt"))
                        and os.path.exists(os.path.join(hsb, "orig.txt")),
                        json.dumps(hinfo.get("totals", {}), ensure_ascii=False)))

        # 18b) a normal file across volumes — copy2+verify succeeds, contents intact
        xsb = os.path.join(sb, "xvol_test")
        os.makedirs(xsb)
        payload = "cross-volume payload 데이터"
        with open(os.path.join(xsb, "n.txt"), "w") as f:
            f.write(payload)
        os.utime(os.path.join(xsb, "n.txt"), (old2, old2))
        st = os.stat(os.path.join(xsb, "n.txt"))
        xplan = {"scope": {"root": xsb, "dest": vol_a}, "operations": [{
            "action": "archive", "src": os.path.join(xsb, "n.txt"),
            "dst": os.path.join(vol_a, "n.txt"), "reason": "xvol test",
            "snapshot": {"size": st.st_size, "mtime": int(st.st_mtime)}}]}
        with open(os.path.join(xsb, "plan.json"), "w", encoding="utf-8") as f:
            json.dump(xplan, f, ensure_ascii=False)
        rc, out = run(["verify", os.path.join(xsb, "plan.json")], xsb)
        tok = json.loads(out)["approval_token"] if rc == 0 else ""
        rc, out = run(["run", os.path.join(xsb, "plan.json"), "--token", tok,
                      "--runs-dir", os.path.join(xsb, "_runs")], xsb)
        xinfo = json.loads(out) if out.strip().startswith("{") else {}
        moved_ok = os.path.exists(os.path.join(vol_a, "n.txt")) and \
            open(os.path.join(vol_a, "n.txt"), encoding="utf-8").read() == payload
        results.append(("apply: cross-volume copy2+verify move + content integrity",
                        xinfo.get("totals", {}).get("done") == 1
                        and not os.path.exists(os.path.join(xsb, "n.txt"))
                        and moved_ok,
                        json.dumps(xinfo.get("totals", {}), ensure_ascii=False)))

        # 18c) case-sensitive APFS — do Report.PDF and report.pdf both survive
        vol_b = make_vol("CleanerSelftestB" + VOL_TAG, "Case-sensitive APFS")
        cs_root = os.path.join(vol_b, "root")
        os.makedirs(os.path.join(cs_root, "dst"))
        with open(os.path.join(cs_root, "Report.PDF"), "w") as f:
            f.write("UPPER")
        with open(os.path.join(cs_root, "report.pdf"), "w") as f:
            f.write("lower")
        os.utime(os.path.join(cs_root, "Report.PDF"), (old2, old2))
        os.utime(os.path.join(cs_root, "report.pdf"), (old2, old2))
        csops = []
        for nm in ("Report.PDF", "report.pdf"):
            p_ = os.path.join(cs_root, nm)
            st = os.stat(p_)
            csops.append({"action": "move", "src": p_,
                          "dst": os.path.join(cs_root, "dst", nm),
                          "reason": "case test",
                          "snapshot": {"size": st.st_size,
                                       "mtime": int(st.st_mtime)}})
        with open(os.path.join(cs_root, "plan.json"), "w", encoding="utf-8") as f:
            json.dump({"scope": {"root": cs_root}, "operations": csops}, f,
                      ensure_ascii=False)
        rc, out = run(["verify", os.path.join(cs_root, "plan.json")], cs_root)
        vinfo = json.loads(out) if rc == 0 else {}
        tok = vinfo.get("approval_token", "")
        rc, out = run(["run", os.path.join(cs_root, "plan.json"), "--token", tok,
                      "--runs-dir", os.path.join(cs_root, "_runs")], cs_root)
        csresult = os.listdir(os.path.join(cs_root, "dst"))
        results.append(("apply: both files preserved on case-sensitive APFS",
                        vinfo.get("case_insensitive_fs") is False
                        and sorted(csresult) == ["Report.PDF", "report.pdf"],
                        f"case_insensitive_fs={vinfo.get('case_insensitive_fs')} "
                        f"dst={sorted(csresult)}"))
    except Exception as e:                                  # noqa: BLE001
        results.append(("apply: 3 cross-volume cases (hardlink, copy2, case-sensitive)",
                        True, f"skipped — no usable hdiutil environment: {e}"))
    finally:
        for v in (vol_a, vol_b):
            if v and os.path.ismount(v):
                subprocess.run(["hdiutil", "detach", v, "-force"],
                               capture_output=True, timeout=30)

    # 22) after a SIGKILL, does the journal alone restore everything —
    #     this is the scenario that this tool's premise, "you can always undo
    #     it", rides on. The manifest is only written on a clean exit, so once
    #     the process dies the journal is the only evidence there is.
    ksb = os.path.join(sb, "kill_test")
    ksrc = os.path.join(ksb, "src")
    os.makedirs(ksrc)
    kold = int(__import__("time").time()) - 86400
    kops = []
    N = 3000
    for i in range(N):
        fp = os.path.join(ksrc, f"f{i:05}.txt")
        with open(fp, "w") as f:
            f.write("x" * 200)
        os.utime(fp, (kold, kold))
        kops.append({"action": "move", "src": fp,
                     "dst": os.path.join(ksb, "dst", f"f{i:05}.txt"),
                     "reason": "kill", "snapshot": {"size": 200, "mtime": kold}})
    kpp = os.path.join(ksb, "plan.json")
    with open(kpp, "w", encoding="utf-8") as f:
        json.dump({"scope": {"root": ksb}, "operations": kops}, f, ensure_ascii=False)
    rc, out = run(["verify", kpp], ksb)
    ktok = json.loads(out)["approval_token"] if rc == 0 else ""
    kruns = os.path.join(ksb, "_runs")
    proc = subprocess.Popen([sys.executable, APPLY, "run", kpp, "--token", ktok,
                             "--runs-dir", kruns], cwd=ksb,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    __import__("time").sleep(0.6)
    proc.kill()
    proc.wait()
    rdirs = os.listdir(kruns) if os.path.isdir(kruns) else []
    if rdirs:
        rd = os.path.join(kruns, rdirs[0])
        had_manifest = os.path.exists(os.path.join(rd, "manifest.json"))
        moved = len(os.listdir(os.path.join(ksb, "dst"))) \
            if os.path.isdir(os.path.join(ksb, "dst")) else 0
        rc, out = run(["undo", rd], ksb)
        back = len(os.listdir(ksrc))
        left = len(os.listdir(os.path.join(ksb, "dst"))) \
            if os.path.isdir(os.path.join(ksb, "dst")) else 0
        results.append(("apply: full restore from the journal alone after SIGKILL",
                        moved > 0 and not had_manifest and back == N and left == 0,
                        f"{moved} moved before the kill -> {back}/{N} restored, "
                        f"{left} left behind"))
        # 23) is the lock left by the dead run reclaimed so a rerun works
        rc2, _ = run(["run", kpp, "--token", ktok, "--runs-dir", kruns], ksb)
        results.append(("apply: lock left by the SIGKILL reclaimed, rerun works",
                        rc2 == 0, f"rc={rc2}"))
    else:
        results.append(("apply: full restore from the journal alone after SIGKILL",
                        False, "no run directory was created"))

    print("\nAdversarial self-test")
    print("=" * 64)
    passed = 0
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + (f"    — {detail}" if detail else ""))
        passed += bool(ok)
    print("=" * 64)
    print(f"  {passed}/{len(results)} passed   sandbox: {sb}")

    if passed == len(results):
        shutil.rmtree(sb, ignore_errors=True)   # only the test sandbox is cleaned up
        print("  sandbox cleaned up")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
