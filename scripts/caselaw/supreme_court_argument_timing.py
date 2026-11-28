#!/usr/bin/env python3
"""Time the Supreme Court argument transcripts against the recordings, and make transcripts where none exists.

The argument player plays a case's recording with its transcript beside it and
lights each turn as it is spoken, so every turn needs the moment it begins.
The Court's transcripts (scripts/caselaw/supreme_court_argument_transcripts.py)
carry pages and lines but no times, and there is no official transcript before
the October 1968 term or for four later arguments the Court does not list.

One speech recognition pass over the recordings serves both: the words it hears,
each with its time, are matched to the official transcript to time each turn;
where there is no official transcript, the words it hears are the transcript,
marked as made by speech recognition with the model named. The model is Whisper
small.en (OpenAI's English speech recognition model, run with faster-whisper
on the processor).

Subcommands, in this order:

  request     Write the list of recordings to hear: research/speech-requests/supreme-court-arguments.json.
              Pushing it to a branch other than main starts .github/workflows/speech-recognition.yml,
              which hears the recordings in shards and returns the words as workflow artifacts.
  transcribe  Hear one shard of the request (run by the workflow): --request FILE --shard N --out DIR.
  align       Read the words (--words DIR) and the transcripts, and write one timed transcript
              per argument (JSON) into --out DIR.
  store       Put the timed transcripts in the R2 bucket under supreme-court-transcripts/timed/,
              add them to the capture index and write them on the case records.

The recording the player uses for an argument is the Court's own MP3 where there
is one (every argument since the October 2010 term), and otherwise the National
Archives' files for that day: one file per case in the Black Series (1955 to
1972), often in parts, and the whole day's reels in the later series.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import difflib
import gzip
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "content" / "sources"
AUDIO_PLAN = ROOT / "research" / "supreme-court-coverage" / "argument-audio.csv"
TRANSCRIPT_PLAN = ROOT / "research" / "supreme-court-coverage" / "argument-transcripts.csv"
REQUEST = ROOT / "research" / "speech-requests" / "supreme-court-arguments.json"
MODEL = "small.en"
MODEL_NAME = "Whisper small.en"
SHARDS = 40
USER_AGENT = "Tribal Law Project research script (Supreme Court argument timing)"
TIMED_TOPIC = "supreme-court-transcripts"


def audio_key(row: dict) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import supreme_court_argument_audio as audio  # noqa: PLC0415
    return audio.storage_key(row)


# ---------------------------------------------------------------- which recordings each argument uses


def arguments() -> list[dict]:
    """Every argument of our cases with a recording: case, day, the files the player plays (in order) and the official transcripts."""
    rows = [r for r in csv.DictReader(AUDIO_PLAN.open(encoding="utf-8")) if r["kind"] == "argument" and r["status"] == "stored"]
    transcripts = collections.defaultdict(list)
    if TRANSCRIPT_PLAN.exists():
        for r in csv.DictReader(TRANSCRIPT_PLAN.open(encoding="utf-8")):
            if r["status"] == "stored":
                transcripts[r["case"]].append(r)
    # Two case records can share one argument (Arizona v. California, 2000): either one's transcript serves both.
    sharing = collections.defaultdict(set)
    for r in rows:
        sharing[(r["link"], argued_day(r), r["segment"])].add(r["case"])
    by_day = collections.defaultdict(list)
    for r in rows:
        by_day[(r["case"], argued_day(r))].append(r)
    out = []
    for (case, day), same in sorted(by_day.items()):
        court = [r for r in same if r["source"] == "court"]
        files = sorted(own_files(court or same), key=lambda r: part_order(r["link"]))
        day_ = dt.date.fromisoformat(day)
        cases = set().union(*(sharing[(r["link"], day, r["segment"])] for r in files))
        official = [t for c in sorted(cases) for t in transcripts[c] if abs((dt.date.fromisoformat(t["date"]) - day_).days) <= 4]
        official = list({t["link"]: t for t in official}.values())
        # A case argued over two days has a transcript for each: each day takes its own, or else the nearest.
        if official:
            gap = min(abs((dt.date.fromisoformat(t["date"]) - day_).days) for t in official)
            official = [t for t in official if abs((dt.date.fromisoformat(t["date"]) - day_).days) == gap]
        # The docket numbers the Chief Justice may call the case by: the transcripts' and the catalog's ("[Case 72-5323]").
        dockets = [t["docket"] for t in official] + [d for r in same for d in re.findall(r"\[Case ([^\]]+)\]", r["segment"])]
        out.append({"case": case, "date": day,
                    "dockets": [d for d in dict.fromkeys(re.split(r"[\s,&/]+", " ".join(dockets))) if re.fullmatch(r"\d\d-\d{2,5}", d)],
                    "files": [{"key": audio_key(r), "link": r["link"], "duration": int(r["duration_seconds"] or 0),
                               "source": r["source"], "whole_session": r["whole_session"] == "yes"} for r in files],
                    "official": [{"link": t["link"], "docket": t["docket"], "date": t["date"],
                                  "key": f"supreme-court-transcripts/{t['term']}/{t['link'].rsplit('/', 1)[1]}"} for t in official]})
    return out


def argued_day(row: dict) -> str:
    """The day argued: the National Archives misdates a few recordings, and the audio plan notes the day the case was argued."""
    found = re.search(r"the case was argued on (\d{4}-\d{2}-\d{2})", row.get("date_note") or "")
    return found.group(1) if found else row["date"]


def own_files(rows: list[dict]) -> list[dict]:
    """A Black Series day lists every case heard on it; keep the files of the case's own docket numbers.

    The files are named 267-<reel>-<docket numbers>-<dates>, and the segment names the case's numbers
    ("Metakatla Indian Community v. Egan [Case 2 & 3]"), so Kake's day keeps 267-354-2-3-... and
    leaves Seymour's (No. 62) and Blau's (No. 66)."""
    def numbers_of_file(link: str) -> tuple:
        found = re.match(r"267-\d+[a-z]?-((?:\d+-)+?)\d{8}", link.rsplit("/", 1)[-1])
        return tuple(found.group(1).strip("-").split("-")) if found else ()
    wanted = set()
    for r in rows:
        found = re.search(r"\[Case ([\d &,]+)\]", r.get("segment") or "")
        if found:
            wanted |= set(re.findall(r"\d+", found.group(1)))
    if not wanted:
        return rows
    kept = [r for r in rows if numbers_of_file(r["link"]) and set(numbers_of_file(r["link"])) == wanted]
    return kept or rows


def part_order(link: str) -> tuple:
    """Parts and reels in playing order: 1of2 before 2of2, r1 before r2."""
    name = link.rsplit("/", 1)[-1]
    found = re.search(r"(\d+)of\d+|[_-]r(\d+)\b|reel[_-]?(\d+)", name, re.I)
    number = int(next(g for g in found.groups() if g)) if found else 0
    return (re.sub(r"(\d+of\d+|[_-]r\d+|reel[_-]?\d+)", "", name, flags=re.I), number, name)


def request(args: argparse.Namespace) -> None:
    files = {}
    for argument in arguments():
        for f in argument["files"]:
            files.setdefault(f["key"], {"key": f["key"], "link": f["link"], "duration": f["duration"]})
    if args.keys:
        # Hear these recordings again; with --no-vad, every moment of them: the voice activity filter drops whole
        # minutes of the telephone arguments of 2020-21 (Cooley: 3,849 words heard of the transcript's 10,267).
        shards = [[{**files[k], **({"vad": False} if args.no_vad else {})}] for k in args.keys]
        target = REQUEST.with_name(f"{REQUEST.stem}-{args.name}.json")
        target.write_text(json.dumps({"model": MODEL, "shards": shards}, indent=1) + "\n", encoding="utf-8")
        print(f"{len(shards)} recordings in {len(shards)} shards: {target}")
        return
    if args.again or args.unheard:
        # Hear again what a lost runner took with it: the recordings in these shards of the first request
        # whose words are not in the folder given, in shards of about two hours each. With --unheard: every
        # recording the arguments play whose words are not there (a reel put right after the first request).
        first = json.loads(REQUEST.read_text(encoding="utf-8"))
        held = {path.name.removesuffix(".json.gz") for path in Path(args.words).glob("*.json.gz")}
        pool = list(files.values()) if args.unheard else [f for i in args.again for f in first["shards"][i]]
        wanted = [f for f in pool if f["key"].replace("/", "__") not in held]
        count = max(1, round(sum(f["duration"] for f in wanted) / 7200))
        shards, totals = [[] for _ in range(count)], [0] * count
        for f in sorted(wanted, key=lambda f: -f["duration"]):
            i = totals.index(min(totals))
            shards[i].append(f)
            totals[i] += f["duration"]
        target = REQUEST.with_name(f"{REQUEST.stem}-{args.name}.json")
        target.write_text(json.dumps({"model": first["model"], "shards": shards}, indent=1) + "\n", encoding="utf-8")
        print(f"{len(wanted)} recordings, {sum(totals) / 3600:.1f} hours, in {count} shards: {target}")
        return
    # Shards of about equal length: longest first, each to the shortest shard so far.
    shards = [[] for _ in range(SHARDS)]
    totals = [0] * SHARDS
    for f in sorted(files.values(), key=lambda f: -f["duration"]):
        i = totals.index(min(totals))
        shards[i].append(f)
        totals[i] += f["duration"]
    REQUEST.parent.mkdir(parents=True, exist_ok=True)
    REQUEST.write_text(json.dumps({"model": MODEL, "shards": shards}, indent=1) + "\n", encoding="utf-8")
    print(f"{len(files)} recordings, {sum(totals) / 3600:.1f} hours, in {SHARDS} shards of about {max(totals) / 3600:.1f} hours")


# ---------------------------------------------------------------- hearing (run on GitHub)


def decode(path: Path):
    """16 kHz mono samples, read by ffmpeg."""
    import numpy as np  # noqa: PLC0415
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


PIECE_SECONDS = 3600


def pieces(samples) -> list[tuple[int, int]]:
    """Where to cut a long recording so each piece is heard on its own: about an hour each, at the
    quietest half second near each hour. The model's feature step holds a whole piece in memory at
    once, and a four-hour reel heard whole used more than a GitHub runner has (Nov. 28, 2026: the
    runners hearing the four longest reels shut down)."""
    import numpy as np  # noqa: PLC0415
    rate, step = 16000, 8000
    cuts, at = [0], PIECE_SECONDS * rate
    while at < len(samples) - 10 * 60 * rate:
        lo, hi = max(cuts[-1] + rate, at - 60 * rate), min(len(samples) - step, at + 60 * rate)
        window = samples[lo:hi][: (hi - lo) // step * step].reshape(-1, step)
        quiet = int(np.argmin((window ** 2).mean(axis=1)))
        cuts.append(lo + quiet * step + step // 2)
        at = cuts[-1] + PIECE_SECONDS * rate
    cuts.append(len(samples))
    return list(zip(cuts, cuts[1:]))


def fetch(link: str, path: Path, tries: int = 4) -> None:
    for attempt in range(tries):
        try:
            request_ = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request_, timeout=180) as response, path.open("wb") as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)
            return
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(10 * (attempt + 1))


def transcribe(args: argparse.Namespace) -> None:
    from faster_whisper import WhisperModel  # noqa: PLC0415

    plan = json.loads(Path(args.request).read_text(encoding="utf-8"))
    shard = plan["shards"][args.shard]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(plan["model"], device="cpu", compute_type="int8")
    for f in shard:
        target = out / (f["key"].replace("/", "__") + ".json.gz")
        if target.exists():
            continue
        started = time.time()
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "recording"
            fetch(f["link"], path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            samples = decode(path)
        heard = []
        for start, end in pieces(samples):
            offset = start / 16000
            segments, _ = model.transcribe(samples[start:end], language="en", beam_size=5, vad_filter=f.get("vad", True), word_timestamps=True,
                                           condition_on_previous_text=False)
            heard += [[round(offset + s.start, 2), round(offset + s.end, 2), s.text.strip(),
                       [[round(offset + w.start, 2), round(offset + w.end, 2), w.word.strip()] for w in (s.words or [])]] for s in segments]
        with gzip.open(target, "wt", encoding="utf-8") as handle:
            json.dump({"key": f["key"], "link": f["link"], "sha256": digest, "model": MODEL_NAME,
                       "seconds": round(len(samples) / 16000, 2), **({"vad": False} if not f.get("vad", True) else {}),
                       "segments": heard}, handle)
        print(f"{f['key']}: {len(samples) / 16000 / 60:.0f} min heard in {(time.time() - started) / 60:.1f} min", flush=True)


# ---------------------------------------------------------------- reading the official transcripts

SPEAKER = re.compile(
    r"^(?P<label>(?:MR\.\s+)?CHIEF\s+JUSTICE\s+[A-Z][A-Za-z'\-]+|JUSTICE\s+[A-Z][A-Za-z'\-]+|"
    r"(?:MR|MS|MRS|MISS)\.\s+[A-Z][A-Za-z'\-]+(?:\s+(?:JR|II|III)\.?)?|GENERAL\s+[A-Z][A-Za-z'\-]+|QUESTION|THE\s+COURT|THE\s+CLERK|THE\s+MARSHAL)"
    r"\s*[:;]\s*(?P<rest>.*)$")
SECTION = re.compile(r"^(?:ORAL\s+ARGUMENT|REBUTTAL\s+ARGUMENT|FURTHER\s+REBUTTAL|ARGUMENT|REARGUMENT)\b.*\bOF\b", re.I)
PROCEEDINGS = re.compile(r"P\s*R\s*[O0Q]\s*C\s*E\s*E\s*D\s*[I1l|]\s*[NM]\s*G\s*S", re.I)
# A scanned transcript's speaker as the text layer gives it: "MR» HILL;", "QUESTIONS", "HR. SELLER»", "CHIEF JUSTICE
# REHNQU1ST•", "QUEST TON;". Read from the line before its spaces are closed up: the speaker is followed by two or more.
LOOSE_SPEAKER = re.compile(
    r"^\s*(?:\d{1,2}\s+)?(?P<label>(?:[MHN]R\W{0,2}\s*)?CHIEF\s+JUSTICE\s+[A-Z][A-Z0-9'\-]+|JUSTICE\s+[A-Z][A-Z0-9'\-]+|"
    r"QUEST\s?[IT1l]ON|(?:[MHN]R|MS|MRS)\W{0,2}\s*[A-Z][A-Z0-9'\-]+|GENERAL\s+[A-Z][A-Z'\-]+)"
    r"(?:[^\sA-Za-z0-9]{1,2}|[s3S?'’]{1,2})?\s{2,}(?P<rest>\S.*)$")


def loose_label(label: str) -> str:
    """A scanned speaker's label in the form SPEAKER reads: "MR® CHIEF JUSTICE BURGER" is "MR. CHIEF JUSTICE BURGER"."""
    label = re.sub(r"\s+", " ", label).strip()
    if re.match(r"QUEST\s?[IT1l]ON$", label):
        return "QUESTION"
    label = re.sub(r"^[MHN]R\W{0,2}\s*", "MR. ", label)
    label = re.sub(r"^MS\W{0,2}\s*", "MS. ", label)
    label = re.sub(r"^MRS\W{0,2}\s*", "MRS. ", label)
    words = label.split()
    words[-1] = words[-1].replace("1", "I").replace("0", "O")
    return " ".join(words)
