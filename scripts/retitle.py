#!/usr/bin/env python3
"""PDF title extraction — pulls the real title out of a document to propose a
new filename.

Renaming is easy to undo but it breaks the user's memory of where things are.
So this tool first judges "how much does the current name explain the
contents", and only opaque names become rename candidates. Names a human chose
are merely listed, never rewritten.

Titles are looked for along three paths:
  1. The PDF metadata /Title — cheapest when present. It is full of garbage put
     there by authoring tools (file paths, 'Microsoft Word - xxx.doc'), so it
     has to be filtered.
  2. The largest type on the first page — a title on a paper or report cover is
     almost always larger than the body. Extracted with pdfplumber, which is
     what exposes font sizes.
  3. The first few lines of page one — the last resort when both of the above
     fail.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata

# A current name shaped like this explains nothing about the contents, so it
# becomes a rename candidate. The Korean alternatives (무제 = "untitled",
# 제목없음 = "no title") are filename-matching data, not UI text.
OPAQUE_RE = re.compile(
    r"^(?:"
    r"\d{4}\.\d{4,5}(v\d+)?"                 # arXiv ID  2508.09736v1
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]+"   # UUID
    r"|[0-9a-f]{16,}"                        # long hex string
    r"|\d{9,}(_\d+)*"                        # numeric filenames from socials
    r"|(download|untitled|unnamed|document|doc|scan|file|무제|제목없음)"
    r"(\s*\(?\d+\)?)?"
    r"|ssrn[-_]?id\d+"
    r"|[0-9a-f]{6,}[-_](en|ko|fr|de|es)"     # OECD/institutional editions  02f73362-en
    r"|\d{1,3}"                              # "1.pdf"
    r")$", re.I)

# Garbage commonly found in PDF metadata /Title. Again, filename-matching data:
# 프레젠테이션 = "presentation", 무제 = "untitled", 한글 = the Hangul word
# processor whose files this tool also sees.
BAD_TITLE_RE = re.compile(
    r"^(microsoft word|microsoft powerpoint|powerpoint 프레젠테이션|"
    r"untitled|무제|slide \d|hwp|한글|.*\.(doc|docx|hwp|hwpx|pptx?|indd|tex)$|"
    r"[a-z]:\\|/users/|/var/|print job|adobe)", re.I)

NOISE_LINE_RE = re.compile(
    r"^(?:\s*|\d+|page \d+|www\.|https?://|[-=_*·•]+|"
    r"arxiv:\S+|doi:\S+|issn.*|isbn.*)$", re.I)


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def clean(t: str) -> str:
    t = nfc(re.sub(r"\s+", " ", t)).strip(" .-–—_:;,\n\t")
    return t


def is_opaque(stem: str) -> bool:
    return bool(OPAQUE_RE.match(nfc(stem).strip()))


def usable(t: str | None) -> bool:
    if not t:
        return False
    t = clean(t)
    return (4 <= len(t) <= 200 and not BAD_TITLE_RE.match(t)
            and not t.startswith("/") and len(t.split()) >= 1)


def from_metadata(path: str) -> str | None:
    try:
        from pypdf import PdfReader
        md = PdfReader(path).metadata or {}
        return clean(md.get("/Title") or "")
    except Exception:                                     # noqa: BLE001
        return None


def from_largest_text(path: str) -> str | None:
    """The line set in the largest type on page one. A cover title is almost
    always the largest thing on the page."""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                return None
            words = pdf.pages[0].extract_words(extra_attrs=["size"])
    except Exception:                                     # noqa: BLE001
        return None
    if not words:
        return None
    # Group words into lines (similar `top` coordinate) and record each line's
    # largest font size
    lines: dict[int, list] = {}
    for w in words:
        key = round(w["top"] / 4)
        lines.setdefault(key, []).append(w)
    scored = []
    for key in sorted(lines):
        ws = sorted(lines[key], key=lambda x: x["x0"])
        # Deliberately NOT clean() here. clean() strips a trailing hyphen, and
        # then the hyphen re-joining below could never fire ("A Pre-" -> "A Pre").
        text = re.sub(r"\s+", " ", nfc(" ".join(w["text"] for w in ws))).strip()
        size = max(w.get("size", 0) for w in ws)
        if text and not NOISE_LINE_RE.match(text):
            scored.append((size, key, text))
    if not scored:
        return None
    top_size = max(s for s, _, _ in scored)
    # Titles are commonly split over two lines, so join consecutive lines whose
    # size is close to the maximum
    parts = [(k, t) for s, k, t in scored if s >= top_size * 0.88]
    parts.sort()
    merged, prev = [], None
    for k, t in parts:
        # The line-group key is top/4, so normal leading puts lines 4-5 apart.
        # A tolerance of 3 cuts a two-line title off after the first line
        # (observed in the field).
        if prev is not None and k - prev > 6:
            break
        # Typesetting hyphenates words at the line break ("A Pre-" / "training").
        # Joining those with a space breaks the word and the title looks
        # truncated.
        if merged and merged[-1].endswith("-"):
            merged[-1] = merged[-1][:-1] + t
        else:
            merged.append(t)
        prev = k
    return clean(" ".join(merged)) or None


def from_first_lines(path: str, limit: int = 4) -> str | None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from extract import head
    t = head(path, 600) or ""
    for line in t.split("\n"):
        line = clean(line)
        if len(line) >= 8 and not NOISE_LINE_RE.match(line):
            return line
    return None


ARXIV_ID_RE = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")


def arxiv_titles(ids: list[str], batch: int = 50) -> dict[str, str]:
    """Fetch canonical titles from the arXiv API. Overwhelmingly more accurate
    than extracting them from the PDF.

    Guessing the title from font size on the cover collapses easily on
    two-column layouts, math fonts and broken letter spacing (measured: of 79
    papers, roughly 20 came out truncated or grabbed body text). If the
    filename is an arXiv ID, it is better to ask the source directly.

    id_list fetches many at once. arXiv asks callers not to fire requests
    back-to-back, so there is a pause between batches.
    """
    import time
    import urllib.parse
    import urllib.request

    out: dict[str, str] = {}
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        url = ("https://export.arxiv.org/api/query?max_results=%d&id_list=%s"
               % (len(chunk), urllib.parse.quote(",".join(chunk))))
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                xml = resp.read().decode("utf-8", "ignore")
        except Exception:                                  # noqa: BLE001
            continue                                       # on failure, fall back to extraction
        for m in re.finditer(
                r"<entry>.*?<id>[^<]*?abs/([\d.]+)v?\d*</id>.*?<title>(.*?)</title>",
                xml, re.S):
            out[m.group(1)] = clean(" ".join(m.group(2).split()))
        if i + batch < len(ids):
            time.sleep(3)
    return out


def _words(t: str) -> set[str]:
    return {w for w in re.split(r"[^0-9A-Za-z가-힣]+", nfc(t).lower())
            if len(w) >= 4}


def title_matches_body(title: str, path: str) -> bool:
    """Check the title really belongs to THIS PDF by comparing it against the
    body text of page one.

    A title fetched from elsewhere can come from an accurate source and still
    not be this file's title. The title is printed on the cover, so if its
    distinctive words actually appear on the first page, the pairing holds.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from extract import head
    body = head(path, 1500) or ""
    if not body:
        return True                      # unreadable body -> withhold judgment, pass
    tw = _words(title)
    if not tw:
        return False
    bw = _words(body)
    hit = len(tw & bw) / len(tw)
    return hit >= 0.5


