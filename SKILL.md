---
name: cleaner
description: Safely tidy local files on macOS. Sorts by format, attributes files to a project or topic from their names and (rarely) their contents, detects exact duplicates and cloned folders, untangles version sprawl, and flags sensitive documents left lying around. It always shows a plan and waits for explicit approval before moving anything, and every run is 100% reversible with a single command. There is no delete: files go to a quarantine folder, or to the Trash only when the user asks for that separately. Triggers — "organize my downloads", "clean up this folder", "tidy my desktop", "find duplicate files", "clean up the duplicates", "what is eating my disk", "this folder got copied twice", "sort these files", "group my files by project", "which project is this file from", "clean up v1 v2 v3", "sort out final_final_v2", "get the old versions out of the way", "show me what has piled up", "show me the cleanup plan", "undo the cleanup", "run the cleanup again", "do another folder too", "move the quarantined files to the Trash", "where are my sensitive files", "find my ID scans and certificates", "cleaner", "다운로드 폴더 정리", "파일 정리해줘", "바탕화면 정리", "중복 파일 찾아", "중복 파일 정리", "용량 차지하는 파일", "디스크 정리", "같은 파일 여러 개", "폴더가 두 개로 복사됐어", "이 파일들 분류해줘", "확장자별로 정리", "프로젝트별로 자료 모아줘", "이 자료 어느 프로젝트 거야", "v1 v2 v3 정리", "최종 최종본 정리", "옛날 버전 치워", "어떤 파일이 쌓여있는지 보여줘", "파일 정리 계획 보여줘", "정리 되돌려", "undo", "방금 정리 취소", "정리 다시", "다른 폴더도 정리", "격리한 거 휴지통으로", "민감한 파일 어디 있나", "인감증명서 신분증 같은 거 찾아". Follow-up work belongs here too — "revise the plan", "drop item N", "rescan", "widen the scope", "reclassify this into that project", "계획 수정", "이 항목만 빼고", "다시 스캔", "N번 제외". Do not use this skill to find a single file; Spotlight already does that. Reading file contents to summarize or write from them, and editing or producing PDF/PPTX files, belong to their own dedicated skills.
---

# cleaner — local file cleanup

## Why this skill exists

Finder and Spotlight already find **the one file whose name you know**, in about a
second. Do not rebuild that. What this skill does instead is the part a person
cannot do by eye: **finding relationships**.

- Whether two folders of 279 items hold the same content — comparing by hand is hours.
- Which `v7` is the successor of which `최종` ("final") — the filenames alone will not say.
- Which project a given PDF belongs to — you need the list of projects to decide.
- What is actually written inside an HWP/HWPX file — **Spotlight does not index
  Hancom documents at all.** (HWP and HWPX are the formats of Hancom Office, the
  word processor that dominates Korean government, academia, and public institutions.)

Concentrate on those four. Sorting everything into folders by file extension is
nearly worthless: measured over 1,510 real files in this user's `~/Downloads`,
`~/Desktop`, and `~/Documents`, only **1.3%** could be disposed of by extension
alone, **95.4%** were decidable once you read the filename, and **3.4%** needed the
contents opened. For someone who writes descriptive filenames, a `PDF/` folder adds
no information and only makes the paths longer.

## Absolute rules

1. **Never delete.** There is no `rm`, `os.remove`, or `rmtree` anywhere in the
   scripts. "Deleting" means moving into a quarantine folder, and the Trash happens
   only when the user asks for it separately.
2. **Never act without a plan.** Show the plan, get approval, then move. The
   approval token is a hash of the plan file, so editing the plan after approval
   makes the runner refuse to execute it.
3. **Move only what can be moved back.** Every run writes a manifest, and that
   manifest alone is enough to restore the previous state.
4. **Never leave the scope.** One directory chosen by the user is the whole world.
   `~`, `/`, `/Users`, and `/Volumes` cannot be given as a scope.
5. **When in doubt, do not move.** Leave undecidable files where they are and report
   them as a list. A misfiled file is physically intact and still lost to the user —
   that is the most common way this kind of work fails.

## Workflow

Everything in `scripts/` is deterministic. The LLM is used only for the judgment
part of step 4. Scanning, hashing, duplicate detection, and moving are far faster
and more accurate in Python, so do not do them yourself.

### Step 0 — Fix the scope (always ask the user)

Settle which directory to clean. The usual candidates are `~/Downloads`,
`~/Desktop`, and `~/Documents`. One run handles **exactly one root**.

If asked to clean `~/Pictures`, refuse and explain why: almost everything in it
lives inside the `Photos Library.photoslibrary` package, and touching that from the
filesystem corrupts the photo library. Photo cleanup belongs in the Photos app.

### Step 1 — Scan (read-only)

```bash
python3 scripts/scan.py <target> -o _work/inventory.jsonl --max-depth -1
```

