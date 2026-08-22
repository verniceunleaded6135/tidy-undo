# 성능·추출 경로 실측치

2026-08-22 macOS 26.5 (Darwin 25.5.0) / python3 3.14.4 환경에서 실측.
새 환경에서는 달라질 수 있으니 이상하면 다시 재보라.

## 파이프라인 소요 시간

| 단계 | 대상 | 실측 |
| --- | --- | --- |
| 스캔 + 앞부분 해시 | 1,510 파일 | 0.2초 |
| 3단계 중복 탐지 | 1,510 파일 | 3.1초 |
| 프로젝트 귀속 (규칙) | 1,374 파일 · 프로젝트 89개 | 0.1초 |
| 텍스트 헤드 전량 추출 | 917 파일 | 28.5초 |
| 중복 탐지 (홈 전체) | 263,041 파일 | 74초 |

전 파이프라인이 몇 분 안에 끝난다. 비용은 제약이 아니다.

## 포맷별 텍스트 추출 최적 경로

| 포맷 | Spotlight 색인율 | 최적 경로 | 속도 |
| --- | --- | --- | --- |
| PDF (텍스트) | 58% | `pypdf` 앞 3페이지 | 63ms |
| PDF (스캔본) | 2% | `sips`→PNG→Vision OCR | 720ms |
| **HWP** | **0%** | **자체 CFB 파서** (`hwp_stdlib.py`, 의존성 0) | **4ms** |
| **HWPX** | **0%** | `unzip -p Contents/section*.xml` | 2ms |
| PPTX | 67% | `unzip -p ppt/slides/slide*.xml` | 3ms |
| DOCX | 100% | `unzip -p word/document.xml` | 2ms |
| XLSX | 67% | `unzip -p xl/sharedStrings.xml` | 11ms |
| MD/TXT/코드 | 100% | 직접 read | 0ms |
| 이미지 | Live Text로 색인됨 | `mdfind` 또는 Vision OCR | — |

**HWP·HWPX의 Spotlight 색인율이 0%**라는 점이 중요하다. 한컴 파일이 많은
환경에서 `mdfind`는 그 파일들에 대해 완전히 눈이 먼다. 자체 추출기가 대체
불가능한 유일한 지점이다. HWP 파서는 144개 전수에서 실패 0.

## Spotlight로 할 수 있는 것

값싸고 정확하므로 굳이 직접 구현하지 마라.

```bash
mdfind -onlyin <dir> "kMDItemTextContent == '*예산*'c"        # 내용 검색 0.4초
mdfind -onlyin <dir> 'kMDItemLastUsedDate < $time.now(-15552000)'  # 180일 미사용
mdfind -onlyin <dir> 'kMDItemFSSize > 50000000'               # 50MB 초과
mdfind "kMDItemIsScreenCapture == 1"                          # 스크린샷만

# 메타데이터는 반드시 다중 파일 1회 호출 — 파일별 루프보다 약 1,000배 빠르다
mdls -name kMDItemWhereFroms -name kMDItemLastUsedDate -name kMDItemUseCount <files...>
```

`kMDItemWhereFroms`는 다운로드 출처 URL을 담는다(보유율 약 64%). 어느
사이트에서 받았는지가 주제 귀속의 강한 단서가 된다.

주의: `kMDItemLastUsedDate`는 결측률이 높다(약 48%). "안 쓰는 파일" 판정을
이 값 하나로 내리지 마라. `kMDItemDateAdded`·mtime·`kMDItemUseCount`를 함께 보라.

## 사용 가능한 것 / 없는 것

사전 설치됨: `pypdf` `pdfplumber` `pdfminer` `python-docx` `python-pptx` `PIL`,
CLI로 `mdfind` `mdls` `xattr` `trash` `jq` `unzip` `xmllint` `sips` `swiftc`.

없음: `fdupes` `exiftool` `tag` `fswatch` `openpyxl` `olefile`.
homebrew python은 PEP 668이라 `pip install`이 직접 안 되므로 venv가 필요하다.
다만 현재 필요한 것은 전부 이미 있거나 자체 구현으로 대체된다.

## 할 수 없는 것

- **상시 감시·실시간 폴더 감시.** 스킬은 호출될 때만 동작한다. 배치 호출이
  올바른 설계이고, 상주가 정말 필요하면 Hazel이나 LaunchAgent의 영역이다.
- **사진 라이브러리 내부 정리.** `Photos Library.photoslibrary`는 사진 앱이
  소유하는 데이터베이스다. 파일시스템에서 건드리면 깨진다.
- **`kMDItemTextContent` 값 읽기.** 항상 `(null)`을 반환한다.

## 규칙 기반 도구와의 경계

Hazel·organize·Folder Action이 이미 잘 하는 것을 다시 만들지 마라 —
확장자·크기·날짜 매칭, 폴더 상시 감시, 반복 실행되는 정형 룰.

LLM이 유일하게 할 수 있는 것은 넷이다.
1. 룰이 하나도 없는 상태에서 **분류 체계 자체를 제안**하는 것
2. 사용자의 **프로젝트명·어휘를 읽어 그 사람의 언어로** 폴더를 짓는 것
3. 룰로 표현 불가능한 잔여물에 **가설과 근거를 붙여** 사용자에게 넘기는 것
4. 발견한 패턴을 **룰로 굳혀 내보내** 자기 호출을 줄이는 것
