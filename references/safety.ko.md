# 안전 설계 상세

SKILL.md의 원칙을 구현할 때 참조한다. 실행기(`scripts/apply.py`)를 수정하거나
새 작업 종류를 추가할 때 이 문서를 먼저 읽어라.

## 목차
1. 사고 시나리오와 방어 대응표
2. 보호 경로
3. 위험 등급 게이트
4. 매니페스트가 반드시 담아야 하는 것
5. macOS 고유 함정

---

## 1. 사고 시나리오와 방어 대응표

| 사고 | 어떻게 발현되나 | 방어 | 구현 위치 |
| --- | --- | --- | --- |
| 잘못된 대상에 실행 | "다운로드 정리"인데 홈 전체를 스코프로 잡음 | root 1개 강제, `~`·`/`·`/Users`·`/Volumes` 거부 | `apply.preflight` |
| git 워킹트리 파괴 | 프로젝트 안 `*.md`를 문서로 분류해 이동 | 프로젝트 마커 보유 디렉토리는 불가분 단위, 진입 금지 | `scan.has_project_marker` |
| 의존성 폭발 | `node_modules` 수만 파일이 스캔에 유입 | 이름 기반 가지치기 | `scan.ALWAYS_SKIP` |
| 앱 번들 분해 | `.app`·`.photoslibrary` 내부로 진입해 리소스를 흩뿌림 | 확장자 + `Contents/Info.plist` 구조 판정 | `scan.is_bundle` |
| 사진 라이브러리 파손 | `~/Pictures` 9만 개를 정리 대상으로 인식 | 패키지 판정으로 1항목 축소 + 스코프에서 제외 | `scan.is_bundle` |
| 심볼릭 링크 탈출 | 링크가 스코프 밖 실체를 가리킴 | `follow_symlinks=False`, 링크 자체만 1항목 | `scan.walk` |
| 순환 링크 무한 재귀 | 디렉토리 링크가 상위를 가리킴 | 방문한 `(dev, ino)` 집합 유지 | `scan.walk` |
| 하드링크 손상 | 볼륨 간 복사로 링크 관계가 끊김 | `nlink > 1`이면 볼륨 간 이동 거부 | `apply.move_noclobber` |
| iCloud 대량 다운로드 | placeholder를 열어 수십 GB를 받음 | `st_flags & SF_DATALESS` 판정 후 열지 않음 | `scan`, `apply.preflight` |
| 이름 충돌 덮어쓰기 | `os.rename`/`shutil.move`가 조용히 덮어씀 | `os.link`(EEXIST 실패) + `unlink` | `apply.move_noclobber` |
| 대소문자 무시 FS 충돌 | `A.pdf`가 `a.pdf`를 덮어씀 | 런타임 프로브 + 형제 목록 대조 | `apply.exists_ci` |
| 한글 NFD 불일치 | 정규화한 문자열로 파일을 못 찾음 | `path`는 원본 그대로, 비교만 `path_nfc` | `scan` 전반 |
| 셸 인젝션 | 따옴표·세미콜론 포함 파일명이 명령으로 해석 | 셸 문자열 조립 없음. `os`/`shutil` 직접 호출 | 전 스크립트 |
| 중단 시 절반만 이동 | 어디까지 갔는지 알 수 없음 | 이동 전 저널에 기록 + `fsync` | `apply.jwrite` |
| 동시 실행 충돌 | 두 정리가 같은 폴더를 건드림 | root에 락 파일 | `apply.run` |
| 스캔 이후 파일 변경 | 계획의 전제가 무너짐 (TOCTOU) | size·mtime 스냅샷 대조, 불일치 시 스킵 | `apply.preflight` |
| 작업 중 파일 이동 | 저장 중 문서·진행 중 다운로드가 손상 | 90초 이내 수정분과 `.crdownload` 류 제외 | `scan`, `apply.preflight` |
| 디스크 풀 | 볼륨 간 이동 중 공간 소진 | 필요량 × 1.2 + 5GB 확인 후 진행 | `apply.preflight` |
| 승인 후 계획 변조 | 승인받은 것과 다른 것이 실행됨 | 토큰 = 계획 해시. 불일치 시 거부 | `apply.plan_token` |
| 인지적 손실 | "정리했더니 못 찾겠다" | 최소 변경 + `_CLEANER_MAP.md` + 사용자 어휘로 폴더명 | 8단계 |