NOISE = re.compile(r"(Heritage Reporting Corporation|Alderson Reporting|A[LI1]DERSON\s+REPORTING|REPORTING\s+COMPANY|"
                   r"FOURTEENTH\s+STREET|^\s*SUITE\s+\d+\s*$|\(800\)\s*FOR\s+DEPO|\(202\)\s*\d{2,3}\s*-\s*\d{4}|Subject to Final Review|"
                   r"VIRGINIA\s+AVE|^\s*20\s+F\s+ST|Ace-Federal Reporters|Hoover Reporting|"
                   r"^\s*Official\s*$|^\s*\d{1,3}\s*$|www\.\S+|^\s*\(\d{3}\)\s*\d{3}-\d{4}\s*$|1111 14th Street|Washington, D\.?\s*C\.?\s*\d{5})", re.I)
END = re.compile(r"\((?:Whereupon|The case was submitted)", re.I)


def pdf_text(path: Path) -> list[str]:
    """The transcript's pages as text, in reading order."""
    out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True).stdout
    return out.split("\f")


def clean_line(line: str) -> str:
    # The reporter's dash and hyphen come out of the text layer as soft hyphens: "that was a \xad\xad a decision", "14\xad510".
    line = line.replace("\xad\xad", "--").replace("\xad", "-")
    line = re.sub(r"^\s*\d{1,2}(?=\s{2,}|\s*$)", "", line)  # the reporter's line numbers
    return re.sub(r"\s+", " ", line).strip()


