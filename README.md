# tidy-undo

**Sort a 1,200-file Downloads folder into your own project names. Open the files
whose names tell you nothing and rename them to what they actually are. Undo all
of it with one command — there is no delete in this codebase.**

A file-organizing skill for [Claude Code](https://claude.com/claude-code) on
macOS, plus a set of standalone Python scripts you can run yourself. It does not
sort your files into `PDF/` and `Images/`. It works out which of *your* projects
each file belongs to and files it there.

---

## What you actually get

### 1. Your Downloads folder sorted by project, not by file type

Sorting by extension moves the mess around. A `PDF/` folder tells you nothing you
did not already know from the icon. What you actually want is: *the contract, the
slide deck, the three reference papers, and the spreadsheet for the same project,
in the same place.*

`tidy-undo` builds a vocabulary from the project directories you already have —
their names, and the names of the files inside them — then scores every loose file
against it. A file goes into a project folder only when one project wins by a
margin. Everything else stays exactly where it is and gets reported to you.

**Before** (real run, `~/Downloads`, 240 loose files at the top level):

```
~/Downloads/
├── 2508.09736v1.pdf
├── 2507.05246v1.pdf
├── 2505.00668v1.pdf
├── KakaoTalk_Photo_2026-07-14-09-21-33.jpeg
├── Screenshot 2026-08-03 at 4.51.12 PM.png
├── agent_economy_v6.pptx
├── agent_economy_final.pptx
├── agent_economy_final_final.pptx
├── policy-division/                 ← 279 files
├── policy-division 2/               ← byte-for-byte copy of the folder above
└── … 232 more loose files
```

**After** (same run, nothing deleted):

```
~/Downloads/                         ← 72 loose files left: the ones it would not guess at
├── _archive/
├── _cleaner_quarantine/
│   └── policy-division 2/           ← the cloned folder, moved intact. Still yours to inspect.
└── policy-division/

~/Archive/                           ← 404 files, filed under names you chose, not names it invented
├── aioutlook/문서/
├── aichip/{문서, 메모, 이미지}/
├── agent-economy/발표/
├── pet-hwahae/{표, 데이터}/
├── humanize-ko/이미지/
└── … 33 more project folders
```

(The folder names one level down are Korean — `문서` is "documents", `발표` is
"decks". That is a hardcoded map and a real limitation for non-Korean users; see
[Known limitations](#known-limitations).)

### 2. PDFs renamed from `2508.09736v1.pdf` to what the paper is called

An arXiv ID is a perfectly good filename for a machine and a useless one for you.
`tidy-undo` opens the file, finds the real title, and proposes a rename. Titles
come from four places, cheapest first: the arXiv API (the ID in the filename is
the only thing sent), the PDF's own `/Title` metadata, the largest text on page
one, and the first few lines. It renames **only files whose current name is
opaque** — arXiv IDs, UUIDs, long hex strings, camera and messenger dumps. A
filename a human chose is listed for you and left alone, because your own `v7`
and `final_3` conventions are load-bearing for your memory.

Real output from the same run — 82 files renamed, no failures:

```
2507.05246v1.pdf  →  When Chain of Thought is Necessary, Language Models Struggle to Evade Monitors.pdf
2504.06196v1.pdf  →  TxGemma： Efficient and Agentic LLMs for Therapeutics.pdf
2501.09223v1.pdf  →  Foundations of Large Language Models.pdf
2504.01848v1.pdf  →  PaperBench： Evaluating AI's Ability to Replicate AI Research.pdf
```

Of 79 title extractions: 75 from the arXiv API, 2 from PDF metadata, 1 from an
arXiv title search, 1 from the largest text on the cover page.

### And along the way

- **Find duplicate files** — content hashing, so identical files with different
  names are still caught. Duplicates go to a quarantine folder, never to the bin.
- **Find cloned folders** — `policy-division` and `policy-division 2`, 279 files
  each. Comparing those two by eye is an afternoon. It is a set comparison of
  hashes here.
- **Untangle version sprawl** — `v6`, `final`, `final_final`. It picks the
  newest by timestamp and hash rather than believing the word "final", because
  `v6` and `final` frequently differ.
- **Flag sensitive files** — ID scans, certificates, contracts, recovery codes
  sitting in plain sight in Downloads. These are *reported, not moved*. Where
  they belong is your call.

---

## Why you can hand it your files

Every AI file organizer says "preview before you move" and "one-click undo" now.
Those are claims in a prompt or a marketing page. Here is what is actually in
the repository, and how to check each one yourself in under a minute.

**There is no delete.** Not "we prefer not to delete" — the calls are not there.

```bash
grep -rn "os.remove\|shutil.rmtree\|send2trash\|\brm -" scripts/
# scripts/selftest.py:500:  shutil.rmtree(sb, ignore_errors=True)   # test sandbox only
```

One match, in the test harness, cleaning up the temporary directory it created
itself. Nothing in the scanning, planning or moving path can delete anything.

`os.unlink` appears seven times in `scripts/apply.py`, and every one is
accounted for: twice as the second half of a move (the source is released only
after the destination link is confirmed to exist), twice on the tool's own temp
copy during a cross-volume move, once on the probe file it writes to detect
case sensitivity, and twice on its own lock file. `os.rmdir` appears once, in
`undo`, and only on
directories that this run itself created and that are now empty. Directories
that existed before are never touched. Moving files to the Trash is a separate
command you have to ask for, after you have looked in the quarantine folder.

**Approval is a hash, not a "yes".** `apply.py verify` prints a token derived
from the plan file. `apply.py run` recomputes it and refuses to move anything if
they differ. Edit one line of the plan after approving it and the run is
rejected — not warned about, rejected. That means "the plan you read" and "the
plan that executed" cannot come apart, even if the model in the loop changes its
mind between the two steps.

**Moves cannot overwrite.** Same-volume moves are `os.link` then `os.unlink`.
`os.link` fails if the destination exists, so overwriting is structurally
impossible rather than merely avoided, and the original is released only after
the new location is confirmed. Cross-volume moves copy to a temp file, verify
the size, link it into place, and only then release the original. Name collisions
become ` (2)`. Hard-linked files are refused across volumes rather than silently
breaking the link.

**Undo is a replay, not a guess.** Every run writes a manifest. One command
replays it backwards:

```bash
python3 scripts/apply.py undo _work/_runs/<run_id>/manifest.json
```

Files whose contents changed since the run, or whose original location is now
occupied by something else, are left alone and reported with a reason.

**A kill -9 does not strand you.** Every move is journaled with an `fsync`
*before* it happens, so an interrupted run can be reversed from the journal even
though the manifest was never written. Point `undo` at the run directory instead
of the manifest:

```bash
python3 scripts/apply.py undo _work/_runs/<run_id>/
```

Verified: a 3,000-operation plan was killed with `SIGKILL` after 963 moves;
all 3,000 were restored, with zero files left behind. A lock file left by a
killed process is reclaimed by the next run after checking whether that PID is
still alive.

**23 adversarial self-tests, and you can run them.** NFD Korean filenames,
spaces, quotes, semicolons, backticks, newlines in filenames, case collisions on
both case-sensitive and case-insensitive filesystems (it builds a real disk image
with `hdiutil` for that), symlinks and symlink cycles, hard links, forged
approval tokens, a plan modified after approval, permission denial, a live lock,
an orphaned lock, a full disk, and the `SIGKILL` recovery above.

```bash
python3 scripts/selftest.py
#   23/23 통과          ← "23/23 passed". The scripts still report in Korean; see below.
```

Everything runs in a temporary sandbox and cleans up after itself.

---

## Real numbers

One person's `~/Downloads` and `~/Documents`, macOS, August 2026:

| | |
|---|---|
| Top-level loose files in `~/Downloads` | **240 → 72** |
| File operations across 16 runs | **1,051** |
| Data moved | **6.87 GB** |
| Failed | **0** |
| Skipped | **0** |
| PDFs renamed to their real titles | **82** |

Broken down by operation: 404 filed into project folders, 316 duplicates
quarantined, 167 relocated, 82 renamed, 51 old versions set aside, 31 sensitive
documents gathered for review.

Honest caveat: this is one machine, one user, one locale. See
[Known limitations](#known-limitations).

---

## Install

Requires macOS and Python 3.10+. Two third-party packages, both only for reading
PDFs. Everything else — scanning, hashing, duplicate detection, classification,
planning, moving, undo, the self-tests — is standard library only.

```bash
git clone https://github.com/<you>/tidy-undo.git
cd tidy-undo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 scripts/selftest.py      # 23/23 before you point it at anything real
```

Homebrew Python enforces [PEP 668](https://peps.python.org/pep-0668/), so
`pip install` outside a venv fails with `error: externally-managed-environment`.
Either use the venv above, or `pip install --user --break-system-packages -r
requirements.txt`. If you skip the PDF title extraction entirely, you need no
dependencies at all.

### As a Claude Code skill

```bash
mkdir -p ~/.claude/skills/cleaner
cp -R SKILL.md scripts references ~/.claude/skills/cleaner/
```

Then talk to it:

> organize my downloads folder

> which project does this file belong to?

> find the duplicate files

> undo that

Claude runs the scan, reports what it found, asks you to look at the plan, and
moves nothing until you say so. "Do whatever you think is best" is explicitly
not approval — the skill is written to require you to read the plan first.

### As command-line scripts

The scripts are deterministic and have no idea Claude exists. Read-only up until
the last line:

```bash
python3 scripts/scan.py ~/Downloads -o _work/inventory.jsonl --max-depth -1
python3 scripts/dedup.py _work/inventory.jsonl -o _work/dupes.json
python3 scripts/classify.py _work/inventory.jsonl -o _work/classified.jsonl

python3 scripts/plan.py --inventory _work/inventory.jsonl \
  --classified _work/classified.jsonl --dupes _work/dupes.json \
  --root ~/Downloads -o _work/plan.json --do quarantine,version

python3 scripts/apply.py verify _work/plan.json     # prints the approval token
# read _work/plan.json yourself, then:
python3 scripts/apply.py run _work/plan.json --token <token> --runs-dir _work/_runs
```

| Script | What it does | Network |
|---|---|---|
| `scan.py` | Read-only inventory. Treats `.app`/`.photoslibrary` bundles as one file, never follows symlinks, never downloads iCloud placeholders. ~1,500 files in 0.2 s | no |
| `dedup.py` | Exact duplicates, cloned folder pairs, version lineages | no |
| `classify.py` | Attributes files to projects. `--no-home-scan` for the conservative mode | no |
| `retitle.py` | PDF title extraction and rename proposals. `--no-network` to stay local | arXiv |
| `extract.py` | First N characters of pdf/hwp/hwpx/pptx/docx/xlsx/md | no |
| `sensitive.py` | Flags sensitive documents. Reports; never moves | no |
| `plan.py` | Builds the plan. `--do archive,quarantine,version,rename,relocate,sensitive` | no |
| `apply.py` | `verify` / `run` / `undo`. The only script that writes | no |
| `selftest.py` | 23 adversarial cases in a sandbox | no |

`plan.py` and `apply.py` cannot leave the `--root` you name. `~`, `/`, `/Users`
and `/Volumes` are rejected as scopes.

Bonus for Korean documents: `extract.py` reads **HWP and HWPX** (Hancom Office)
with a zero-dependency stdlib parser. Spotlight indexes those formats at exactly
0%, so `mdfind` cannot see inside them at all — which is why a folder full of
`.hwp` files is invisible to every other tool on this list.

---

## Who this is for

**It fits you if:**

- Your work divides into named projects, and you already have directories for
  them somewhere in your home folder. That is what the classifier reads. It is
  the whole mechanism.
- Your filenames are mostly descriptive. Roughly 95% of files can be attributed
  from the name alone; about 3% genuinely need the contents opened; about 1% can
  be handled from the extension. That ratio is why the design reads names first
  and opens files last.
- You download academic PDFs, spec sheets, or government documents that arrive
  named after an ID rather than a subject.
- You want to look at a plan before anything moves.

**It does not fit you if:**

- Your home folder has no project structure. Without directories to learn from,
  the classifier has no vocabulary, almost everything lands in `_미분류`
  (unsorted), and you have gained nothing over a manual sort. Have somewhere for
  the files to go *first*.
- You want ongoing automation — a folder watcher, rules that fire on download.
  That is [Hazel](https://www.noodlesoft.com/) or
  [organize](https://github.com/tfeldmann/organize), and they are better at it
  than this is. See below.
- You want a GUI. This is a skill and a set of scripts.
- You do not want to run an LLM over your filenames. The scan, hashing,
  deduplication and moving are pure Python, but the judgment step — "which
  project is this?" for the files the rules could not settle — is a model call.
- You are on Windows or Linux. The scanner depends on macOS-specific behaviour
  around bundles, iCloud placeholders and case sensitivity.

---

## How it compares

Nothing here is a knock on the alternatives. They solve adjacent problems and
several solve them better than this does.

| | tidy-undo | [Hazel](https://www.noodlesoft.com/) | [organize](https://github.com/tfeldmann/organize) | [llama-fs](https://github.com/iyaja/llama-fs) | Commercial AI organizers |
|---|---|---|---|---|---|
| Groups files by **your project names** | yes, learned from your home folder | no — rules you write | no — rules you write | by inferred topic | by inferred category |
| Runs continuously in the background | no, on request | yes | yes (scheduled) | no | varies |
| Renames opaque PDFs from their contents | yes, with arXiv lookup | no | no | yes | yes |
| Delete / trash exists in the tool | **no** | yes | yes | yes | usually |
| Undo | manifest replay, survives `kill -9` | Finder undo, best-effort | no | no | usually a session undo |
| Approval bound to the exact plan | yes, hash token | n/a | n/a | no | preview screen |
| Reads HWP/HWPX | yes | no | no | no | no |
| Adversarial self-tests in the repo | 23 | n/a | yes | none | n/a |
| Price | free, MIT | paid | free, MIT | free, MIT | mostly paid |

**Use a rules engine instead when your problem is repetitive and well-defined.**
"Every invoice PDF from this sender goes in that folder, forever" is a rule, not
a judgment, and Hazel or `organize` will do it faster, cheaper, and without a
model in the loop. This tool is for the pile that has already accumulated and
that no rule was ever written for.

---

## Known limitations

Stated plainly, because you are about to point this at your own files.

- **One Mac, one user, one locale.** All the numbers above come from a single
  machine. The defenses for external drives, exFAT volumes, managed corporate
  devices and non-Korean locales are written and unit-tested but have not met
  the real world. Run `selftest.py` on your machine first, and keep a backup for
  the first run. Bug reports from a second machine are the single most useful
  contribution right now.
- **The generated folder names are Korean.** The `KIND_DIR` map at the top of
  `scripts/plan.py` hardcodes `문서` (documents), `발표` (decks), `이미지`
  (images) and so on, and the default archive destination is `_정리됨`. Nothing
  crashes for other locales — you just get Korean folder names, which
  contradicts this tool's own stated goal of using *your* vocabulary. It is a
  single dictionary; edit it.
- **The scripts print status messages in Korean.** Progress lines, refusal
  reasons and the self-test report are Korean strings. The JSON the scripts
  emit — inventories, plans, manifests — uses English keys throughout, so
  anything you parse is unaffected; the human-readable `reason` values inside a
  plan are Korean. Translation of the console output is in progress.
- **Sensitive-file detection is tuned for Korean paperwork.** `sensitive.py`
  recognises Korean ID documents, certificates, financial and legal forms. Its
  English patterns cover only obvious cases (passwords, API keys, NDAs,
  passports). It will miss US SSNs, EU identity documents, and most non-Korean
  official forms. **Do not read "nothing was flagged" as "nothing sensitive is
  there."**
- **Classifier thresholds are tuned for Korean compound words.** `--min-score`
  and `--margin` were calibrated on a Korean-language dataset. English filenames
  are handled (the tokenizer runs word tokens alongside Korean 3-grams) but you
  may need to retune for a different language.
- **`classify.py` reads your home directory by default.** To build its
  vocabulary it walks every non-hidden top-level directory in `~` and reads up to
  600 file and folder names inside each. This stays in memory and is never
  written to disk or sent anywhere, but the resulting `classified.jsonl` keeps
  the matched tokens as evidence — short fragments that may originate in
  filenames from other projects. Be aware of that before sharing that file or
  screenshotting it. Pass `--no-home-scan` to disable the walk entirely and use
  only directories you name with `--project-dir`; fewer files get classified
  automatically as a result.
- **`retitle.py` talks to arXiv.** When a filename matches the arXiv ID pattern
  it sends that ID — a public identifier, not your file — to
  `export.arxiv.org`. In the rarer fallback path it sends up to ten words of a
  title extracted from the PDF as a search query. That is the only route by
  which anything derived from file *contents* leaves your machine, and it only
  applies to files already named like public preprints. `--no-network` disables
  both. No other script makes any network call.
- **Filenames go into the model's context.** When Claude Code runs the judgment
  step, the filenames it could not settle by rule are sent to the model. File
  *contents* are not, unless you explicitly ask for the extraction step. If that
  is unacceptable for your data, use the scripts directly and do the judgment
  yourself — every script except the classification step works without a model.
- **`~/Pictures` is refused, deliberately.** Nearly everything in there lives
  inside the `Photos Library.photoslibrary` package, and touching it through the
  filesystem corrupts the library. The skill rejects that scope. Calling the
  scripts directly bypasses that guard — don't.
- **Do not resume an interrupted run.** After restoring from a journal, start
  over from the scan. The snapshot the plan was built against is stale.

---

## Contributing

Bug reports from a **second machine** are worth more than features right now,
especially: non-Korean locales, external and network volumes, case-sensitive
filesystems, corporate-managed Macs, and anything involving iCloud Drive
placeholders.

If you change anything under `scripts/`, `python3 scripts/selftest.py` must be
23/23 before the change touches a real file. A pull request that changes the
mover without adding a case to `selftest.py` will be asked for one.

Things that are out of scope on purpose, and why:

- **Deleting files.** Quarantine plus your own Finder is enough. The absence of
  delete calls is the feature.
- **Automatic renaming of human-chosen filenames.** Your `v7` and `final_3`
  conventions carry information the tool cannot see. Only opaque names are
  renamed.
- **Archiving by modification time.** Downloads is a working surface. Old does
  not mean unwanted.
- **Sorting everything into extension folders.** It lengthens paths and adds no
  information.
- **Rearranging the inside of git working trees.** That destroys uncommitted
  work. Project directories are treated as indivisible units.

---

## License

[MIT](./LICENSE).

Korean documentation: [README.ko.md](./README.ko.md).