Packages (`.app`, `.photoslibrary`, `.rtfd`) count as a single file, and project
directories (those holding `.git`, `package.json`, and similar markers) are treated
as indivisible units that the scanner does not descend into. Symlinks are not
followed, and iCloud files that are not downloaded locally are never opened.
1,500 files take 0.2 seconds.

### Step 2 — Detect relationships (read-only)

```bash
python3 scripts/dedup.py _work/inventory.jsonl -o _work/dupes.json
python3 scripts/classify.py _work/inventory.jsonl -o _work/classified.jsonl
```

`dedup.py` produces three things: exact-duplicate groups, cloned folder pairs, and
version lineages. `classify.py` builds a vocabulary from the names of the project
directories in the home folder and the filenames inside them, and attributes files
with it. Rules settle 30–40% of the files; the rest goes to step 4.

### Step 3 — Report back to the user

Lead with the numbers: "331 duplicate groups / 365 redundant copies / 2.1 GB",
"8 cloned folder pairs", "70 version lineages". Then name the biggest clusters
concretely. The user does not want the full list — they want to know **what the
problem is**.

### Step 4 — Judge the leftovers (the only place an LLM is needed)

Read **only the filenames** of the items `classify.py` left as `unresolved` or
`ambiguous`, and decide what they belong to. Filenames in this kind of environment
are descriptive, so the name alone decides most cases.

**Open the contents only when the filename genuinely cannot tell you** — screenshots,
`KakaoTalk_Photo_*` (images saved out of the KakaoTalk messenger), UUID-shaped names.
Those usually stay opaque even after you open them, so rather than forcing a
classification, leave them in `_미분류/` (unclassified).

If you really need the contents:

```bash
python3 scripts/extract.py --from-inventory _work/inventory.jsonl --only pdf,hwp,hwpx --jsonl
```

This pulls the head of pdf, hwp, hwpx, pptx, docx, xlsx, and md files. 1,000 files
in under 30 seconds. **HWP and HWPX are not indexed by Spotlight at all, so `mdfind`
cannot find them** — that is why this extractor is irreplaceable in an environment
full of Hancom documents.

Record your decisions by filling the `project` and `confidence` fields in
`classified.jsonl`.

If more than 200 items are unresolved, hand them to a `general-purpose` subagent to
protect the main context; the role definition is in
`references/agent-file-classifier.md`, so pass its contents as the prompt. Below 200,
judging them directly costs less than the round trip.

### Step 5 — Build the plan

```bash
python3 scripts/plan.py --inventory _work/inventory.jsonl \
  --classified _work/classified.jsonl --dupes _work/dupes.json \
  --root <target> -o _work/plan.json --do archive,quarantine,version
```

`--do` selects the kinds of operation. Doing one kind at a time keeps the unit of
approval sharp — for duplicates only, that is `--do quarantine`.

**A rule verdict on its own no longer moves a file.** Rules only narrow the
candidates; the decision to move rests exclusively on a verdict that came from
reading the filename (`llm`, `audited`, or `user`). The reason is measured: of the
20 misattributions the auditor caught in a real run, **all 20 came from rule
verdicts and 0 came from LLM verdicts** — a design-pack folder named
`ppt-samsung-ir-restrained`, for instance, was vacuuming up unrelated Samsung SDS
presentation material by token overlap alone. Rule-only items are therefore emitted
as a separate list ("rule verdict only — needs a filename read") instead of being
planned as moves; feed that list back through step 4. `--trust-rules` restores the
old behavior, but do not use it.

### Step 6 — Audit, then get approval (not skippable)

Before the plan is shown to the user, have it reviewed adversarially. The role
definition is in `references/agent-cleanup-auditor.md`. **Always run this as a
subagent — for every plan, whatever its size.** This is not a threshold rule: the
audit pass is precisely what caught the misattributions described in step 5, and the
side that wrote the plan cannot see the holes in its own reasoning.

On `reject`, rebuild the plan. On `revise`, drop the flagged items and rebuild.
Only `pass` moves forward.

```bash
python3 scripts/apply.py verify _work/plan.json
```

This prints the token and the pre-flight results. Always show the user, together:
total operations · total size · **the absolute path of the scope root** · how many
new folders will be created · which items are skipped and why · the one-line undo
command.

Then **get explicit approval.** "Just do whatever" is not approval. Approval is the
user seeing the plan and answering "go ahead with this".

### Step 7 — Execute

```bash
python3 scripts/apply.py run _work/plan.json --token <token> --runs-dir _work/_runs
```

Files move by `os.link` + `unlink` — the link fails if the destination already
exists, which makes overwriting structurally impossible, and the original only
disappears once the new location is confirmed. Name collisions step aside to
` (2)`. Every move is written to disk before it happens, so an interrupted run still
records how far it got.

### Step 8 — Leave a map

Write `_CLEANER_MAP.md` at the top of the folder you cleaned: a tree of the new
structure and a human-readable account of what went where. A cleanup that loses
nothing physically but leaves the user unable to find things has failed.