def read_transcript(path: Path) -> dict:
    """Counsel (from the appearances), and each turn: who speaks, the text and its page."""
    pages = pdf_text(path)
    lines = [(number, clean_line(line), line) for number, page in enumerate(pages, 1) for line in page.split("\n")]
    lines = [(n, l, r) for n, l, r in lines if l and not NOISE.search(l)]
    text_all = "\n".join(l for _, l, _r in lines)
    alone = [i for i, (_, l, _r) in enumerate(lines) if mostly_capitals(l) and PROCEEDINGS.fullmatch(re.sub(r"[^A-Za-z0-9|]", "", l))]
    start = alone[0] if alone else next((i for i, (_, l, _r) in enumerate(lines) if PROCEEDINGS.search(l.replace(" ", "")) or PROCEEDINGS.search(l)), None)
    appearances = []
    head = "\n".join(l for _, l, _r in lines[: start or 0])
    found = re.search(r"APPEARANCES?\s*:?(.*?)(?:C\s*[O0]\s*N\s*T\s*E\s*[NH]\s*T\s*S|ORAL\s+ARGUMENT\s+OF|$)", head, re.S | re.I)
    if found:
        block = re.sub(r"\s+", " ", found.group(1)).strip()
        for entry in re.split(r"(?<![\s.][A-Z]\.)(?<=[.;])\s+(?=[A-Z][A-Z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+)*,\s+(?:ESQ|JR|II|III|Solicitor|Deputy|Assistant|Attorney|General|Special))", block):
            name = re.match(r"([A-Z][A-Za-z.'\-]*(?:\s+[A-Za-z][A-Za-z.'\-]*)*?(?:,\s+(?:JR\.|II|III))?),", entry)
            if not name:
                continue
            behalf = re.search(r"(?:on behalf of|for)\s+(?:the\s+)?(.+?)\.?$", entry, re.I)
            appearances.append({"name": clean_name(name.group(1)), "for": behalf.group(1).strip().rstrip(".") if behalf else "",
                                "text": entry.strip()})
    turns, sections = [], []
    current = None
    body = lines[(start or 0) + 1:]
    i = 0
    while i < len(body):
        page, line, raw = body[i]
        if END.search(line):
            break
        if SECTION.match(line) and mostly_capitals(line):
            heading = line
            while i + 1 < len(body) and mostly_capitals(body[i + 1][1]) and re.match(r"(ON BEHALF|BEHALF|OF THE|IN CASE|FOR THE|FOR |AS AMICUS|SUPPORTING|ESQ|[A-Z0-9 .,'\-]+$)", body[i + 1][1]) \
                    and not SPEAKER.match(body[i + 1][1]) and not SECTION.match(body[i + 1][1]):
                heading += " " + body[i + 1][1]
                i += 1
            sections.append({"heading": heading, "turn": len(turns)})
            i += 1
            continue
        m = SPEAKER.match(line)
        loose = None if m else LOOSE_SPEAKER.match(re.sub(r"^\s*\d{1,2}(?=\s)", "", raw))
        if m:
            current = {"label": re.sub(r"\s+", " ", m.group("label")), "page": page, "text": m.group("rest")}
            turns.append(current)
        elif loose:
            current = {"label": loose_label(loose.group("label")), "page": page, "text": re.sub(r"\s+", " ", loose.group("rest")).strip()}
            turns.append(current)
        elif current:
            current["text"] = (current["text"] + " " + line).strip()
        i += 1
    counsel = []
    for section in sections:
        m = re.search(r"\bOF\s+(.+?)(?:,?\s+ESQ\.?(?:UIRE)?)?(?:,?\s+(?:ON BEHALF OF|FOR|AS AMICUS|SUPPORTING)\s+(.+?))?[.,]?$", section["heading"], re.I)
        if not m or not re.search(r"[A-Za-z]", m.group(1)):
            continue
        raw = re.split(r"[,*]?\s*\b(?:ESQ|ESC|ES[O0]|EBO|ON\s+BEHALF|FOR\s+THE)\b", m.group(1), maxsplit=1, flags=re.I)[0]
        raw = re.sub(r"\s+ON$", "", raw.strip(" ,.:*#"), flags=re.I)
        raw = re.sub(r"^(?:[A-Z]\s+)?(?:PAGE|PACE|FACE)\b\W*", "", raw, flags=re.I)
        raw = re.sub(r"^(?:M[RS]S?|NS)\.?\s+", "", raw)
        name = clean_name(raw)
        if not name:
            continue
        name = re.sub(r",? (Jr)\.?$", r", \1.", name)
        rebuttal = bool(re.match(r"REBUTTAL|FURTHER REBUTTAL", section["heading"], re.I))
        section["counsel"] = name
        section["label"] = ("Rebuttal" if rebuttal else surname_of(name))
        if not any(c["name"] == name for c in counsel):
            behalf = (m.group(2) or "").strip(" .,")
            behalf = sentence_case_keep(behalf) if behalf else ""
            behalf = re.sub(r"\s*[-—–]+\s*Rebuttal$", "", re.sub(r"^The ", "the ", behalf), flags=re.I)
            counsel.append({"name": name, "for": re.sub(r"\bet al$", "et al.", behalf)})
    # The appearances say whom each counsel argued for when the heading does not, and spell the name
    # as the appearances do where a heading's scan misreads it ("SCWHARTZ" for SCHWARTZ).
    for c in counsel:
        best = max(appearances, key=lambda a: similar(a["name"], c["name"]), default=None)
        if best and similar(best["name"], c["name"]) >= 0.8:
            renamed = re.sub(r",? (Jr)\.?$", r", \1.", best["name"])
            for section in sections:
                if section.get("counsel") == c["name"]:
                    section["counsel"] = renamed
                    section["label"] = section["label"] if section["label"] == "Rebuttal" else surname_of(renamed)
            c["name"] = renamed
            c["for"] = c["for"] or best["for"]
            c["title"] = appearance_title(best["text"])
    merged: list[dict] = []
    for c in counsel:
        same = next((k for k in merged if similar(k["name"], c["name"]) >= 0.85), None)
        if same is None:
            merged.append(c)
            continue
        for section in sections:
            if section.get("counsel") == c["name"]:
                section["counsel"] = same["name"]
        same["for"] = same["for"] or c["for"]
        same.setdefault("title", c.get("title"))
    if not merged:
        # No argument headings could be read: the appearances name the counsel.
        merged = [{"name": a["name"], "for": a["for"], "title": appearance_title(a["text"])} for a in appearances if a["name"]]
    appearances = merged
    for t in turns:
        t["text"] = re.sub(r"\s+", " ", t["text"]).strip()
    # Sections point at the turn they open; keep them pointing there once empty turns are dropped.
    kept = [t for t in turns if t["text"]]
    for section in sections:
        section["turn"] = sum(1 for t in turns[: section["turn"]] if t["text"])
    return {"counsel": appearances, "turns": kept, "sections": sections,
            "has_proceedings": start is not None, "chars": len(text_all)}


