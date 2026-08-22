#!/usr/bin/env python3
"""Sensitive-file detection — identifies identity, financial and credential
documents from their filenames.

It never opens file contents. The more sensitive a document is, the more
inappropriate it is to skim it, and filenames alone identify this class of
document well enough.

Its default purpose is to REPORT, not to classify. Only the user can decide
where these files should go.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata

# The Korean literals in the patterns below are FILENAME-MATCHING DATA, not UI
# text — they are the words that actually appear in Korean official-document
# filenames (인감증명 = certificate of seal impression, 주민등록 = resident
# registration, and so on). Translating them would delete the feature.
#
# Higher level == more damaging and more immediate if leaked.
PATTERNS = [
    # Level 3 — a leak leads straight to account or asset takeover
    (3, "자격증명", re.compile(
        r"(recovery.?code|backup.?code|2fa|otp|비밀번호|password|passwd|"
        r"credential|api.?key|secret|private.?key|\.pem$|\.p12$|\.keystore$|"
        r"공동인증서|공인인증서|비밀번호부|계정정보)", re.I)),
    # Level 2 — directly usable for identity theft
    (2, "신원", re.compile(
        r"(인감증명|주민등록(등본|초본)|주민등록증|신분증|여권|passport|"
        r"운전면허|가족관계(증명|등록부)|기본증명서|혼인관계|외국인등록|"
        # Military service records, signature images and registry extracts were
        # missed by the earlier revision. A signature image is directly usable
        # for forgery, so it is graded the same as a certificate.
        r"병적증명|주민번호|서명\.(jpe?g|png|heic)|등록원부)", re.I)),
    (2, "금융", re.compile(
        r"(통장사본|계좌|잔고증명|소득금액증명|원천징수|급여명세|"
        r"신용카드|카드번호|납세증명|세금계산서|금전소비대차|"
        r"반납금|납부방법|이체신청|자동이체|퇴직금|연금)", re.I)),
    # Level 1 — personal information, but the immediate harm is lower.
    # Requires a case number (e.g. 2025고합1620) or a word specific to "my own
    # lawsuit". Matching 판결문 (court ruling) alone would sweep in bulk work
    # material such as open judicial datasets — that is not sensitive data.
    (1, "법적분쟁", re.compile(
        r"(\d{4}\s?(고합|고단|고정|가합|가단|가소|나|다|드단|드합|재)\s?\d+|"
        r"소송위임|고소장|고발장|답변서|준비서면|내용증명|지급명령|"
        r"재판부\s?설명|합의서|"
        # Cases where the filename is the title of a complaint itself, e.g.
        # '손해배상청구의 소' (action for damages). The earlier revision missed
        # this, leaving real litigation papers filed as ordinary documents.
        r"손해배상|청구의\s?소|소장|조정신청|가압류|가처분)", re.I)),
    # `nda` sits inside common English words such as Agenda and pretendard.
    # Word boundaries are mandatory here.
    (1, "계약", re.compile(
        r"(계약서|약정서|각서|근로계약|용역계약|비밀유지|\bNDA\b)")),
    (1, "증명서", re.compile(
        r"(재직증명|경력증명|졸업증명|학위증명|수료증명|성적증명|자격증)", re.I)),
    (1, "의료", re.compile(r"(진단서|소견서|처방전|건강검진|진료기록)", re.I)),
    (1, "행정증명", re.compile(
        r"(자격득실|건강보험자격|고용보험|국민연금가입|사업자등록증|"
        r"법인등기부|주주명부)", re.I)),
]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sensitive-file detection (read-only)")
    ap.add_argument("inventory")
    ap.add_argument("-o", "--out")
    ap.add_argument("--min-level", type=int, default=1)
    args = ap.parse_args()

    found = []
    with open(args.inventory, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("kind") != "file":
                continue
            name = unicodedata.normalize("NFC", r["name"])
            for level, kind, rx in PATTERNS:
                if level >= args.min_level and rx.search(name):
                    # sens_kind stays Korean on purpose: plan.py builds the
                    # destination folder name out of it (e.g. "2_신원/"), so
                    # translating it would break existing users' folder trees.
                    found.append({**r, "sensitivity": level, "sens_kind": kind,
                                  "matched": rx.search(name).group()})
                    break

    found.sort(key=lambda x: (-x["sensitivity"], x["name"]))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for r in found:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_kind: dict[str, int] = {}
    for r in found:
        k = f"L{r['sensitivity']} {r['sens_kind']}"
        by_kind[k] = by_kind.get(k, 0) + 1
    print(json.dumps({"total": len(found), "by_kind": by_kind},
                     ensure_ascii=False, indent=2))
    for r in found:
        print(f"  [{r['sensitivity']}] {r['sens_kind']:6} {r['name'][:62]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
