# Measured performance and extraction paths

Measured on 2026-08-22, macOS 26.5 (Darwin 25.5.0) with python3 3.14.4. Other
environments will differ; if something looks off, measure it again.

Korean original: `capabilities.ko.md`.

## Pipeline timings

| Stage | Input | Measured |
| --- | --- | --- |
| Scan + head hash | 1,510 files | 0.2 s |
| Three-stage duplicate detection | 1,510 files | 3.1 s |
| Project attribution (rules) | 1,374 files across 89 projects | 0.1 s |
| Extract text heads, everything | 917 files | 28.5 s |
| Duplicate detection (entire home folder) | 263,041 files | 74 s |

The whole pipeline finishes in minutes. Cost is not the constraint here.

## Best extraction path per format

| Format | Spotlight index rate | Best path | Speed |
| --- | --- | --- | --- |
| PDF (text) | 58% | `pypdf`, first 3 pages | 63 ms |
| PDF (scanned) | 2% | `sips` → PNG → Vision OCR | 720 ms |
| **HWP** | **0%** | **Own CFB parser** (`hwp_stdlib.py`, zero dependencies) | **4 ms** |
| **HWPX** | **0%** | `unzip -p Contents/section*.xml` | 2 ms |
| PPTX | 67% | `unzip -p ppt/slides/slide*.xml` | 3 ms |
| DOCX | 100% | `unzip -p word/document.xml` | 2 ms |
| XLSX | 67% | `unzip -p xl/sharedStrings.xml` | 11 ms |
| MD / TXT / code | 100% | Read directly | 0 ms |
| Images | Indexed via Live Text | `mdfind` or Vision OCR | — |

The **0% Spotlight index rate for HWP and HWPX** is the important line. HWP and HWPX
are the formats of Hancom Office, the word processor that dominates Korean
government, academia, and public institutions, and `mdfind` is completely blind to
those files. That is the one place where an in-house extractor cannot be replaced by
the system. The HWP parser ran across all 144 such files present with zero failures;
HWPX succeeded on all 86.

## What Spotlight can do for you

It is cheap and accurate, so do not reimplement it.

```bash
mdfind -onlyin <dir> "kMDItemTextContent == '*예산*'c"        # content search ("budget"), 0.4 s
mdfind -onlyin <dir> 'kMDItemLastUsedDate < $time.now(-15552000)'  # unused for 180 days
mdfind -onlyin <dir> 'kMDItemFSSize > 50000000'               # larger than 50 MB
mdfind "kMDItemIsScreenCapture == 1"                          # screenshots only

# Always query metadata for many files in one call — roughly 1,000x faster than a per-file loop
mdls -name kMDItemWhereFroms -name kMDItemLastUsedDate -name kMDItemUseCount <files...>
```

`kMDItemWhereFroms` holds the URL a download came from (present on about 64% of
files). Which site a file came from is a strong signal for topic attribution.

A caution: `kMDItemLastUsedDate` is missing often (about 48% of the time). Never
declare a file unused on that value alone — read it alongside `kMDItemDateAdded`,
mtime, and `kMDItemUseCount`.

## What is available, and what is not

Preinstalled: `pypdf`, `pdfplumber`, `pdfminer`, `python-docx`, `python-pptx`, `PIL`;
on the command line, `mdfind`, `mdls`, `xattr`, `trash`, `jq`, `unzip`, `xmllint`,
`sips`, `swiftc`.

Absent: `fdupes`, `exiftool`, `tag`, `fswatch`, `openpyxl`, `olefile`. The homebrew
python enforces PEP 668, so `pip install` does not work directly and would need a
venv. Nothing currently needed is missing, though — it is either already present or
replaced by an in-house implementation.

## What this cannot do

- **Continuous or real-time folder watching.** The skill only runs when it is
  invoked. Batch invocation is the correct design; if something truly must be
  resident, that is the territory of Hazel or a LaunchAgent.
- **Cleaning up inside a photo library.** `Photos Library.photoslibrary` is a
  database owned by the Photos app. Touching it from the filesystem corrupts it.
- **Reading the value of `kMDItemTextContent`.** It always returns `(null)`.

## Where rule-based tools end and this begins

Do not rebuild what Hazel, organize, or Folder Actions already do well — matching on
extension, size, and date; watching a folder continuously; running fixed rules
repeatedly.

There are exactly four things only an LLM can do here.
1. **Propose the classification scheme itself** when no rules exist yet.
2. **Read the user's project names and vocabulary** and name folders in their language.
3. Attach **a hypothesis and its evidence** to the residue that no rule can express,
   and hand it to the user.
4. **Harden discovered patterns into rules** and export them, reducing how often the
   LLM has to be called at all.