ROLE = re.compile(r"\b(General|Solicitor|Attorney|Counsel|Assistant|Deputy|Professor|Secretary|Governor|Chief)\b")
OFFICE = re.compile(r"^(Department|Office|Bureau|Division)\b")


def appearance_title(text: str) -> str:
    """What the appearances say of a counsel besides the name: the office ("Assistant Attorney General",
    "Solicitor General of Texas"), else the department, else the city ("Window Rock, Arizona")."""
    rest = re.sub(r"^.*?,\s*ESQ(?:UIRE|\.)?\s*,?", "", text, count=1, flags=re.I | re.S)
    if rest == text:
        rest = re.sub(r"^[^,]+,", "", text, count=1)
    rest = re.split(r";|\b(?:on behalf of|for the|for petitioner|for respondent|for appellant|for appellee)\b", rest, maxsplit=1, flags=re.I)[0]
    parts = [re.sub(r"\s+\d{5}(?:-\d{4})?$", "", re.sub(r"\s+", " ", x).strip()) for x in rest.split(",")]
    # A closing period goes, but not one that ends an abbreviation ("Ore.", "D.C.").
    parts = [x[:-1] if x.endswith(".") and not re.search(r"\b(?:[A-Z][a-z]{0,3}|[A-Z])\.$", x) else x for x in parts]
    parts = [x for x in parts if x and not re.search(r"\d|P\.\s*O\.|Box\b|Suite\b|Street|Avenue", x)]
    roles = [x for x in parts if ROLE.search(x)]
    if roles:
        return re.sub(r"\s+of\s*$", "", roles[0])
    offices = [x for x in parts if OFFICE.match(x)]
    if offices:
        return offices[0]
    place = ", ".join(parts[-2:])
    return re.sub(r"D\.\s*C$", "D.C.", place).replace("D. C.", "D.C.")


def clean_name(raw: str) -> str:
    """A counsel's name from a heading or the appearances, without what a scan adds: "ROBERT P» WILL" is Robert P. Will,
    "DREW So DAYS, III" Drew S. Days, III, "IAN Aa MACPEERSON, ESQOJ" Ian A. Macpeerson."""
    raw = re.split(r"\s\d{2,}\s|,?\s*\bESQ\w*", raw, maxsplit=1, flags=re.I)[0]
    raw = re.sub(r"^(?:M[RS]S?|NS)\W?\d?\s+", "", raw.strip())
    raw = re.sub(r"(?<=\b[A-Z])(?:[,*•»=]|[oa](?=\s))", ".", raw)
    raw = re.sub(r"\b0\.", "O.", raw)
    raw = re.sub(r"\s+,", ",", raw)
    raw = raw.strip(" ,.:*#»?")
    if not re.search(r"[A-Za-z]{2}", raw):
        return ""
    name = titlecase(raw)
    return re.sub(r",? (Jr)\.?$", r", \1.", name)