def arxiv_search(fragment: str, path: str) -> str | None:
    """Search arXiv by a fragment of the title to recover the canonical one.

    Used when the extracted title came out truncated. Whatever is found is
    still checked against the body — search can rank a different but similar
    paper first, and taking that verbatim produces a worse result than the
    truncated title (an entirely wrong paper's title).
    """
    import urllib.parse
    import urllib.request

    q = re.sub(r"[^0-9A-Za-z ]+", " ", fragment)
    q = " ".join(q.split()[:10])
    if len(q) < 8:
        return None
    url = ("https://export.arxiv.org/api/query?max_results=5&search_query="
           + urllib.parse.quote(f'ti:"{q}"'))
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            xml = resp.read().decode("utf-8", "ignore")
    except Exception:                                      # noqa: BLE001
        return None
    for m in re.finditer(r"<entry>.*?<title>(.*?)</title>", xml, re.S):
        cand = clean(" ".join(m.group(1).split()))
        if usable(cand) and title_matches_body(cand, path):
            return cand
    return None


def title_of(path: str) -> tuple[str | None, str]:
    for fn, how in ((from_metadata, "metadata"),
                    (from_largest_text, "largest_text"),
                    (from_first_lines, "first_lines")):
        t = fn(path)
        if usable(t):
            return clean(t), how
    return None, "none"


