#!/usr/bin/env python3
"""Find, store and record the sound recordings of the Supreme Court's arguments in our cases.

Every Supreme Court case record argued or decided since the 1955 term
(content/sources, kind federal-case, authority the Supreme Court) is looked
for in two places:

- the Court's own site, which has posted the MP3 of every argument since the
  October 2010 term (supremecourt.gov/oral_arguments/argument_audio/<term>,
  one page per term, and one page per argument naming the file);
- the National Archives, which holds every recording since the 1955 term in
  Record Group 267 (Black Series, Oct. 1955 to Dec. 1972; Red Series, Dec.
  1972 to June 2005; Gold Series and "Oral Arguments and Selected
  Non-Arguments", Oct. 2005 on). The catalog descriptions are read from the
  National Archives Catalog dataset on the AWS Registry of Open Data
  (s3://nara-national-archives-catalog/descriptions/record-groups/rg_267/),
  which needs no key. The files are fetched from the catalog's own media
  host (catalog.archives.gov/medialz/), which serves the same files at the
  same paths; the storage bucket the dataset names refuses outside requests. Each description is one tape or one day in court,
  titled with the cases heard and their docket numbers ("[Case 98-791]"),
  with the day it was made. A Black Series description has one file per
  case; the later series have one file per reel, holding the whole session,
  so a case is somewhere inside it.

A case matches a recording when a docket number of the case (from the
Supreme Court Database's docket-centered file and the record's own links)
is in the recording's title and the recording was made within four days of
an argument date (the record's court_dates.argued and the Database's
argument and reargument dates). Docket numbers of the form 96-1577 are
unique, so a recording that names one matches even when the catalog's date
differs, and the difference is noted. A part of the title headed "OPINIONS"
made within four days of the decision is the Court announcing its opinion.

Subcommands, run from the repository root in this order:

  match    Build the list of recordings: research/supreme-court-coverage/argument-audio.csv,
           one row per file (case, what it holds, date, source, address,
           catalog entry, size).
  fetch    Download each file into --work, hash it, put it in the R2 bucket
           under supreme-court-audio/ and add it to the capture index. A
           file the source refuses is left with its status in the CSV and
           can be fetched later.
  records  Write each case's argument_audio block from the CSV and the
           capture index.

    python3 scripts/caselaw/supreme_court_argument_audio.py match --cache ~/audio-cache
    python3 scripts/caselaw/supreme_court_argument_audio.py fetch --cache ~/audio-cache --work ~/audio-work [--source court|archives]
    python3 scripts/caselaw/supreme_court_argument_audio.py records

The cache holds the Database's docket file, the Court's term pages and the
National Archives descriptions. R2 credentials come from the environment
(scripts/captures/mirror_captures.py).
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import csv
import datetime as dt
import hashlib
import html
import io
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "content" / "sources"
PLAN = ROOT / "research" / "supreme-court-coverage" / "argument-audio.csv"
TOPIC = "supreme-court-audio"
SCDB_DOCKETS = "http://scdb.wustl.edu/_brickFiles/2026_01/SCDB_2026_01_caseCentered_Docket.csv.zip"
COURT = "https://www.supremecourt.gov"
FIRST_COURT_TERM = 2010
DATASET = "https://nara-national-archives-catalog.s3.amazonaws.com/"
RG267 = "descriptions/record-groups/rg_267/"
CATALOG = "https://catalog.archives.gov/id/"
# The dataset gives each file's address in the Archives' storage bucket, which refuses outside requests (403, Nov. 27, 2026);
# the catalog serves the same file from its own media host, at the same path under /medialz/.
MEDIA_HOST = "https://catalog.archives.gov/medialz/"
# Catalog descriptions whose file is another day's reel, with the reel that holds the session described. Heard Nov. 28, 2026:
# 118975039 (Mar. 23, 1994, Dolan; Milhelm Attea) links 267-912, which opens "Tuesday, March 22nd, 1994" and holds
# Shannon v. United States and Tuilaepa v. California; 267-911 opens "Wednesday, March 23rd, 1994" and calls Dolan first.
MISFILED = {"118975039": {"267-912.mp3": "267-911.mp3"}}


def media_link(link: str) -> str:
    return re.sub(r"^https://s3[.a-z0-9-]*\.amazonaws\.com/NARAprodstorage/lz/", MEDIA_HOST, link)
USER_AGENT = "Tribal Law Project research script (Supreme Court argument audio)"
NEAR_DAYS = 4
FIRST_DAY = dt.date(1955, 10, 1)
FIELDS = ["case", "kind", "date", "source", "series", "catalog_id", "catalog_title", "segment", "whole_session",
          "date_note", "link", "bytes", "duration_seconds", "status"]
RIGHTS = ("Sound recording made by the Supreme Court of the United States, a work of the United States "
          "government (17 U.S.C. § 105).")


def get(url: str, tries: int = 4) -> bytes:
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code in (403, 404) or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(url)


def download(url: str, path: Path, tries: int = 3) -> tuple[int, str]:
    """Stream a file to disk, hashing it on the way; a read that stalls for two minutes is tried again."""
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            digest, size = hashlib.sha256(), 0
            with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as out:
                while chunk := response.read(1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
                expected = response.headers.get("Content-Length")
            if expected and int(expected) != size:
                raise ConnectionError(f"short read: {size} of {expected} bytes")
            return size, digest.hexdigest()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(url)


def duration(path: Path) -> int | None:
    """The recording's length in whole seconds, read by ffprobe (None if ffprobe is missing or cannot read it)."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=120).stdout.strip()
        return round(float(out)) if out else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def cached(cache: Path, name: str, url: str) -> bytes:
    path = cache / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(get(url))
    return path.read_bytes()