def similar(one: str, other: str) -> float:
    """How alike two counsel names are, ignoring case, titles and punctuation."""
    plain = lambda text: re.sub(r"[^a-z ]", "", re.sub(r",?\s*\b(?:jr|ii|iii|esq)\b\.?", "", text.lower())).split()
    a, b = plain(one), plain(other)
    if not a or not b:
        return 0.0
    surname = difflib.SequenceMatcher(None, a[-1], b[-1]).ratio()
    # A heading that gives the surname alone ("ARGUMENT OF MR. KNEEDLER") is compared by the surname.
    return surname if len(a) == 1 or len(b) == 1 or a[0][:1] == b[0][:1] else surname * 0.7


def mostly_capitals(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 3 and sum(c.isupper() for c in letters) / len(letters) > 0.85


def surname_of(name: str) -> str:
    words = [w for w in re.sub(r",.*$", "", name).split() if w.rstrip(".") not in ("Jr", "II", "III", "IV")]
    return words[-1] if words else name


def sentence_case_keep(text: str) -> str:
    """An all-capitals phrase from a heading in ordinary case: 'CHAD EVERET BRACKEEN, ET AL.' to 'Chad Everet Brackeen, et al.'"""
    if not mostly_capitals(text):
        return text
    small = {"of", "the", "and", "for", "as", "et", "al", "al.", "on", "in", "a", "an", "to"}
    words = text.lower().split()
    out = [w if (i and w.strip(",.") in small) else titlecase(w.upper()) for i, w in enumerate(words)]
    return " ".join(out).replace("Et Al", "et al")


def titlecase(name: str) -> str:
    parts = []
    for word in name.split():
        if word.strip(",.") in ("II", "III", "IV"):
            parts.append(word)
        elif re.match(r"^MC[A-Z]", word, re.I) and len(word) > 3:
            parts.append("Mc" + word[2:].capitalize())
        else:
            # O'TOOLE is O'Toole.
            parts.append("-".join(re.sub(r"^([A-Za-z]['’])([a-z])", lambda m: m.group(1).upper() + m.group(2).upper(), w.capitalize())
                                  for w in word.split("-")))
    return " ".join(parts).replace("Jr.", "Jr.")


def sentence_case(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text[:1].upper() + text[1:]


def display_name(label: str) -> tuple[str, str]:
    """How a turn's speaker is shown, and their role (justice, counsel or unknown)."""
    label = re.sub(r"\s+", " ", label.upper())
    m = re.match(r"(?:MR\. )?CHIEF JUSTICE (.+)", label)
    if m:
        return f"Chief Justice {m.group(1).title()}", "justice"
    m = re.match(r"JUSTICE (.+)", label)
    if m:
        return f"Justice {m.group(1).title()}", "justice"
    if label == "QUESTION":
        return "A Justice", "justice"
    m = re.match(r"(MR|MS|MRS|MISS|GENERAL)\.? (.+)", label)
    if m:
        surname = m.group(2).strip()
        surname = "Mc" + surname[2:].capitalize() if re.match(r"MC[A-Z]", surname) else surname.title()
        return f"{m.group(1).title()}{'.' if m.group(1) != 'GENERAL' else ''} {surname}", "counsel"
    return label.title(), "unknown"


# ---------------------------------------------------------------- matching the words heard to the transcript


def words_of(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))]


def heard_words(argument: dict, words_dir: Path) -> tuple[list[tuple[float, str]], list[dict]] | None:
    """Every word heard across the argument's files, with its time from the start of the first file."""
    stream, files, offset = [], [], 0.0
    for f in argument["files"]:
        path = words_dir / (f["key"].replace("/", "__") + ".json.gz")
        if not path.exists():
            return None
        heard = json.load(gzip.open(path, "rt", encoding="utf-8"))
        for segment in heard["segments"]:
            for start, _end, word in segment[3]:
                for w in words_of(word):
                    stream.append((round(offset + start, 2), w))
        files.append({"key": f["key"], "offset": round(offset, 2), "duration": heard["seconds"], "heard": heard})
        offset += heard["seconds"]
    return stream, files