def safe_name(title: str, ext: str, maxlen: int = 90) -> str:
    """Make a title usable as a filename. A slash must be substituted — it
    would split the name into folders."""
    t = nfc(title).replace("/", "／").replace(":", "：").replace("\\", "＼")
    t = re.sub(r'[<>"|?*\x00-\x1f]', "", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if len(t) > maxlen:
        cut = t[:maxlen]
        t = cut[:cut.rfind(" ")] if " " in cut[maxlen // 2:] else cut
    return f"{t}.{ext}" if ext else t


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PDF title extraction (read-only)")
    ap.add_argument("inventory")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--only-opaque", action="store_true",
                    help="only files whose name is opaque (recommended)")
    ap.add_argument("--ext", default="pdf")
    ap.add_argument("--no-network", action="store_true",
                    help="extract from inside the PDF only, no arXiv lookup")
    args = ap.parse_args()

    exts = {e.strip().lower() for e in args.ext.split(",")}
    rows, targets = [], []
    stats = {"considered": 0, "titled": 0, "no_title": 0, "name_unchanged": 0}
    with open(args.inventory, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("kind") != "file" or r.get("ext") not in exts:
                continue
            if r.get("dataless"):
                continue
            opaque = is_opaque(r["stem"])
            if args.only_opaque and not opaque:
                continue
            targets.append((r, opaque))

    # Look up everything that is an arXiv ID in one go first
    arx = {}
    if not args.no_network:
        ids = []
        for r, _ in targets:
            m = ARXIV_ID_RE.match(nfc(r["stem"]).strip())
            if m:
                ids.append(m.group(1))
        if ids:
            print(f"arXiv lookup: {len(ids)} id(s)...", file=sys.stderr)
            arx = arxiv_titles(sorted(set(ids)))
            print(f"  {len(arx)} answered", file=sys.stderr)

    for r, opaque in targets:
            stats["considered"] += 1
            m = ARXIV_ID_RE.match(nfc(r["stem"]).strip())
            title = how = None
            if m and m.group(1) in arx:
                cand = arx[m.group(1)]
                # It really does happen that the arXiv ID in the filename does
                # not match the contents — a different paper downloaded and
                # saved under that ID. Trusting it unchecked would attach a
                # completely wrong title, so compare against the body.
                if title_matches_body(cand, r["path"]):
                    title, how = cand, "arxiv"
                else:
                    stats["id_mismatch"] = stats.get("id_mismatch", 0) + 1
            if title is None:
                title, how = title_of(r["path"])
                # An extracted title truncates easily on two-column layouts and
                # broken letter spacing. When we know it is an arXiv paper, that
                # fragment can search back for the canonical title.
                if title and m and not args.no_network:
                    found = arxiv_search(title, r["path"])
                    if found:
                        title, how = found, "arxiv_search"
            if not title:
                stats["no_title"] += 1
                continue
            new = safe_name(title, r["ext"])
            if nfc(new) == nfc(r["name"]):
                stats["name_unchanged"] += 1
                continue
            stats["titled"] += 1
            rows.append({"path": r["path"], "current": nfc(r["name"]),
                         "title": title, "proposed": new, "method": how,
                         "opaque": opaque, "size": r["size"]})

    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