def us_date(text: str) -> dt.date | None:
    if not text:
        return None
    month, day, year = (int(part) for part in text.split("/"))
    return dt.date(year + (2000 if year < 50 else 1900 if year < 100 else 0), month, day)


def docket_form(text: str) -> str:
    """One spelling of a docket number: lower case, no spaces or periods, "orig" for original cases."""
    text = text.lower().replace("original", "orig").replace(".", "").replace(" ", "")
    return text.replace("-orig", "orig")


# ---------------------------------------------------------------- our cases


def our_cases(cache: Path) -> dict[str, dict]:
    """Every Supreme Court case record argued or decided since the 1955 term, with its docket numbers and dates."""
    archive = zipfile.ZipFile(io.BytesIO(cached(cache, "scdb-dockets.csv.zip", SCDB_DOCKETS)))
    rows = list(csv.DictReader(io.TextIOWrapper(archive.open(archive.namelist()[0]), encoding="latin-1")))
    by_case = collections.defaultdict(list)
    by_cite = collections.defaultdict(set)
    for row in rows:
        by_case[row["caseId"]].append(row)
        if row["usCite"]:
            by_cite[re.sub(r"\s+", " ", row["usCite"])].add(row["caseId"])
    joined = collections.defaultdict(set)
    with (ROOT / "research" / "reviews" / "2026-11-24-supreme-court-native-cases.csv").open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["scdb_case_id"]:
                joined[row["key"]].add(row["scdb_case_id"])
    cases = {}
    for path in sorted(SOURCES.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        if "authority: Supreme Court of the United States" not in text:
            continue
        record = yaml.safe_load(text)
        if record.get("kind") != "federal-case":
            continue
        citation = record["citation"]
        ids = joined.get(record["key"]) or by_cite.get(f"{citation.get('volume')} U.S. {citation.get('page')}", set())
        database = [row for case_id in ids for row in by_case.get(case_id, [])]
        argued = {dt.date.fromisoformat(str(day)) for day in (record.get("court_dates") or {}).get("argued") or []}
        for row in database:
            for field in ("dateArgument", "dateRearg"):
                if row[field]:
                    argued.add(us_date(row[field]))
        parts = ((citation.get("issued") or {}).get("date-parts") or [[0]])[0]
        decided = dt.date(*parts) if len(parts) == 3 else None
        if not (decided and decided >= FIRST_DAY) and not any(day >= FIRST_DAY for day in argued):
            continue
        dockets = {row["docket"].strip() for row in database if row["docket"].strip()}
        # The record's own links name the docket for cases the Database's release does not cite yet.
        for link in re.findall(r"https?://\S+", yaml.safe_dump(record)):
            found = re.search(r"/(?:text|cases/\d{4}|mp3files|audio/\d{4})/(\d{1,2}-\d{1,5}|\d+-?orig)\b", link, re.I)
            if found:
                dockets.add(found.group(1))
        cases[record["key"]] = {"key": record["key"], "title": citation.get("title"), "dockets": {docket_form(d) for d in dockets},
                                "argued": sorted(argued), "decided": decided}
    return cases


def near(one: dt.date, other: dt.date) -> bool:
    return abs((one - other).days) <= NEAR_DAYS


def term_of(day: dt.date) -> int:
    return day.year if day.month >= 10 else day.year - 1


# ---------------------------------------------------------------- the Court's own files


def court_rows(cases: dict, cache: Path) -> list[dict]:
    latest = term_of(dt.date.today())
    listed = []
    for term in range(FIRST_COURT_TERM, latest + 1):
        try:
            page = cached(cache, f"court/argument_audio-{term}.html", f"{COURT}/oral_arguments/argument_audio/{term}").decode("utf-8", "replace")
        except urllib.error.HTTPError:
            continue
        for path, docket, name, day in re.findall(
                r"href='\.\./audio/(\d{4}/[^']+)'>([^<]+)</a></span>\s*<span[^>]*>([^<]*)</span></td>\s*<td[^>]*>(\d\d/\d\d/\d\d)</td>", page):
            listed.append({"path": path, "docket": docket_form(docket.strip()), "name": html.unescape(name).strip(), "date": us_date(day)})
    rows = []
    for case in cases.values():
        for entry in listed:
            if entry["docket"] not in case["dockets"]:
                continue
            if case["argued"] and not any(near(entry["date"], day) for day in case["argued"]):
                continue
            page = cached(cache, f"court/{entry['path']}.html", f"{COURT}/oral_arguments/audio/{entry['path']}").decode("utf-8", "replace")
            files = sorted(set(re.findall(r"https://www\.supremecourt\.gov/media/audio/mp3files/[^'\"\s]+\.mp3", page)))
            for link in files:
                rows.append({"case": case["key"], "kind": "argument", "date": entry["date"].isoformat(), "source": "court",
                             "series": "", "catalog_id": "", "catalog_title": entry["name"], "segment": entry["name"],
                             "whole_session": "no", "date_note": "", "link": link, "bytes": "", "status": "listed"})
    return rows


# ---------------------------------------------------------------- the National Archives' recordings


def archive_descriptions(cache: Path) -> list[dict]:
    """The sound recording descriptions in Record Group 267, read from the catalog dataset (kept in the cache, recordings only)."""
    kept = cache / "rg267-sound-recordings.json"
    if kept.exists():
        return json.loads(kept.read_text(encoding="utf-8"))
    keys, token = [], None
    while True:
        query = {"list-type": "2", "prefix": RG267}
        if token:
            query["continuation-token"] = token
        listing = get(DATASET + "?" + urllib.parse.urlencode(query)).decode()
        keys += re.findall(r"<Key>([^<]+\.jsonl)</Key>", listing)
        found = re.search(r"<NextContinuationToken>([^<]+)", listing)
        if not found:
            break
        token = html.unescape(found.group(1))

    def recordings(key: str) -> list[dict]:
        out = []
        for line in get(DATASET + key).decode("utf-8").split("\n"):
            if not line.strip():
                continue
            record = json.loads(line)["record"]
            if "Sound Recordings" not in (record.get("generalRecordsTypes") or []) or not record.get("digitalObjects"):
                continue
            series = [a["title"] for a in record.get("ancestors", []) if a.get("levelOfDescription") == "series"]
            out.append({"naId": record["naId"], "title": record["title"], "series": series[0] if series else "",
                        "dates": [d["logicalDate"] for d in record.get("productionDates") or []],
                        "files": [{"link": o["objectUrl"], "name": o.get("objectFilename", ""), "bytes": o.get("objectFileSize")}
                                  for o in record["digitalObjects"] if "Sound" in (o.get("objectType") or "")]})
        return out

    descriptions = []
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        for found in pool.map(recordings, keys):
            descriptions += found
    descriptions.sort(key=lambda item: item["naId"])
    kept.write_text(json.dumps(descriptions, indent=0), encoding="utf-8")
    return descriptions


def title_parts(title: str) -> list[tuple]:
    """Each part of a recording's title: argument or opinion announcement, its words, its docket numbers, and the day it names, if any."""
    mode, parts = "argument", []
    # Parts are split at " / " (or "] /"), never inside a date such as "Opinion (1/21/1997)".
    for part in re.split(r"\s+/\s*|(?<=\])\s*/\s*", title):
        heading = re.match(r"\s*(OPINI?ONS?|JUDGE?MENTS?|ARGUMENTS?|REARGUMENTS?)\s*:\s*(.*)", part, re.I)
        if heading:
            mode = "argument" if "ARGUMENT" in heading.group(1).upper() else "opinion"
            part = heading.group(2)
        dockets = set()
        for inside in re.findall(r"\[(?:Cases?|No\.?)?\s*([^\]]+)\]", part):
            for one in re.split(r",|&|\band\b", inside):
                if docket_form(one):
                    dockets.add(docket_form(one))
        # A part that begins "Opinions [Case ...]" with no colon is an announcement, without changing the parts after it.
        alone = "opinion" if re.match(r"\s*(Opinions?|Judge?ments?)\b", part) else mode
        stated = re.search(r"\((\d{1,2}/\d{1,2}/\d{4})\)", part)
        parts.append((alone, part.strip(), dockets, us_date(stated.group(1)) if stated else None))
    return parts


def archive_rows(cases: dict, cache: Path) -> list[dict]:
    rows = []
    for item in archive_descriptions(cache):
        if not item["dates"]:
            continue
        made = dt.date.fromisoformat(item["dates"][0])
        black = "Black" in item["series"]
        for case in cases.values():
            short = set()
            for docket in case["dockets"]:
                # The Black Series titles give the docket number without the term, as "[Case 78]" for 70-78.
                found = re.fullmatch(r"\d\d-(\d+)|(\d+)orig", docket)
                if found:
                    short.add(found.group(1) or found.group(2))
            for mode, words, dockets, stated in title_parts(item["title"]):
                hit = case["dockets"] & dockets or (black and short & dockets)
                if not hit:
                    continue
                unique = any(re.fullmatch(r"\d\d-\d+", d) for d in case["dockets"] & dockets)
                decided = case["decided"]
                kind = note = None
                if decided and near(made, decided) and (mode == "opinion" or not any(near(made, d) for d in case["argued"])):
                    kind = "opinion announcement"
                elif mode == "opinion" and decided and stated and near(stated, decided):
                    # A later reel can hold an earlier announcement: "Opinion (1/21/1997) [Case 95-1595]".
                    kind = "opinion announcement"
                    note = f"The announcement of {stated.isoformat()} is on a reel the National Archives dates {made.isoformat()}."
                elif mode == "argument" and any(near(made, d) for d in case["argued"]):
                    kind = "argument"
                elif mode == "argument" and not case["argued"] and decided and 0 <= (decided - made).days <= 365:
                    kind = "argument"
                elif mode == "argument" and unique:
                    kind = "argument"
                    note = f"The National Archives dates the recording {made.isoformat()}; the case was argued on " + \
                           (", ".join(d.isoformat() for d in case["argued"]) or "a date not recorded") + "."
                if not kind:
                    continue
                files = item["files"]
                if black:
                    # One file per case: 267-<tape>-<docket>-<date>[-1of2].mp3
                    files = [f for f in files if (re.match(r"267-\d+-([^-]+)-", f["name"]) or [None, None])[1] in (short | case["dockets"])] or files
                fixed = MISFILED.get(str(item["naId"]), {})
                for one in files:
                    name = one["link"].rsplit("/", 1)[1]
                    if name in fixed:
                        one = {**one, "link": one["link"][: -len(name)] + fixed[name], "bytes": None}
                    rows.append({"case": case["key"], "kind": kind, "date": made.isoformat(), "source": "archives",
                                 "series": item["series"], "catalog_id": str(item["naId"]), "catalog_title": item["title"],
                                 "segment": words, "whole_session": "no" if black else "yes", "date_note": note or "",
                                 "link": media_link(one["link"]), "bytes": str(one["bytes"] or ""), "status": "listed"})
                break
    return rows


# ---------------------------------------------------------------- subcommands


def read_plan() -> list[dict]:
    if not PLAN.exists():
        return []
    with PLAN.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_plan(rows: list[dict]) -> None:
    rows.sort(key=lambda r: (r["case"], r["date"], r["source"], r["link"]))
    with PLAN.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def match(args) -> None:
    cache = Path(args.cache).expanduser()
    cases = our_cases(cache)
    old = {(r["case"], r["link"]): r for r in read_plan()}
    rows = court_rows(cases, cache) + archive_rows(cases, cache)
    seen = set()
    unique_rows = []
    for row in rows:
        if (row["case"], row["link"], row["kind"]) in seen:
            continue
        seen.add((row["case"], row["link"], row["kind"]))
        before = old.get((row["case"], row["link"]))
        if before:
            row["status"], row["bytes"] = before["status"], before["bytes"] or row["bytes"]
            row["duration_seconds"] = before.get("duration_seconds", "")
        unique_rows.append(row)
    write_plan(unique_rows)
    found = {r["case"] for r in unique_rows if r["kind"] == "argument"}
    print(f"{len(cases)} cases; {len(found)} with an argument recording; {len(unique_rows)} files listed")
    for key in sorted(set(cases) - found):
        print(f"  no argument recording found: {key} {sorted(cases[key]['dockets'])} argued {[d.isoformat() for d in cases[key]['argued']]}")


def storage_key(row: dict) -> str:
    name = urllib.parse.unquote(row["link"].rsplit("/", 1)[1])
    if row["source"] == "court":
        return f"{TOPIC}/court/{term_of(dt.date.fromisoformat(row['date']))}/{name}"
    series = row["series"].split(" from the ")[-1].lower().replace(" ", "-") if "from the" in row["series"] else "gold-series"
    return f"{TOPIC}/national-archives/{series}/{name.lower()}"


def fetch(args) -> None:
    sys.path.insert(0, str(ROOT / "scripts" / "captures"))
    import mirror_captures  # noqa: E402

    work = Path(args.work).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    rows = read_plan()
    index = mirror_captures.load_index()
    filed = {row["key"]: row for row in index}
    s3 = mirror_captures.client()
    bucket = mirror_captures.bucket_name()
    todo = {}
    for row in rows:
        if args.source and row["source"] != args.source:
            continue
        key = storage_key(row)
        if key in filed and row.get("duration_seconds"):
            row["status"] = "stored"
            continue
        todo.setdefault(key, []).append(row)

    def one(item):
        key, same = item
        row = same[0]
        path = work / key.replace("/", "_")
        try:
            size, digest = download(row["link"], path)
        except urllib.error.HTTPError as error:
            return key, same, None, f"refused ({error.code})"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            path.unlink(missing_ok=True)
            return key, same, None, f"failed ({type(error).__name__})"
        if mirror_captures.remote_state(s3, bucket, key) != (size, digest):
            s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": "audio/mpeg",
                                                              "Metadata": {"sha256": digest, "captured": dt.date.today().isoformat()}})
        ok = mirror_captures.remote_state(s3, bucket, key) == (size, digest)
        seconds = duration(path)
        path.unlink()
        print(f"{'stored' if ok else 'upload failed'}: {key} ({size:,} bytes)", flush=True)
        return key, same, (size, digest, seconds) if ok else None, "stored" if ok else "upload failed"

    stored = 0
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        for key, same, result, status in pool.map(one, todo.items()):
            for row in same:
                row["status"] = status
            if not result:
                print(f"{status}: {same[0]['link']}", file=sys.stderr)
                continue
            size, digest, seconds = result
            for row in same:
                row["bytes"], row["duration_seconds"] = str(size), str(seconds or "")
            row = same[0]
            if key in filed:
                continue
            what = "argument" if row["kind"] == "argument" else "opinion announcement"
            title = (f"Supreme Court {what} recording, {row['date']}: {row['catalog_title']}"
                     if row["source"] == "court" else
                     f"National Archives, {row['series']}, catalog {row['catalog_id']} ({row['date']}): {row['catalog_title']}")
            index.append(mirror_captures.index_row(
                key=key, size=size, digest=digest, media="audio/mpeg", topic=TOPIC, title=title[:300], nations="",
                obtained=("downloaded from the Supreme Court's argument audio pages (supremecourt.gov) by script" if row["source"] == "court"
                          else "downloaded from the National Archives Catalog (Record Group 267) by script"),
                links=[row["link"]] + ([CATALOG + row["catalog_id"]] if row["catalog_id"] else []),
                record=";".join(sorted({r["case"] for r in same})), redistribution="yes", rights=RIGHTS,
                existing_keys=set(filed)))
            filed[key] = index[-1]
            stored += 1
            if stored % 10 == 0:
                mirror_captures.write_index(index)
                write_plan(rows)
    mirror_captures.write_index(index)
    write_plan(rows)
    print(f"stored {stored}; " + ", ".join(f"{n} {s}" for s, n in collections.Counter(r["status"] for r in rows).items()))