def locate(official: list[str], heard: list[str]) -> tuple[int, int]:
    """Where in the words heard the transcript lies: the span holding most of its five-word runs."""
    grams = {tuple(official[i:i + 5]) for i in range(len(official) - 4)}
    hits = [i for i in range(len(heard) - 4) if tuple(heard[i:i + 5]) in grams]
    if not hits:
        return 0, len(heard)
    lo, hi = hits[len(hits) // 50], hits[-1 - len(hits) // 50]
    return max(0, lo - 300), min(len(heard), hi + 300)


SECONDS_PER_WORD = 0.36  # about 165 words a minute, the pace of argument


MISSING_TURNS = 8
# Fewer turns than this read from an official transcript means its scan could not be read turn by turn.
MIN_OFFICIAL_TURNS = 20


def time_turns(turns: list[dict], stream: list[tuple[float, str]]) -> dict:
    """Give each turn the moment it begins, from the words of it that were heard.

    The transcript's words are matched to the words heard in order (difflib); runs of
    three or more words count, so a lone "the" does not pull a turn out of place. A turn
    begins at its first counted word, less the time its words before that one take to say.
    Turns with no counted word take a time between their neighbours'. Returns the share of
    the transcript's words matched and the first and last times matched (the argument's span
    within a whole day's recording)."""
    official, owner, place = [], [], []
    for i, t in enumerate(turns):
        for k, w in enumerate(words_of(t["text"])):
            official.append(w)
            owner.append(i)
            place.append(k)
    heard = [w for _, w in stream]
    lo, hi = locate(official, heard)
    window = heard[lo:hi]
    matcher = difflib.SequenceMatcher(None, official, window, autojunk=False)
    first: dict[int, float] = {}
    matched, span = 0, [None, None]
    for a, b, size in matcher.get_matching_blocks():
        if size < 3:
            continue
        matched += size
        start, end = stream[lo + b][0], stream[lo + b + size - 1][0]
        span[0] = start if span[0] is None else span[0]
        span[1] = end
        for k in range(size):
            i = owner[a + k]
            if i not in first:
                first[i] = stream[lo + b + k][0] - place[a + k] * SECONDS_PER_WORD
    times = [first.get(i) for i in range(len(turns))]
    known = [i for i, t in enumerate(times) if t is not None]
    for i in range(len(times)):
        if times[i] is None:
            before = max((k for k in known if k < i), default=None)
            after = min((k for k in known if k > i), default=None)
            if before is not None and after is not None:
                times[i] = times[before] + (times[after] - times[before]) * (i - before) / (after - before)
            else:
                times[i] = times[before] if before is not None else (times[after] if after is not None else 0.0)
    for i in range(1, len(times)):
        times[i] = max(times[i], times[i - 1])
    for t, s in zip(turns, times):
        t["t"] = round(max(0.0, s), 1)
    # A recording that begins or ends partway through the argument (Minnesota v. Mille Lacs Band, 1998: the
    # Archives' reel begins some forty minutes in): the turns before the first one heard, or after the last,
    # are not in it. A few unheard turns at either end are only the call of the case or a closing word.
    if known:
        head = known[0] > MISSING_TURNS
        tail = len(turns) - 1 - known[-1] > MISSING_TURNS
        for i, t in enumerate(turns):
            if (head and i < known[0]) or (tail and i > known[-1]):
                t["missing"] = True
    return {"matched": matched / max(1, len(official)), "timed": len(known), "span": span,
            "missing": sum(1 for t in turns if t.get("missing"))}


# ---------------------------------------------------------------- our own transcripts, where there is none

CALL = re.compile(r"\b(?:hear|heard|resume)\s+(?:the\s+)?arguments?\b|\bnumber\s+\d+|\bno\.\s*\d+|\bcase on today's calendar\b", re.I)
# "The case is submitted", "Gentlemen, cases submitted", "If there are no further questions, submitted." but not
# "We know that this was submitted solely ..." (Bakke, 1977), "It is submitted that ..." (Oneida, 1973) or
# "submitted an application".
SUBMITTED = re.compile(r"\b(?:case is|cases?)\s+submitted\b|\bsubmitted\s*\.", re.I)
COMMON = {"v", "the", "of", "and", "united", "states", "state", "county", "tribe", "tribes", "indian", "indians", "nation",
          "nations", "inc", "co", "company", "et", "al", "district", "court", "for", "village", "community", "department",
          "superintendent", "penitentiary", "game", "commission", "tax", "washington", "arizona", "california"}


def segments_in(files: list[dict]) -> list[tuple[float, float, str]]:
    return [(round(f["offset"] + a, 2), round(f["offset"] + b, 2), text)
            for f in files for a, b, text, _words in f["heard"]["segments"]]


def named(names: set[str], text: str) -> bool:
    """Whether the words heard name the case: "Clark" for Clarke, as the model hears it."""
    for heard in words_of(text):
        for name in names:
            if heard == name or (len(heard) >= 5 and (name.startswith(heard) or heard.startswith(name))) \
                    or (len(heard) > 4 and difflib.SequenceMatcher(None, heard, name).ratio() >= 0.85):
                return True
    return False


def called(names: set[str], docket: str, text: str) -> bool:
    """Whether a call names the case, by a party's name or by its docket number as the model hears it:
    "7253 23, Keeble against the United States" ("We'll hear arguments next in 7253 23 key bowl ...") for 72-5323."""
    digits = re.sub(r"\D", "", docket)
    return named(names, text) or (len(digits) >= 4 and digits in re.sub(r"[\s,.-]", "", text))


def case_span(segments: list[tuple[float, float, str]], title: str, total: float, continues: bool = False,
              docket: str = "", hint: tuple[float, float] | None = None) -> tuple[float, float, str]:
    """Where one case lies in a whole day's recording: from the Chief Justice's call of the case
    ("We'll hear arguments next in No. 72-1309, Oneida Indian Nation against ...", or "We'll resume
    arguments in ...") to "submitted" or the next call. Where the call is not heard: a case argued on
    into the next day is the day's last case called; a second day's reel goes straight on with the case
    from its beginning."""
    names = {w for w in words_of(title) if len(w) > 3 and w not in COMMON}
    start, how = None, "call"
    for i, (a, _b, text) in enumerate(segments):
        near = " ".join(t for _, _, t in segments[i:i + 3])
        if CALL.search(text) and called(names, docket, near):
            start = a
            break
    if start is None and continues:
        calls = [a for a, _b, text in segments if CALL.search(text) and re.search(r"\bhear", text, re.I)]
        if calls:
            start, how = calls[-1], "the day's last case called"
    if start is None and hint:
        # Where the call is misheard ("the United States against no time" for Mottaz, 1986), the stretch where the
        # words of the Court's transcript were heard, though its scan gives no turns.
        return hint[0], hint[1], "the Court's transcript's words"
    if start is None:
        start, how = 0.0, "start of the recording"
    end = total
    for a, b, text in segments:
        if a <= start + 300:
            continue
        if SUBMITTED.search(text):
            end = b + 2
            break
        if CALL.search(text) and re.search(r"hear", text, re.I) and not called(names, docket, text):
            end = a
            break
    return start, end, how


def own_turns(segments: list[tuple[float, float, str]], start: float, end: float) -> list[dict]:
    """Paragraphs of the words heard: a new one after a pause that ends a sentence, or after 80 words.
    The speakers are not marked."""
    turns, current, last_end = [], None, None
    for a, b, text in segments:
        if a < start - 0.5 or a >= end:
            continue
        text = text.strip()
        if not text:
            continue
        pause = a - last_end if last_end is not None else 0
        if current and ((pause > 0.7 and current["text"][-1:] in ".?!") or pause > 3 or len(current["text"].split()) > 80):
            turns.append(current)
            current = None
        if current is None:
            current = {"t": round(a, 1), "text": text}
        else:
            current["text"] += " " + text
        last_end = b
    if current:
        turns.append(current)
    return turns


# ---------------------------------------------------------------- counsel where there is no transcript

COUNSEL_PLAN = ROOT / "research" / "supreme-court-coverage" / "argument-counsel.csv"


def reported_counsel(case: str, day: str) -> list[dict]:
    """Who argued, from the report's syllabus page ("Norman M. Littell argued the cause for petitioners."),
    read by hand into research/supreme-court-coverage/argument-counsel.csv because the scans' text layer
    misreads names. A row with days applies to those days only (a reargument)."""
    if not COUNSEL_PLAN.exists():
        return []
    out = []
    for r in csv.DictReader(COUNSEL_PLAN.open(encoding="utf-8")):
        if r["case"] == case and (not r["days"] or day in r["days"].split()):
            out.append({"name": r["name"], "title": r["title"], "for": r["for"], "source": r["source"]})
    return out


# ---------------------------------------------------------------- align: one timed transcript per argument


def record_of(case: str) -> dict:
    import yaml  # noqa: PLC0415
    path = next(SOURCES.glob(f"*/{case}.yaml"))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def turn_out(t: dict, counsel: list[dict]) -> dict:
    speaker, role = display_name(t["label"])
    out = {"t": t["t"], "speaker": speaker, "role": role, "page": t["page"], "text": t["text"]}
    if t.get("missing"):
        out["missing"] = True
    if role == "counsel":
        surname = speaker.split()[-1].lower()
        for i, c in enumerate(counsel):
            if surname_of(c["name"]).lower() == surname:
                out["counsel"] = i
                break
    return out


def align(args: argparse.Namespace) -> None:
    words_dir, out_dir, work = Path(args.words), Path(args.out), Path(args.work)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = []
    every = arguments()
    for argument in every:
        if args.case and argument["case"] not in args.case:
            continue
        found = heard_words(argument, words_dir)
        if not found:
            report.append({"case": argument["case"], "date": argument["date"], "status": "not heard yet"})
            continue
        stream, files = found
        heard_files, hint = files, None
        total = round(sum(f["duration"] for f in files), 2)
        timed = {"case": argument["case"], "date": argument["date"], "model": MODEL_NAME,
                 "files": [{"key": f["key"], "offset": f["offset"], "duration": f["duration"], "sha256": f["heard"]["sha256"]}
                           for f in files]}
        if argument["official"]:
            counsel, sections, turns, matched, spans, timed_turns, words, missing = [], [], [], [], [], 0, 0, 0
            for official in argument["official"]:
                path = work / official["key"]
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    fetch(official["link"], path)
                read = read_transcript(path)
                result = time_turns(read["turns"], stream)
                base = len(turns)
                for c in read["counsel"]:
                    if not any(c["name"] == k["name"] for k in counsel):
                        counsel.append(c)
                for section in read["sections"]:
                    if section.get("counsel") and section["turn"] < len(read["turns"]):
                        # A counsel's second turn at the lectern is the rebuttal, however the heading reads.
                        if any(other["counsel"] == section["counsel"] for other in sections):
                            section["label"] = "Rebuttal"
                        sections.append({"label": section["label"], "counsel": section["counsel"],
                                         "t": read["turns"][section["turn"]]["t"], "turn": base + section["turn"],
                                         **({"missing": True} if read["turns"][section["turn"]].get("missing") else {})})
                turns += [turn_out(t, counsel) for t in read["turns"]]
                n = sum(len(words_of(t["text"])) for t in read["turns"])
                matched.append(result["matched"] * n)
                words += n
                timed_turns += result["timed"]
                missing += result["missing"]
                spans.append(result["span"])
            spans = [s for s in spans if s[0] is not None]
            whole = any(f["whole_session"] for f in argument["files"])
            if spans and not whole:
                # The Archives' one-file-a-case reels, where the plan took a whole reel's files for want of a
                # docket in their names (Affiliated Ute Citizens, 1971: four cases' files): keep the files the
                # argument was heard in.
                lo, hi = min(x[0] for x in spans), max(x[1] for x in spans)
                keep = [f for f in files if f["offset"] < hi and f["offset"] + f["duration"] > lo]
                if keep and len(keep) < len(files):
                    shift = keep[0]["offset"]
                    for t in turns:
                        t["t"] = round(max(0.0, t["t"] - shift), 1)
                    for x in sections:
                        x["t"] = round(max(0.0, x["t"] - shift), 1)
                    files = [{**f, "offset": round(f["offset"] - shift, 2)} for f in keep]
                    total = round(sum(f["duration"] for f in files), 2)
                    timed["files"] = [{"key": f["key"], "offset": f["offset"], "duration": f["duration"], "sha256": f["heard"]["sha256"]}
                                      for f in files]
            span = [max(0.0, min(s[0] for s in spans) - 20), min(total, max(s[1] for s in spans) + 20)] if (spans and whole) else [0.0, total]
            for c in counsel:
                c["for"] = c.get("for") or ""
            timed.update({"made": "official", "transcripts": [{"key": o["key"], "link": o["link"], "docket": o["docket"]} for o in argument["official"]],
                          "span": [round(span[0], 1), round(span[1], 1)], "counsel": [{"name": c["name"], "title": c.get("title", ""), "for": c["for"]} for c in counsel],
                          "sections": sections, "turns": turns})
            share = sum(matched) / max(1, words)
            report.append({"case": argument["case"], "date": argument["date"], "status": "official",
                           "matched": round(share, 3), "turns": len(turns), "timed": timed_turns, "span": timed["span"],
                           **({"not_in_recording": missing} if missing else {})})
            if len(turns) < MIN_OFFICIAL_TURNS:
                trimmed = files if share >= 0.5 else heard_files
                if share >= 0.2 and all(f["whole_session"] for f in argument["files"]):
                    hint = tuple(report[-1]["span"])
                # A scan whose speakers the text layer does not give (Keeble, 1973: two turns; Bakke, 1977: six) has
                # no turns to time, so the player shows our own transcript and links the Court's.
                report[-1]["status"] = "official unread"
                # The files the argument was heard in stand where enough of the scan's words were matched.
                files = trimmed
                total = round(sum(f["duration"] for f in files), 2)
                timed = {"case": argument["case"], "date": argument["date"], "model": MODEL_NAME,
                         "files": [{"key": f["key"], "offset": f["offset"], "duration": f["duration"], "sha256": f["heard"]["sha256"]}
                                   for f in files]}
        if "made" not in timed:
            segments = segments_in(files)
            whole = any(f["whole_session"] for f in argument["files"])
            record = record_of(argument["case"])
            if whole:
                # Argued on into the next day ("We'll resume arguments in the United States against Clark").
                continues = any(other["case"] == argument["case"] and 0 < (dt.date.fromisoformat(other["date"]) - dt.date.fromisoformat(argument["date"])).days <= 3
                                for other in every)
                start, end, how = case_span(segments, record["citation"]["title"], total, continues, " ".join(argument["dockets"][:1]), hint)
            else:
                start, end, how = 0.0, total, "the case's own recording"
            counsel = reported_counsel(argument["case"], argument["date"])
            turns = [{"t": t["t"], "speaker": "", "role": "unknown", "text": t["text"]} for t in own_turns(segments, start, end)]
            timed.update({"made": "speech recognition", "span": [round(start, 1), round(end, 1)], "counsel": counsel,
                          "sections": [], "turns": turns})
            report.append({"case": argument["case"], "date": argument["date"], "status": "speech recognition",
                           "turns": len(turns), "span": timed["span"], "span_from": how, "counsel": len(counsel)})
        target = out_dir / argument["case"] / f"{argument['date']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(timed, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    (out_dir / "report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for r in report:
        print(json.dumps(r))


# ---------------------------------------------------------------- store: the bucket, the capture index and the records

TIMED_PREFIX = f"{TIMED_TOPIC}/timed"
RIGHTS_OFFICIAL = ("The Supreme Court's transcript of the argument (supremecourt.gov), each turn timed against the "
                   "recording by the Tribal Law Project with speech recognition (Whisper small.en).")
RIGHTS_OWN = ("A transcript the Tribal Law Project made of the recording with speech recognition (Whisper small.en); "
              "there is no official transcript of this argument. The recording is a record of the National Archives.")


def timing_yaml(entries: list[dict]) -> str:
    lines = ["argument_timing:"]
    for e in entries:
        lines.append(f"- date: '{e['date']}'")
        lines.append(f"  made: {e['made']}")
        lines.append(f"  model: {e['model']}")
        lines.append(f"  turns: {e['turns']}")
        lines.append("  recording:")
        for f in e["recording"]:
            lines.append(f"  - {{key: {f['key']}, seconds: {f['duration']}}}")
        lines.append(f"  span: [{e['span'][0]}, {e['span'][1]}]")
        if e["counsel"]:
            lines.append("  counsel:")
            for c in e["counsel"]:
                lines.append(f"  - name: {json.dumps(c['name'], ensure_ascii=False)}")
                if c.get("title"):
                    lines.append(f"    title: {json.dumps(c['title'], ensure_ascii=False)}")
                if c.get("for"):
                    lines.append(f"    for: {json.dumps(c['for'], ensure_ascii=False)}")
                if c.get("source"):
                    lines.append(f"    source: {c['source']}")
        if e["sections"]:
            lines.append("  sections:")
            for x in e["sections"]:
                lines.append(f"  - {{label: {json.dumps(x['label'], ensure_ascii=False)}, counsel: {json.dumps(x['counsel'], ensure_ascii=False)}, t: {x['t']}}}")
        lines.append("  file:")
        lines.append(f"    storage_key: {e['key']}")
        lines.append(f"    checksum: {e['sha256']}")
        lines.append("    media_type: application/json")
        lines.append(f"    bytes: {e['bytes']}")
    return "\n".join(lines) + "\n"


def store(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(ROOT / "scripts" / "captures"))
    import mirror_captures  # noqa: PLC0415

    timed_dir = Path(args.timed)
    index = mirror_captures.load_index()
    filed = {row["key"]: row for row in index}
    s3 = mirror_captures.client()
    bucket = mirror_captures.bucket_name()
    by_case = collections.defaultdict(list)
    uploaded = 0
    for path in sorted(timed_dir.glob("*/*.json")):
        timed = json.loads(path.read_text(encoding="utf-8"))
        if not timed.get("turns"):
            continue
        data = path.read_bytes()
        digest, size = hashlib.sha256(data).hexdigest(), len(data)
        key = f"{TIMED_PREFIX}/{timed['case']}/{timed['date']}.json"
        if mirror_captures.remote_state(s3, bucket, key) != (size, digest):
            s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/json",
                          Metadata={"sha256": digest, "captured": dt.date.today().isoformat()})
            uploaded += 1
        if mirror_captures.remote_state(s3, bucket, key) != (size, digest):
            print(f"upload failed: {key}", file=sys.stderr)
            continue
        official = timed["made"] == "official"
        row = mirror_captures.index_row(
            key=key, size=size, digest=digest, media="application/json", topic=TIMED_TOPIC,
            title=(f"Timed transcript of the argument of {timed['date']}" if official else
                   f"Speech recognition transcript of the argument of {timed['date']}") + f" ({timed['case']})",
            nations="", obtained="made by scripts/caselaw/supreme_court_argument_timing.py",
            links=[t["link"] for t in timed.get("transcripts", [])] or [f["key"] for f in timed["files"]][:1],
            record=timed["case"], redistribution="yes", rights=RIGHTS_OFFICIAL if official else RIGHTS_OWN,
            existing_keys=set(filed))
        if key in filed:
            filed[key].update({k: row[k] for k in ("bytes", "sha256", "title", "link", "rights")})
        else:
            index.append(row)
            filed[key] = row
        by_case[timed["case"]].append({"date": timed["date"], "made": "official transcript" if official else "speech recognition",
                                       "model": timed["model"], "turns": len(timed["turns"]), "key": key,
                                       "recording": timed["files"], "span": timed["span"],
                                       "counsel": timed["counsel"],
                                       # A counsel whose argument is not in the recording has no place on its track.
                                       "sections": [x for x in timed["sections"] if not x.get("missing")],
                                       "sha256": digest, "bytes": size})
    mirror_captures.count_new_pages = lambda rows: None
    mirror_captures.write_index(index)
    written = 0
    for path in sorted(SOURCES.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        found = re.search(r"(?m)^key: (\S+)", text)
        if not found or found.group(1) not in by_case:
            continue
        text = re.sub(r"(?m)^argument_timing:\n(?:[- ] .*\n)*", "", text)
        block = timing_yaml(sorted(by_case[found.group(1)], key=lambda e: e["date"]))
        text = text.replace("\nrights:\n", "\n" + block + "rights:\n", 1) if "\nrights:\n" in text else text.rstrip("\n") + "\n" + block
        path.write_text(text, encoding="utf-8")
        written += 1
    print(f"uploaded {uploaded}; argument_timing written on {written} records")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("request")
    p.add_argument("--again", type=int, nargs="*", help="shards of the first request to hear again")
    p.add_argument("--keys", nargs="*", help="these recordings, one to a shard")
    p.add_argument("--no-vad", action="store_true", help="with --keys: hear them without the voice activity filter")
    p.add_argument("--unheard", action="store_true", help="hear every recording the arguments play whose words are not in --words")
    p.add_argument("--words", help="with --again or --unheard: the words already heard")
    p.add_argument("--name", default="again", help="with --again: the new request's name")
    p = sub.add_parser("transcribe")
    p.add_argument("--request", required=True)
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("align")
    p.add_argument("--words", required=True, help="the words heard: the workflow's artifacts, unpacked into one folder")
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=str(Path(tempfile.gettempdir()) / "argument-timing"), help="downloaded transcripts and reports")
    p.add_argument("--case", nargs="*", help="only these cases")
    p = sub.add_parser("store")
    p.add_argument("--timed", required=True, help="the folder align wrote")
    args = parser.parse_args()
    {"request": request, "transcribe": transcribe, "align": align, "store": store}[args.command](args)


if __name__ == "__main__":
    main()
