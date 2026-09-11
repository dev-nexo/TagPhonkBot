
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1, TPE2


def get_tags(path: str | Path) -> ID3:
    try:
        return ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
        tags.save(path, v2_version=3)
        return ID3(path)


def text(tags: ID3, frame: str) -> str:
    obj = tags.get(frame)
    if not obj or not hasattr(obj, "text"):
        return ""
    return " / ".join(str(x) for x in obj.text).strip()


def snapshot(path: str | Path) -> dict[str, Any]:
    tags = get_tags(path)
    return {
        "title": text(tags, "TIT2"),
        "artist": text(tags, "TPE1"),
        "album": text(tags, "TALB"),
        "albumartist": text(tags, "TPE2"),
        "year": text(tags, "TDRC"),
        "genre": text(tags, "TCON"),
        "track": text(tags, "TRCK"),
        "disc": text(tags, "TPOS"),
        "composer": text(tags, "TCOM"),
        "bpm": text(tags, "TBPM"),
        "publisher": text(tags, "TPUB"),
        "copyright": text(tags, "TCOP"),
        "isrc": text(tags, "TSRC"),
        "cover": bool(tags.getall("APIC")),
    }


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    keys = sorted(set(before) | set(after))
    result = []
    for key in keys:
        if before.get(key) != after.get(key):
            result.append({"field": key, "before": before.get(key), "after": after.get(key)})
    return result


def normalize_dirty_text(value: str) -> str:
    value = value.replace("_", " ")
    value = re.sub(r"\[[^\]]*(free download|www\.|https?://)[^\]]*\]", "", value, flags=re.I)
    value = re.sub(r"\((official (audio|video)|lyrics?|visuali[sz]er)\)", "", value, flags=re.I)
    value = re.sub(r"\b(320\s*kbps|256\s*kbps|192\s*kbps)\b", "", value, flags=re.I)
    value = re.sub(r"\s{2,}", " ", value).strip(" .-_")
    words = []
    for word in value.split():
        if len(word) > 3 and word.isupper():
            words.append(word.title())
        else:
            words.append(word)
    return " ".join(words)


def clean_from_filename(path: str | Path, original_name: str | None = None) -> dict[str, str]:
    raw = Path(original_name or path).stem
    raw = normalize_dirty_text(raw)
    if " - " in raw:
        artist, title = raw.split(" - ", 1)
        return {"artist": normalize_dirty_text(artist), "title": normalize_dirty_text(title)}
    return {"title": raw}


def _set_text(tags: ID3, frame: str, cls, value: str) -> None:
    tags.delall(frame)
    if value:
        tags.add(cls(encoding=3, text=[value]))


def apply_builtin_preset(path: str | Path, preset: str) -> dict[str, Any]:
    tags = get_tags(path)
    before = snapshot(path)

    if preset in {"clean", "dj", "phonk"}:
        for key in list(tags.keys()):
            prefix = key.split(":", 1)[0]
            if prefix in {
                "COMM", "WXXX", "TENC", "TSSE", "PRIV", "GEOB", "UFID", "AENC", "ENCR",
                "GRID", "OWNE", "RBUF", "RVA2", "SIGN", "SYTC",
            }:
                del tags[key]

    if preset == "phonk":
        if not text(tags, "TCON"):
            _set_text(tags, "TCON", TCON, "Phonk")

    if preset == "minimal":
        keep = {"TIT2", "TPE1", "TALB", "TPE2", "TDRC", "TCON", "TRCK", "APIC"}
        for key in list(tags.keys()):
            if key.split(":", 1)[0] not in keep:
                del tags[key]

    tags.save(path, v2_version=3)
    after = snapshot(path)
    return {"before": before, "after": after, "changes": diff(before, after)}


def apply_name_cleanup(path: str | Path, original_name: str | None = None) -> dict[str, Any]:
    tags = get_tags(path)
    before = snapshot(path)
    guessed = clean_from_filename(path, original_name)

    if guessed.get("artist"):
        _set_text(tags, "TPE1", TPE1, guessed["artist"])
    if guessed.get("title"):
        _set_text(tags, "TIT2", TIT2, guessed["title"])

    tags.save(path, v2_version=3)
    after = snapshot(path)
    return {"before": before, "after": after, "changes": diff(before, after)}


def export_json_bytes(path: str | Path) -> bytes:
    data = snapshot(path)
    data["filename"] = Path(path).name
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def export_csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO()
    fields = [
        "filename", "artist", "title", "album", "albumartist", "year", "genre",
        "track", "disc", "composer", "bpm", "publisher", "copyright", "isrc",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue().encode("utf-8-sig")


def copy_id3_tags(source: str | Path, destination: str | Path) -> None:
    src = get_tags(source)
    dst = get_tags(destination)
    dst.clear()
    for frame in src.values():
        dst.add(frame)
    dst.save(destination, v2_version=3)
