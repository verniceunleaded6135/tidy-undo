#!/usr/bin/env python3
"""민감 파일 탐지 — 신원·금융·자격증명 문서를 파일명으로 식별한다.

내용을 열지 않는다. 민감 파일일수록 열어서 훑는 행위 자체가 부적절하고,
파일명만으로도 이런 문서는 충분히 식별된다.

분류가 아니라 '보고'가 기본 용도다. 어디로 옮길지는 사용자만 결정할 수 있다.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata

# 등급이 높을수록 노출 시 피해가 크고 즉각적이다.
PATTERNS = [
    # 3등급 — 유출 즉시 계정·자산 탈취로 이어진다
    (3, "자격증명", re.compile(
        r"(recovery.?code|backup.?code|2fa|otp|비밀번호|password|passwd|"
        r"credential|api.?key|secret|private.?key|\.pem$|\.p12$|\.keystore$|"
        r"공동인증서|공인인증서|비밀번호부|계정정보)", re.I)),
    # 2등급 — 신원 도용에 직접 쓰인다
    (2, "신원", re.compile(
        r"(인감증명|주민등록(등본|초본)|주민등록증|신분증|여권|passport|"
        r"운전면허|가족관계(증명|등록부)|기본증명서|혼인관계|외국인등록|"
        # 병적증명서·서명 이미지·등록원부는 앞선 판본이 놓쳤다. 서명 이미지는
        # 위조에 직접 쓰이므로 증명서류와 같은 등급으로 다룬다.
        r"병적증명|주민번호|서명\.(jpe?g|png|heic)|등록원부)", re.I)),
    (2, "금융", re.compile(
        r"(통장사본|계좌|잔고증명|소득금액증명|원천징수|급여명세|"
        r"신용카드|카드번호|납세증명|세금계산서|금전소비대차|"
        r"반납금|납부방법|이체신청|자동이체|퇴직금|연금)", re.I)),
    # 1등급 — 개인정보이나 즉각적 피해는 낮다
    # 사건번호(2025고합1620)나 '나의 소송' 성격의 단어를 요구한다. `판결문`만으로는
    # 사법 데이터 개방 같은 업무자료가 대량으로 걸린다 — 그건 민감정보가 아니다.
    (1, "법적분쟁", re.compile(
        r"(\d{4}\s?(고합|고단|고정|가합|가단|가소|나|다|드단|드합|재)\s?\d+|"
        r"소송위임|고소장|고발장|답변서|준비서면|내용증명|지급명령|"
        r"재판부\s?설명|합의서|"
        # '손해배상청구의 소' 처럼 소장 제목 자체가 파일명인 경우. 앞선 판본이
        # 이걸 놓쳐 실제 소송 서류가 일반 파일로 남았다.
        r"손해배상|청구의\s?소|소장|조정신청|가압류|가처분)", re.I)),
    # `nda`는 Agenda·pretendard 처럼 흔한 영어 단어 안에 들어 있다. 단어 경계 필수.
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
    ap = argparse.ArgumentParser(description="민감 파일 탐지 (읽기 전용)")
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
        k = f"{r['sensitivity']}등급 {r['sens_kind']}"
        by_kind[k] = by_kind.get(k, 0) + 1
    print(json.dumps({"total": len(found), "by_kind": by_kind},
                     ensure_ascii=False, indent=2))
    for r in found:
        print(f"  [{r['sensitivity']}] {r['sens_kind']:6} {r['name'][:62]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
