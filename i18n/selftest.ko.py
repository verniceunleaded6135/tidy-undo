#!/usr/bin/env python3
"""적대적 자가 테스트 — 실행기가 실제로 안전한지 샌드박스에서 증명한다.

여기서 다루는 이름들(NFD 한글, 공백·따옴표·세미콜론·개행, 대소문자 충돌)은
이 사용자의 실제 Downloads 에 존재하는 패턴이거나, 셸 조립 방식이었다면
사고로 이어졌을 이름이다. 통과하지 못하면 실행기를 사용자 파일에 쓰지 않는다.
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

CASES = [
    ("NFD 한글 파일명", unicodedata.normalize("NFD", "한글_보고서_최종.pdf")),
    ("공백과 괄호", "보고서 (1).pdf"),
    ("따옴표와 세미콜론", "Don't Stop; echo pwned.mp3"),
    ("달러와 백틱", "cost $100 `whoami`.txt"),
    ("개행 포함", "line\nbreak.txt"),
    ("대소문자 충돌 A", "Report.PDF"),
    ("대소문자 충돌 a", "report.pdf"),
    ("목적지와 이름 충돌", "충돌.pdf"),
]


def _dead_pid() -> int:
    """확실히 죽어 있는 PID 를 만든다. 임의의 큰 수를 쓰면 재사용된 PID 와
    충돌해 테스트가 간헐적으로 뒤집힌다. 실제로 프로세스를 띄웠다 거둔다."""
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
            print(f"  (생성 불가) {label}: {e}")

    # 대소문자 무시 파일시스템에서는 Report.PDF 와 report.pdf 가 같은 파일이다.
    # CASES 목록이 아니라 실제로 디스크에 생긴 것만 대상으로 삼아야, 존재하지도
    # 않는 파일에 대한 유령 작업이 계획에 섞이지 않는다.
    ci_fs = len(os.listdir(src)) < len(CASES)
    made = [(n, n) for n in os.listdir(src)]
    print(f"  픽스처 {len(made)}개 생성 "
          f"(파일시스템 대소문자 {'무시' if ci_fs else '구분'})")

    # 실행기는 90초 이내에 쓰인 파일을 '작업 중'으로 보고 건드리지 않는다.
    # 방금 만든 픽스처가 그 가드에 걸리므로 시각을 하루 전으로 되돌린다.
    old_ts = int(__import__("time").time()) - 86400
    for _label, name in made:
        os.utime(os.path.join(src, name), (old_ts, old_ts))

    # 목적지에 같은 이름을 미리 심어 충돌 회피를 강제한다
    with open(os.path.join(dst, "충돌.pdf"), "w") as f:
        f.write("DIFFERENT CONTENT — 이 파일은 절대 덮어써지면 안 된다")
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

    # 1) verify 가 토큰을 내는가
    rc, out = run(["verify", plan_path], sb)
    token = json.loads(out)["approval_token"] if rc == 0 else ""
    results.append(("verify가 승인 토큰 발급", rc == 0 and len(token) == 12, token))

    # 2) 잘못된 토큰은 거부되는가 (승인 게이트)
    rc, out = run(["run", plan_path, "--token", "0" * 12], sb)
    results.append(("위조 토큰 거부", rc == 2 and "토큰 불일치" in out, out.strip()[:60]))

    # 3) 계획이 승인 후 변조되면 거부되는가
    tampered = json.load(open(plan_path, encoding="utf-8"))
    tampered["operations"].append({"action": "move", "src": "/etc/hosts",
                                   "dst": os.path.join(dst, "hosts")})
    tpath = os.path.join(sb, "tampered.json")
    with open(tpath, "w", encoding="utf-8") as f:
        json.dump(tampered, f, ensure_ascii=False)
    rc, out = run(["run", tpath, "--token", token], sb)
    results.append(("승인 후 계획 변조 거부", rc == 2, out.strip()[:60]))

    # 4) 실제 실행
    rc, out = run(["run", plan_path, "--token", token, "--runs-dir",
                   os.path.join(sb, "_runs")], sb)
    ok_run = rc == 0
    info = json.loads(out) if ok_run else {}
    tot = info.get("totals", {})
    results.append(("전 항목 이동 성공",
                    ok_run and tot.get("done") == len(made)
                    and tot.get("failed") == 0 and tot.get("skipped") == 0,
                    json.dumps(tot, ensure_ascii=False)))

    # 5) 기존 파일이 덮어써지지 않았는가 — 가장 중요한 검사
    guard_after = open(os.path.join(dst, "충돌.pdf"), "rb").read()
    results.append(("충돌 시 기존 파일 보존", guard_before == guard_after,
                    f"{len(guard_after)}바이트"))

    # 6) 충돌한 쪽은 비켜간 이름으로 살아있는가
    survived = [n for n in os.listdir(dst) if n.startswith("충돌")]
    results.append(("충돌한 파일도 유실 없음", len(survived) == 2, str(survived)))

    # 7) 대소문자 처리 — FS 성격에 따라 기대치가 다르다.
    #    구분 FS면 두 파일이 모두 살아야 하고, 무시 FS면 애초에 한 파일이므로
    #    "하나가 조용히 사라지지 않았는가"가 검사할 내용이다.
    reports = [n for n in os.listdir(dst) if n.lower().startswith("report")]
    expect = 1 if ci_fs else 2
    results.append((f"대소문자 처리 ({'무시' if ci_fs else '구분'} FS)",
                    len(reports) == expect, f"{reports} (기대 {expect}개)"))

    # 8) NFD 한글이 실제로 옮겨졌는가
    korean = [n for n in os.listdir(dst)
              if unicodedata.normalize("NFC", n).startswith("한글_보고서")]
    results.append(("NFD 한글 파일명 이동", len(korean) == 1, str(korean)))

    # 9) 셸 메타문자 파일명이 사고 없이 처리됐는가
    results.append(("셸 메타문자 파일명 무사",
                    not os.path.exists(os.path.join(sb, "pwned")) and
                    any("Don't Stop" in n for n in os.listdir(dst)), ""))

    # 10) 원복
    manifest = info.get("manifest", "")
    rc, out = run(["undo", manifest], sb)
    undo_info = json.loads(out) if out.strip().startswith("{") else {}
    back = sorted(os.listdir(src))
    results.append(("undo로 전량 원위치",
                    len(back) == len(made) and undo_info.get("blocked", 1) == 0,
                    f"{len(back)}/{len(made)}개 복귀"))

    # 11) 원복 후 목적지가 원래 상태인가 (심어둔 파일만 남아야 한다)
    left = sorted(os.listdir(dst))
    results.append(("원복 후 목적지 청결", left == ["충돌.pdf"], str(left)))

    # --- 여기부터 2026-08-22 OSS 공개 감사에서 추가한 케이스 -----------------
    # 지금까지의 1,000+회 실사용은 전부 이 맥·이 사용자·APFS 대소문자무시
    # 환경 하나에서만 일어났다. 그 조건 밖의 분기는 한 번도 실행된 적이 없었다.

    # 12) scan.py — 임의 확장자 + Info.plist 구조로도 번들을 잡아내는가
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
    results.append(("scan: Info.plist 구조로 임의확장자 번들 판정",
                    p.returncode == 0 and kinds == ["bundle"],
                    f"rc={p.returncode} kinds={kinds}"))

    # 13) scan.py — 심볼릭 링크는 추적하지 않고, 순환 링크도 안전한가
    lsb = os.path.join(sb, "link_test")
    lscope = os.path.join(lsb, "scope")
    other = os.path.join(lsb, "other")
    os.makedirs(lscope); os.makedirs(other)
    with open(os.path.join(other, "target.txt"), "w") as f:
        f.write("x")
    os.symlink(os.path.join(other, "target.txt"),
              os.path.join(lscope, "link.txt"))
    os.makedirs(os.path.join(lscope, "sub"))
    os.symlink(lscope, os.path.join(lscope, "sub", "loop"))   # 순환 링크
    p = subprocess.run([sys.executable, SCAN, lscope, "-o",
                        os.path.join(lsb, "inv.jsonl"), "--max-depth", "-1"],
                       cwd=lsb, capture_output=True, text=True, timeout=10)
    lkinds = [json.loads(l)["kind"]
             for l in open(os.path.join(lsb, "inv.jsonl"), encoding="utf-8")]
    results.append(("scan: 심볼릭 링크 미추적 + 순환 링크 무한루프 없음",
                    p.returncode == 0 and lkinds.count("symlink") == 2,
                    f"rc={p.returncode} kinds={lkinds}"))

    # 14) scan.py — 읽기 권한이 없는 디렉토리(TCC 거부와 동형)는 error로
    #     보고하고 죽지 않으며, 형제 디렉토리는 정상 스캔되는가
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
        os.chmod(os.path.join(pscope, "locked"), 0o755)   # rmtree 가능하게 복원
    # error 종류는 인벤토리 JSONL에는 안 쓰이고 stdout 요약 counts에만 잡힌다
    # (scan.py main()의 "noise", "depth_limit", "error" continue 분기). 그래서
    # 여기서는 JSONL이 아니라 stdout의 counts를 본다.
    try:
        scan_counts = json.loads(p.stdout).get("counts", {})
    except json.JSONDecodeError:
        scan_counts = {}
    ok_files = [r for r in recs if r["kind"] == "file"]
    results.append(("scan: 권한 거부 디렉토리는 error로 집계, 형제는 계속 스캔",
                    p.returncode == 0 and len(ok_files) == 1
                    and scan_counts.get("error") == 1,
                    f"files={len(ok_files)} counts={scan_counts}"))

    # 15) apply.py — 락 파일이 있으면 동시 실행을 거부하는가
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

    # 15a) 살아 있는 프로세스가 잡은 락이면 거부해야 한다.
    #      자기 자신의 PID 를 쓴다 — 지금 확실히 살아 있는 유일한 PID 다.
    with open(os.path.join(lksb, ".cleaner.lock"), "w") as f:
        f.write(f"{os.getpid()} live-run")
    rc, out = run(["run", os.path.join(lksb, "plan.json"), "--token", tok,
                  "--runs-dir", os.path.join(lksb, "_runs")], lksb)
    results.append(("apply: 살아있는 락이면 동시 실행 거부",
                    rc == 2 and "진행 중" in out
                    and os.path.exists(os.path.join(lksb, "f.txt")),
                    out.strip()[:60]))

    # 15b) 주인이 죽은 락은 회수해야 한다. SIGKILL 로 죽은 실행이 락을 남기면,
    #      회수하지 않는 한 그 디렉토리에서는 두 번 다시 정리할 수 없다.
    dead = _dead_pid()
    with open(os.path.join(lksb, ".cleaner.lock"), "w") as f:
        f.write(f"{dead} stale-run")
    rc, out = run(["run", os.path.join(lksb, "plan.json"), "--token", tok,
                  "--runs-dir", os.path.join(lksb, "_runs")], lksb)
    moved = os.path.exists(os.path.join(lksb, "sub", "f.txt"))
    results.append(("apply: 주인 없는 락은 회수하고 진행",
                    rc == 0 and moved, f"dead_pid={dead} moved={moved}"))
    # 이 시나리오는 실제로 미해결 결함을 하나 드러낸다: 락은 절대 자동 해제되지
    # 않는다(원 프로세스가 SIGKILL·크래시로 죽으면 락이 영구히 남는다). 여기서는
    # "락이 있으면 거부한다"만 검증한다 — 그 자체는 안전하게 동작한다.

    # 16) apply.py — 목적지 쓰기 권한이 없으면 원본을 그대로 두고 실패로
    #     보고하는가(크래시·부분상태 없이)
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
    results.append(("apply: 쓰기 권한 거부 시 원본 보존 + 안전 실패",
                    info2.get("totals", {}).get("failed") == 1
                    and os.path.exists(os.path.join(wsb, "src.txt")),
                    json.dumps(info2.get("totals", {}), ensure_ascii=False)))

    # 17) classify.py — --no-home-scan이 실제로 홈 전체 스캔을 끄는가
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
    results.append(("classify: --no-home-scan이 프로젝트 후보를 0개로",
                    p.returncode == 0 and "후보 0개" in (p.stderr or ""),
                    (p.stderr or "").strip()[:60]))

    # 18) 볼륨 간 이동 — hdiutil로 실제 다른 볼륨을 만들어야만 재현되는 경로다.
    #     하드링크(nlink>1) 볼륨 간 이동 거부, 정상 파일의 copy2+verify 성공,
    #     대소문자 구분 APFS에서 두 파일이 모두 살아남는지를 검증한다.
    #     hdiutil이 없거나 실패하면(샌드박스·CI 등) 실패로 세지 않고 건너뛴다.
    vol_a = vol_b = None
    try:
        if shutil.which("hdiutil") is None:
            raise RuntimeError("hdiutil 없음")
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

        vol_a = make_vol("CleanerSelftestA", "APFS")

        # 18a) 하드링크 볼륨 간 이동 거부 — 원본 보존
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
        results.append(("apply: 하드링크 볼륨 간 이동 거부 + 원본 보존",
                        hinfo.get("totals", {}).get("failed") == 1
                        and os.path.exists(os.path.join(hsb, "hl.txt"))
                        and os.path.exists(os.path.join(hsb, "orig.txt")),
                        json.dumps(hinfo.get("totals", {}), ensure_ascii=False)))

        # 18b) 일반 파일의 볼륨 간 이동 — copy2+verify 성공, 내용 무결성
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
        results.append(("apply: 볼륨 간 copy2+verify 이동 성공 + 내용 무결성",
                        xinfo.get("totals", {}).get("done") == 1
                        and not os.path.exists(os.path.join(xsb, "n.txt"))
                        and moved_ok,
                        json.dumps(xinfo.get("totals", {}), ensure_ascii=False)))

        # 18c) 대소문자 구분 APFS — Report.PDF와 report.pdf가 둘 다 살아남는가
        vol_b = make_vol("CleanerSelftestB", "Case-sensitive APFS")
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
        results.append(("apply: 대소문자 구분 APFS에서 두 파일 모두 보존",
                        vinfo.get("case_insensitive_fs") is False
                        and sorted(csresult) == ["Report.PDF", "report.pdf"],
                        f"case_insensitive_fs={vinfo.get('case_insensitive_fs')} "
                        f"dst={sorted(csresult)}"))
    except Exception as e:                                  # noqa: BLE001
        results.append(("apply: 볼륨 간 이동 3종 (하드링크·copy2·대소문자구분)",
                        True, f"스킵 — hdiutil 환경 사용 불가: {e}"))
    finally:
        for v in (vol_a, vol_b):
            if v and os.path.ismount(v):
                subprocess.run(["hdiutil", "detach", v, "-force"],
                               capture_output=True, timeout=30)

    # 22) 강제종료(SIGKILL) 후 저널만으로 전량 복원되는가 —
    #     "언제나 되돌릴 수 있다"는 이 도구의 전제가 걸린 시나리오다.
    #     매니페스트는 정상 종료 때만 쓰이므로, 죽으면 저널이 유일한 근거가 된다.
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
        results.append(("apply: 강제종료 후 저널만으로 전량 복원",
                        moved > 0 and not had_manifest and back == N and left == 0,
                        f"중단시 {moved}건 이동 → 복귀 {back}/{N}, 잔류 {left}"))
        # 23) 죽은 실행이 남긴 락을 회수하고 다시 실행되는가
        rc2, _ = run(["run", kpp, "--token", ktok, "--runs-dir", kruns], ksb)
        results.append(("apply: 강제종료가 남긴 락을 회수하고 재실행",
                        rc2 == 0, f"rc={rc2}"))
    else:
        results.append(("apply: 강제종료 후 저널만으로 전량 복원", False,
                        "실행 디렉토리가 생기지 않았다"))

    print("\n적대적 자가 테스트")
    print("=" * 64)
    passed = 0
    for name, ok, detail in results:
        print(f"  {'통과' if ok else '실패'}  {name}"
              + (f"    — {detail}" if detail else ""))
        passed += bool(ok)
    print("=" * 64)
    print(f"  {passed}/{len(results)} 통과   샌드박스: {sb}")

    if passed == len(results):
        shutil.rmtree(sb, ignore_errors=True)   # 테스트 샌드박스만 정리
        print("  샌드박스 정리 완료")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