## 2. 보호 경로

**하드 차단** — 스코프로 지정 불가, 진입 불가:
```
/  /System/**  /Library/**  /private/**  /usr/**  /bin/**  /sbin/**  /etc/**
/var/**  /Applications/**  /Volumes  /Users  /Users/Shared/**
~  ~/Library/**  ~/.Trash/**  ~/.ssh/**  ~/.gnupg/**  ~/.aws/**  ~/.config/**
~/.claude/**  ~/CLAUDE.md
~/Library/Mobile Documents/**   ← iCloud. 여기서 옮기면 모든 기기에 전파된다
~/Library/CloudStorage/**       ← Dropbox·OneDrive·Google Drive 마운트
```

터미널에 전체 디스크 접근 권한이 있으면 `~/Library/Mail`·`Messages`·`Safari`·
`AddressBook`을 **읽을 수 있다**. 정리 대상은 물론 인덱싱·요약 대상에서도 제외하라.

**불가분 컨테이너** — 스코프 안이어도 내부 진입 금지:
프로젝트 마커 보유 디렉토리, 의존성/빌드 산출물, 모든 macOS 패키지,
사진·음악·영상 라이브러리.

**개별 제외** — 스코프 안이지만 손대지 않는 것:
`.DS_Store` `.localized` `Icon\r` `._*` `*.crdownload` `*.part` `~$*` `.*.swp`,
iCloud placeholder, 90초 이내 수정분, 스코프 밖을 가리키는 심링크.

## 3. 위험 등급 게이트

| 등급 | 범위 | 되돌리기 | 필요 승인 |
| --- | --- | --- | --- |
| L0 읽기 | 스캔·중복탐지·분류 제안·리포트 | 불필요 | 스코프 확인만 |
| L1 이동 | 새 폴더 생성 + 그 안으로 이동 | 매니페스트 역재생 | 계획 제시 + 명시 승인 |
| L2 격리 | 중복본을 격리 폴더로 | 완전 원복 | L1 승인 + 격리 목록 재확인 |
| L2.5 휴지통 | 격리분을 `trash`로 | Finder에서 수동 복원 | 별도 세션·별도 승인 |
| L3 원본 변경 | 리네임·압축해제·볼륨 간 이동 | 종류별로 다름 | 항목 단위 개별 승인 |
| L4 영구 삭제 | **존재하지 않음** | — | 어떤 승인으로도 불가 |

"알아서 다 해줘"는 L1을 넘지 못한다. 한 실행은 한 등급만 수행한다 —
L1과 L2를 섞으면 사용자가 무엇을 승인했는지 흐려진다.

## 4. 매니페스트가 반드시 담아야 하는 것

이 코드가 없어도 사람이 손으로 원복할 수 있어야 한다.

| 요건 | 필드 |
| --- | --- |
| 원경로 | `operations[].src` (원본 그대로. 정규화본으로는 NFD 파일을 못 찾는다) |
| 실제 도착지 | `dst_actual` — 의도(`dst_intended`)와 반드시 구분 |
| 충돌 전말 | `collisions` — 몇 번 비켜갔는가 |
| 무결성 | `snapshot.size`, `snapshot.mtime` |
| 되돌리기 순서 | `seq` 역순 재생 |
| 미처리 사유 | `skipped[].skip_reason` — 침묵 스킵 금지 |
| 새로 만든 폴더 | `created_dirs` — 원래 있던 폴더는 지우면 안 된다 |

## 5. macOS 고유 함정

- **APFS는 정규화를 무시하고 비교**하므로 NFC 문자열로도 NFD 파일이 열린다.
  하지만 exFAT·SMB 외장 볼륨은 그렇지 않다. 원본 문자열을 버리지 마라.
- **APFS 클론** 때문에 논리 크기 합계가 실제 점유보다 크다. 절약량을 보고할 때
  "논리 기준"임을 밝혀라.
- **`kMDItemTextContent`는 값을 읽을 수 없다.** `mdls`로 조회하면 항상 `(null)`.
  Spotlight는 "이 단어가 있나?" 질문에만 답한다. 본문이 필요하면 자체 추출.
- **`/private/tmp`는 Spotlight 미색인.** 홈 경로에서만 태그·검색이 동작한다.
- **`/usr/bin/trash`는 Apple 순정**이며 `~/.Trash/`로 이동한다. 파일은 남는다.
