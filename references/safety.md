# Safety design in detail

Consult this when implementing the principles in SKILL.md. Read it before you modify
the runner (`scripts/apply.py`) or add a new kind of operation.

Korean original: `safety.ko.md`.

## Contents
1. Accident scenarios and their defenses
2. Protected paths
3. Risk-level gates
4. What the manifest must contain
5. macOS-specific traps

---

## 1. Accident scenarios and their defenses

| Accident | How it shows up | Defense | Where implemented |
| --- | --- | --- | --- |
| Run against the wrong target | "Clean up Downloads" ends up scoping the whole home folder | Exactly one root; `~`, `/`, `/Users`, `/Volumes` refused | `apply.preflight` |
| Git working tree destroyed | `*.md` inside a project gets classified as a document and moved | A directory holding a project marker is indivisible; never descend into it | `scan.has_project_marker` |
| Dependency explosion | Tens of thousands of `node_modules` files flood the scan | Name-based pruning | `scan.ALWAYS_SKIP` |
| App bundle taken apart | Descending into `.app` or `.photoslibrary` scatters its resources | Extension plus the `Contents/Info.plist` structure test | `scan.is_bundle` |
| Photo library corrupted | 90,000 items under `~/Pictures` treated as cleanup targets | Package detection collapses it to one item, and the scope excludes it | `scan.is_bundle` |
| Symlink escape | A link points at something outside the scope | `follow_symlinks=False`; the link itself is the single item | `scan.walk` |
| Infinite recursion on cyclic links | A directory link points back at an ancestor | Keep a visited set of `(dev, ino)` | `scan.walk` |
| Hard links broken | A cross-volume copy severs the link relationship | Refuse cross-volume moves when `nlink > 1` | `apply.move_noclobber` |
| Bulk iCloud download | Opening a placeholder pulls down tens of GB | Test `st_flags & SF_DATALESS` and do not open | `scan`, `apply.preflight` |
| Overwrite on name collision | `os.rename` / `shutil.move` overwrite silently | `os.link` (fails with EEXIST) followed by `unlink` | `apply.move_noclobber` |
| Case-insensitive filesystem collision | `A.pdf` overwrites `a.pdf` | Runtime probe plus a comparison against the sibling listing | `apply.exists_ci` |
| Hangul NFD mismatch | A normalized string fails to open the actual file | `path` stays exactly as read; only comparisons use `path_nfc` | throughout `scan` |
| Shell injection | A filename containing quotes or semicolons is interpreted as a command | No shell strings are ever assembled; `os` / `shutil` are called directly | all scripts |
| Half-finished move after an interruption | No way to know how far it got | Write to the journal before each move, then `fsync` | `apply.jwrite` |
| Concurrent runs collide | Two cleanups touch the same folder | A lock file at the root | `apply.run` |
| Files change after the scan | The plan's premises no longer hold (TOCTOU) | Compare against the size/mtime snapshot; skip on mismatch | `apply.preflight` |
| A file moved while in use | A document being saved, or a download in flight, gets corrupted | Exclude anything modified within 90 seconds, plus `.crdownload`-style temporaries | `scan`, `apply.preflight` |
| Disk full | Space runs out mid cross-volume move | Require required bytes × 1.2 + 5 GB before proceeding | `apply.preflight` |
| Plan tampered with after approval | Something other than what was approved gets executed | Token = hash of the plan; refuse on mismatch | `apply.plan_token` |
| Cognitive loss | "You cleaned it up and now I can't find anything" | Minimal changes, `_CLEANER_MAP.md`, folder names drawn from the user's own vocabulary | Step 8 |

## 2. Protected paths

**Hard blocks** — cannot be given as a scope, cannot be entered:
```
/  /System/**  /Library/**  /private/**  /usr/**  /bin/**  /sbin/**  /etc/**
/var/**  /Applications/**  /Volumes  /Users  /Users/Shared/**
~  ~/Library/**  ~/.Trash/**  ~/.ssh/**  ~/.gnupg/**  ~/.aws/**  ~/.config/**
~/.claude/**  ~/CLAUDE.md
~/Library/Mobile Documents/**   <- iCloud. A move here propagates to every device
~/Library/CloudStorage/**       <- Dropbox / OneDrive / Google Drive mounts
```

If the terminal has Full Disk Access, `~/Library/Mail`, `Messages`, `Safari`, and
`AddressBook` **are readable**. Exclude them not only from cleanup but from indexing
and summarization as well.

**Indivisible containers** — inside the scope, but never entered:
directories holding a project marker, dependency and build output trees, every macOS
package, and photo/music/video libraries.

**Individually excluded** — inside the scope, but never touched:
`.DS_Store`, `.localized`, `Icon\r`, `._*`, `*.crdownload`, `*.part`, `~$*`,
`.*.swp`, iCloud placeholders, anything modified within the last 90 seconds, and
symlinks pointing outside the scope.

## 3. Risk-level gates

| Level | Covers | Reversal | Approval required |
| --- | --- | --- | --- |
| L0 read | Scan, duplicate detection, classification proposals, reports | Not needed | Confirm the scope only |
| L1 move | Create new folders and move files into them | Replay the manifest backwards | Present the plan + explicit approval |
| L2 quarantine | Move duplicate copies into the quarantine folder | Fully reversible | L1 approval + a second look at the quarantine list |
| L2.5 trash | Send quarantined items to `trash` | Manual restore from Finder | Separate session, separate approval |
| L3 modify originals | Rename, unarchive, cross-volume move | Varies by kind | Item-by-item approval |
| L4 permanent deletion | **Does not exist** | — | No approval can enable it |

"Just take care of it" never gets past L1. One run performs one level only — mixing
L1 and L2 blurs what the user actually approved.

## 4. What the manifest must contain

A person must be able to reverse the run by hand, even without this code.

| Requirement | Field |
| --- | --- |
| Original path | `operations[].src` (exactly as read — a normalized form will not find an NFD file) |
| Where it actually landed | `dst_actual` — always distinct from the intent, `dst_intended` |
| Collision history | `collisions` — how many times it had to step aside |
| Integrity | `snapshot.size`, `snapshot.mtime` |
| Undo order | `seq`, replayed in reverse |
| Why something was left alone | `skipped[].skip_reason` — silent skips are forbidden |
| Newly created folders | `created_dirs` — folders that already existed must not be removed |

## 5. macOS-specific traps

- **APFS compares filenames normalization-insensitively**, so an NFC string will open
  an NFD file. External exFAT and SMB volumes do not. Never throw away the original
  string.
- **APFS clones** make the sum of logical sizes larger than the space actually
  occupied. When you report savings, say that the figure is logical.
- **`kMDItemTextContent` cannot be read.** Querying it with `mdls` always returns
  `(null)`. Spotlight only answers "does this word appear?". If you need the body
  text, extract it yourself.
- **`/private/tmp` is not indexed by Spotlight.** Tagging and search work only under
  the home directory.
- **`/usr/bin/trash` is Apple's own** and moves files to `~/.Trash/`. The files
  survive.