def block_yaml(entries: list[dict]) -> str:
    lines = ["argument_audio:"]
    for entry in entries:
        lines.append(f"- kind: {entry['kind']}")
        lines.append(f"  date: '{entry['date']}'")
        lines.append(f"  source: {entry['source']}")
        lines.append(f"  link: {entry['link']}")
        if entry.get("catalog"):
            lines.append(f"  catalog: {entry['catalog']}")
        if entry.get("whole_session"):
            lines.append("  whole_session: true")
        if entry.get("note"):
            lines.append(f"  note: {json.dumps(entry['note'], ensure_ascii=False)}")
        if entry.get("duration_seconds"):
            lines.append(f"  duration_seconds: {entry['duration_seconds']}")
        if entry.get("file"):
            f = entry["file"]
            lines.append("  file:")
            lines.append(f"    storage_key: {f['storage_key']}")
            lines.append(f"    checksum: {f['checksum']}")
            lines.append(f"    media_type: {f['media_type']}")
            lines.append(f"    bytes: {f['bytes']}")
    return "\n".join(lines) + "\n"


def records(args) -> None:
    sys.path.insert(0, str(ROOT / "scripts" / "captures"))
    import mirror_captures  # noqa: E402

    index = {row["key"]: row for row in mirror_captures.load_index()}
    by_case = collections.defaultdict(list)
    for row in read_plan():
        key = storage_key(row)
        stored = index.get(key)
        entry = {"kind": row["kind"], "date": row["date"],
                 "source": "Supreme Court of the United States" if row["source"] == "court" else "National Archives",
                 "link": row["link"], "catalog": CATALOG + row["catalog_id"] if row["catalog_id"] else "",
                 "whole_session": row["whole_session"] == "yes", "note": row["date_note"],
                 "duration_seconds": int(row["duration_seconds"]) if row.get("duration_seconds") else None}
        if stored:
            entry["file"] = {"storage_key": key, "checksum": stored["sha256"], "media_type": stored["media_type"], "bytes": stored["bytes"]}
        by_case[row["case"]].append(entry)
    written = 0
    for path in sorted(SOURCES.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        key = re.search(r"(?m)^key: (\S+)", text)
        if not key or key.group(1) not in by_case:
            continue
        entries = sorted(by_case[key.group(1)], key=lambda e: (e["date"], e["source"] != "Supreme Court of the United States", e["link"]))
        text = re.sub(r"(?m)^argument_audio:\n(?:[- ] .*\n)*", "", text)
        anchor = "\nrights:\n" if "\nrights:\n" in text else None
        block = block_yaml(entries)
        text = text.replace(anchor, "\n" + block + "rights:\n", 1) if anchor else text.rstrip("\n") + "\n" + block
        path.write_text(text, encoding="utf-8")
        written += 1
    print(f"wrote argument_audio for {written} records")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("match")
    p.add_argument("--cache", required=True)
    p.set_defaults(run=match)
    p = sub.add_parser("fetch")
    p.add_argument("--cache", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--source", choices=["court", "archives"])
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(run=fetch)
    p = sub.add_parser("records")
    p.set_defaults(run=records)
    args = parser.parse_args()
    args.run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