## Undo

```bash
python3 scripts/apply.py undo _work/_runs/<run_id>/manifest.json
python3 scripts/apply.py undo _work/_runs/<run_id>/          # a directory works too
```

Undo replays the operations in reverse. Files whose contents changed after the
cleanup, and slots where a different file has since appeared, are left untouched and
reported with the reason. Only folders this run created are removed; folders that
already existed stay.

## Moving quarantined files to the Trash (only on separate request)

Do this only after the user has inspected the quarantine folder themselves and asked
for it. Never chain it onto the same run — it blurs the unit of approval.

```bash
/usr/bin/trash -v <paths>
```

macOS's `trash` does not support `--` (the end-of-options marker); passing it makes
`trash` treat it as a filename and error out. Instead call it from Python with
`subprocess.run([...], shell=False)` and an argument array — no shell is involved, so
it is safe except for filenames that begin with `-`, and spaces and quotes pass
through untouched.

Once files are in the Trash, their original locations exist only in the manifest.
Say so before you do it.

## What not to do

| Don't | Why |
| --- | --- |
| Sort everything into folders by extension | Only 1.3% of files are disposable by extension alone. A `PDF/` folder adds no information |
| Auto-archive by mtime | The downloads folder is an active work surface. Nothing guarantees that old means unimportant |
| Auto-rename files | It destroys the user's own conventions — `260812_`, `v7`, `3교본` ("proof copy 3") — and breaks the match with their memory |
| Parse the contents of every file | The share that needs it is small, while the cost and the misclassification risk are large. The filename comes first |
| Assume `최종` ("final") means newest | `v6` and `최종` routinely have different hashes. Decide by timestamp and hash |
| Rearrange the inside of a project directory | It breaks the git working tree and uncommitted work disappears |
| Reorganize bulk source material inside a folder | Material already sorted by ministry or by source gets scattered. Remove only the duplicated folders |

## Sensitive-file check (a report, not a classification)

Seal certificates (`인감증명서`, the notarized personal seal certificate used for
contracts in Korea), scans of ID cards, recovery codes, contracts, and litigation
material routinely sit in plain sight in the downloads folder. When you find them,
**report them as a list — do not move them.** Only the user can decide where they
should go.

## Self-test

If you modified the runner, then before it touches a single user file:

```bash
python3 scripts/selftest.py
```

It verifies 23 cases in a sandbox — NFD-decomposed Hangul filenames, spaces, quotes,
newlines in names, name collisions, forged tokens, plans tampered with after
approval, symlinks and cyclic links, hard links, permission denials, lock reclaim,
journal recovery after a SIGKILL, and, via `hdiutil`, a real disk image to exercise
cross-volume moves and a **case-sensitive** filesystem. Anything short of a full pass
means you do not touch user files.

## Team composition

This skill runs almost entirely on deterministic scripts and keeps human-shaped roles
at the two points that need judgment. Using an LLM for scanning, hashing, duplicate
detection, or moving is slow, inaccurate, and expensive.

| Role | When | Definition |
| --- | --- | --- |
| Attribution judge | Step 4, when more than 200 items are unresolved | `references/agent-file-classifier.md` |
| Plan auditor | Step 6, **always** | `references/agent-cleanup-auditor.md` |

The two roles never need to talk to each other (the pipeline is one-directional:
judge → plan → audit). So call them as subagents rather than as a team, and pass
results between them as files. Project copies live in
`.claude/agents/`.

## Test scenarios

**Happy path** — "clean up my downloads folder"
→ confirm scope → scan → detect duplicates and attributions → report the numbers →
judge the leftovers → build the plan → audit passes → present the plan → approval →
execute → write the map → show the undo command.

**Error path** — after approval, the user says "drop item 3"
→ rebuild the plan (the token changes, so the previous approval is void) → re-verify
→ re-approve. Executing with the old token is refused by the runner. That is the
design working; do not work around it, get approval again.

**Error path** — the run is interrupted (including SIGKILL or power loss)
→ pass the **run directory** to `undo`. With no manifest, it restores from the journal.

```bash
python3 scripts/apply.py undo _work/_runs/<run_id>/
```

The manifest is only written on a clean exit, so a SIGKILL would otherwise leave
hundreds of already-moved files with no input to undo them. The journal is fsynced
**before every single move**, which is enough on its own to restore (verified by
killing a 3,000-operation plan midway and restoring all of it). A `.cleaner.lock`
left behind by a dead run is reclaimed by the next run after it checks whether the
recorded PID is still alive.

After a restore, start over from the beginning rather than resuming — the snapshot
taken at scan time is already out of date.

## References

- Accident scenarios and defensive design in full: `references/safety.md`
- Measured performance and per-format extraction paths: `references/capabilities.md`
- Korean originals of every document here: `*.ko.md` alongside them
