
from __future__ import annotations

import asyncio
import math
import os
import random
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import wave
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from .audio_tools import _ensure_binaries_sync, probe
from .config import config
from .security import verify_telegram_init_data

router = APIRouter()
BOT_TOKEN = ""

PROJECT_TTL = 3 * 60 * 60
MAX_TRACKS = 8
MAX_CLIPS = 24
MAX_CLIP_BYTES = 20 * 1024 * 1024
MAX_PROJECT_BYTES = 80 * 1024 * 1024
MAX_RENDER_SECONDS = 10 * 60

PROJECTS: dict[str, dict[str, Any]] = {}
RENDER_SEMAPHORE = asyncio.Semaphore(1)

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

DEFAULT_SEQUENCE = {
    "kick": [0] * 16,
    "snare": [0] * 16,
    "hat": [0] * 16,
    "cow": [0] * 16,
}


def _auth(init_data: str | None) -> dict[str, Any]:
    return verify_telegram_init_data(init_data or "", BOT_TOKEN)


def _uid(init_data: str | None) -> int:
    return int(_auth(init_data)["id"])


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _track(name: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:10],
        "name": name,
        "volume": 1.0,
        "pan": 0.0,
        "mute": False,
        "solo": False,
        "fx": {
            "lowpass": 20000.0,
            "highpass": 20.0,
            "bass": 0.0,
            "reverb": 0.0,
            "delay": 0.0,
        },
    }


def _new_project(user_id: int, name: str = "Untitled") -> dict[str, Any]:
    pid = uuid.uuid4().hex
    folder = Path(tempfile.mkdtemp(prefix=f"tagphonk_daw_{user_id}_"))
    now = time.time()
    project = {
        "id": pid,
        "user_id": user_id,
        "name": (name or "Untitled")[:80],
        "bpm": 130.0,
        "master_gain": 1.0,
        "normalize": True,
        "sequence_gain": 0.72,
        "sequence_enabled": False,
        "dir": str(folder),
        "total_bytes": 0,
        "tracks": [_track("Track 1")],
        "clips": [],
        "sequence": {k: list(v) for k, v in DEFAULT_SEQUENCE.items()},
        "created_at": now,
        "updated_at": now,
    }
    PROJECTS[pid] = project
    return project


def _cleanup() -> None:
    now = time.time()
    for pid, project in list(PROJECTS.items()):
        if now - float(project.get("updated_at", now)) > PROJECT_TTL:
            shutil.rmtree(project["dir"], ignore_errors=True)
            PROJECTS.pop(pid, None)


def _project(project_id: str, user_id: int) -> dict[str, Any]:
    _cleanup()
    project = PROJECTS.get(project_id)
    if not project or int(project["user_id"]) != int(user_id):
        raise HTTPException(status_code=404, detail="DAW-проект не найден.")
    project["updated_at"] = time.time()
    return project


def _public(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": project["id"],
        "name": project["name"],
        "bpm": project["bpm"],
        "master_gain": project["master_gain"],
        "normalize": project["normalize"],
        "sequence_gain": project["sequence_gain"],
        "sequence_enabled": project["sequence_enabled"],
        "tracks": project["tracks"],
        "clips": [
            {
                k: clip[k]
                for k in (
                    "id", "track_id", "filename", "duration", "start",
                    "trim_start", "trim_end", "gain", "rate", "fade_in", "fade_out", "size_bytes",
                )
            }
            for clip in project["clips"]
        ],
        "sequence": project["sequence"],
        "total_bytes": project["total_bytes"],
        "limits": {
            "tracks": MAX_TRACKS,
            "clips": MAX_CLIPS,
            "project_bytes": MAX_PROJECT_BYTES,
        },
        "updated_at": project["updated_at"],
    }


def _sanitize_sequence(value: Any) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    incoming = value if isinstance(value, dict) else {}
    for name in ("kick", "snare", "hat", "cow"):
        row = incoming.get(name, DEFAULT_SEQUENCE[name])
        if not isinstance(row, list):
            row = DEFAULT_SEQUENCE[name]
        result[name] = [1 if bool(x) else 0 for x in (row + [0] * 16)[:16]]
    return result


def _apply_patch(project: dict[str, Any], payload: dict[str, Any]) -> None:
    if "name" in payload:
        project["name"] = str(payload.get("name") or "Untitled").strip()[:80]
    if "bpm" in payload:
        project["bpm"] = _clamp(payload["bpm"], 50, 220, project["bpm"])
    if "master_gain" in payload:
        project["master_gain"] = _clamp(payload["master_gain"], 0, 2.0, project["master_gain"])
    if "sequence_gain" in payload:
        project["sequence_gain"] = _clamp(payload["sequence_gain"], 0, 1.5, project["sequence_gain"])
    if "normalize" in payload:
        project["normalize"] = bool(payload["normalize"])
    if "sequence_enabled" in payload:
        project["sequence_enabled"] = bool(payload["sequence_enabled"])
    if "sequence" in payload:
        project["sequence"] = _sanitize_sequence(payload["sequence"])

    incoming_tracks = payload.get("tracks")
    if isinstance(incoming_tracks, list):
        existing = {t["id"]: t for t in project["tracks"]}
        reordered = []
        for raw in incoming_tracks[:MAX_TRACKS]:
            if not isinstance(raw, dict):
                continue
            tid = str(raw.get("id") or "")
            track = existing.get(tid)
            if not track:
                continue
            track["name"] = str(raw.get("name") or track["name"]).strip()[:40]
            track["volume"] = _clamp(raw.get("volume"), 0, 2, track["volume"])
            track["pan"] = _clamp(raw.get("pan"), -1, 1, track["pan"])
            track["mute"] = bool(raw.get("mute", track["mute"]))
            track["solo"] = bool(raw.get("solo", track["solo"]))
            fx = raw.get("fx") if isinstance(raw.get("fx"), dict) else {}
            track["fx"]["lowpass"] = _clamp(fx.get("lowpass"), 400, 20000, track["fx"]["lowpass"])
            track["fx"]["highpass"] = _clamp(fx.get("highpass"), 20, 8000, track["fx"]["highpass"])
            track["fx"]["bass"] = _clamp(fx.get("bass"), -12, 12, track["fx"]["bass"])
            track["fx"]["reverb"] = _clamp(fx.get("reverb"), 0, 1, track["fx"]["reverb"])
            track["fx"]["delay"] = _clamp(fx.get("delay"), 0, 1, track["fx"]["delay"])
            reordered.append(track)
        if reordered:
            project["tracks"] = reordered

    incoming_clips = payload.get("clips")
    if isinstance(incoming_clips, list):
        existing = {c["id"]: c for c in project["clips"]}
        track_ids = {t["id"] for t in project["tracks"]}
        for raw in incoming_clips:
            if not isinstance(raw, dict):
                continue
            clip = existing.get(str(raw.get("id") or ""))
            if not clip:
                continue
            track_id = str(raw.get("track_id") or clip["track_id"])
            if track_id in track_ids:
                clip["track_id"] = track_id
            clip["start"] = _clamp(raw.get("start"), 0, MAX_RENDER_SECONDS, clip["start"])
            clip["gain"] = _clamp(raw.get("gain"), 0, 2, clip["gain"])
            clip["rate"] = _clamp(raw.get("rate"), 0.5, 2.0, clip.get("rate", 1.0))
            clip["fade_in"] = _clamp(raw.get("fade_in"), 0, 10, clip.get("fade_in", 0.0))
            clip["fade_out"] = _clamp(raw.get("fade_out"), 0, 10, clip.get("fade_out", 0.0))
            trim_start = _clamp(raw.get("trim_start"), 0, clip["duration"], clip["trim_start"])
            trim_end = _clamp(raw.get("trim_end"), trim_start + 0.03, clip["duration"], clip["trim_end"])
            clip["trim_start"] = trim_start
            clip["trim_end"] = trim_end

    project["updated_at"] = time.time()


def _project_duration(project: dict[str, Any]) -> float:
    longest = 0.0
    track_by_id = {t["id"]: t for t in project["tracks"]}
    any_solo = any(t["solo"] for t in project["tracks"])
    for clip in project["clips"]:
        track = track_by_id.get(clip["track_id"])
        if not track or track["mute"] or (any_solo and not track["solo"]):
            continue
        audible = max(0.03, clip["trim_end"] - clip["trim_start"]) / max(0.5, float(clip.get("rate", 1.0)))
        longest = max(longest, clip["start"] + audible)
    has_sequence = bool(project.get("sequence_enabled")) and any(any(row) for row in project["sequence"].values())
    bar = 60.0 / project["bpm"] * 4.0
    if has_sequence:
        longest = max(longest, bar * 4)
    return min(MAX_RENDER_SECONDS, max(longest, bar if has_sequence else 0.1))


def _synth_bar(project: dict[str, Any]) -> Path:
    sr = 44100
    bpm = float(project["bpm"])
    step = 60.0 / bpm / 4.0
    duration = step * 16
    n = max(1, int(duration * sr))
    mix = [0.0] * n
    rng = random.Random(1337)

    def add_kick(start: int):
        length = int(min(0.42, duration) * sr)
        phase = 0.0
        for i in range(length):
            idx = start + i
            if idx >= n:
                break
            t = i / sr
            freq = 145.0 * math.exp(-7.0 * t) + 42.0
            phase += 2 * math.pi * freq / sr
            env = math.exp(-9.5 * t)
            mix[idx] += math.sin(phase) * env * 0.95

    def add_snare(start: int):
        length = int(min(0.24, duration) * sr)
        for i in range(length):
            idx = start + i
            if idx >= n:
                break
            t = i / sr
            env = math.exp(-15.0 * t)
            noise = rng.uniform(-1.0, 1.0)
            tone = math.sin(2 * math.pi * 180 * t)
            mix[idx] += (noise * 0.78 + tone * 0.22) * env * 0.55

    def add_hat(start: int):
        length = int(min(0.09, duration) * sr)
        prev = 0.0
        for i in range(length):
            idx = start + i
            if idx >= n:
                break
            t = i / sr
            raw = rng.uniform(-1.0, 1.0)
            high = raw - prev * 0.92
            prev = raw
            mix[idx] += high * math.exp(-34.0 * t) * 0.22

    def add_cow(start: int):
        length = int(min(0.22, duration) * sr)
        for i in range(length):
            idx = start + i
            if idx >= n:
                break
            t = i / sr
            env = math.exp(-10.0 * t)
            sig = math.sin(2 * math.pi * 540 * t) + 0.65 * math.sin(2 * math.pi * 805 * t)
            mix[idx] += sig * env * 0.28

    synth = {"kick": add_kick, "snare": add_snare, "hat": add_hat, "cow": add_cow}
    for name, row in project["sequence"].items():
        fn = synth[name]
        for i, enabled in enumerate(row):
            if enabled:
                fn(int(i * step * sr))

    peak = max(1.0, max(abs(x) for x in mix))
    gain = 0.86 / peak
    path = Path(project["dir"]) / "sequence_bar.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        frames = bytearray()
        for sample in mix:
            value = int(max(-1.0, min(1.0, sample * gain)) * 32767)
            frames += struct.pack("<hh", value, value)
        wav.writeframes(frames)
    return path


def _render_sync(project: dict[str, Any], fmt: str) -> Path:
    ffmpeg, _ffprobe = _ensure_binaries_sync()
    duration = _project_duration(project)
    track_map = {t["id"]: t for t in project["tracks"]}
    any_solo = any(t["solo"] for t in project["tracks"])

    active_clips = []
    for clip in project["clips"]:
        track = track_map.get(clip["track_id"])
        if not track or track["mute"] or (any_solo and not track["solo"]):
            continue
        active_clips.append((clip, track))

    has_sequence = bool(project.get("sequence_enabled")) and any(any(row) for row in project["sequence"].values())
    if not active_clips and not has_sequence:
        raise ValueError("В проекте пока нечего рендерить.")

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for clip, _track_data in active_clips:
        cmd += ["-i", clip["path"]]

    seq_index = None
    if has_sequence:
        seq_path = _synth_bar(project)
        seq_index = len(active_clips)
        cmd += ["-stream_loop", "-1", "-i", str(seq_path)]

    filters: list[str] = []
    labels: list[str] = []

    for i, (clip, track) in enumerate(active_clips):
        trim_start = max(0.0, float(clip["trim_start"]))
        trim_end = max(trim_start + 0.03, float(clip["trim_end"]))
        start_ms = max(0, int(float(clip["start"]) * 1000))
        volume = float(clip["gain"]) * float(track["volume"])
        pan = float(track["pan"])
        left = volume * (1.0 - max(0.0, pan))
        right = volume * (1.0 + min(0.0, pan))

        rate = max(0.5, min(2.0, float(clip.get("rate", 1.0))))
        output_duration = max(0.03, (trim_end - trim_start) / rate)
        fade_in = max(0.0, min(float(clip.get("fade_in", 0.0)), output_duration / 2))
        fade_out = max(0.0, min(float(clip.get("fade_out", 0.0)), output_duration / 2))

        chain = [
            f"[{i}:a]atrim=start={trim_start:.4f}:end={trim_end:.4f}",
            "asetpts=PTS-STARTPTS",
            f"atempo={rate:.5f}",
            "aresample=44100",
            "aformat=sample_fmts=fltp:channel_layouts=stereo",
        ]
        if fade_in > 0.005:
            chain.append(f"afade=t=in:st=0:d={fade_in:.4f}")
        if fade_out > 0.005:
            chain.append(f"afade=t=out:st={max(0.0, output_duration - fade_out):.4f}:d={fade_out:.4f}")

        fx = track["fx"]
        if float(fx["highpass"]) > 21:
            chain.append(f"highpass=f={float(fx['highpass']):.1f}")
        if float(fx["lowpass"]) < 19950:
            chain.append(f"lowpass=f={float(fx['lowpass']):.1f}")
        if abs(float(fx["bass"])) >= 0.1:
            chain.append(f"equalizer=f=85:t=q:w=1:g={float(fx['bass']):.2f}")
        if float(fx["reverb"]) > 0.01:
            decay = 0.08 + float(fx["reverb"]) * 0.56
            chain.append(f"aecho=0.8:0.88:65:{decay:.3f}")
        if float(fx["delay"]) > 0.01:
            decay = 0.04 + float(fx["delay"]) * 0.42
            chain.append(f"aecho=0.8:0.75:180:{decay:.3f}")

        chain += [
            f"pan=stereo|c0={left:.6f}*c0|c1={right:.6f}*c1",
            f"adelay={start_ms}:all=1",
        ]
        label = f"a{i}"
        filters.append(",".join(chain) + f"[{label}]")
        labels.append(f"[{label}]")

    if seq_index is not None:
        label = "seq"
        filters.append(
            f"[{seq_index}:a]atrim=duration={duration:.4f},"
            "asetpts=PTS-STARTPTS,aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={float(project['sequence_gain']):.4f}[{label}]"
        )
        labels.append(f"[{label}]")

    mix_label = "mix"
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
          f"volume={float(project['master_gain']):.4f},alimiter=limit=0.97[{mix_label}]"
    )

    final_label = mix_label
    if project.get("normalize"):
        filters.append(
            f"[{mix_label}]loudnorm=I=-14:TP=-1.5:LRA=11[out]"
        )
        final_label = "out"

    out = Path(project["dir"]) / f"TagPhonk-mix.{fmt}"
    codecs = {
        "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
        "wav": ["-c:a", "pcm_s16le"],
        "flac": ["-c:a", "flac"],
        "m4a": ["-c:a", "aac", "-b:a", "256k"],
    }
    if fmt not in codecs:
        raise ValueError("Неподдерживаемый формат.")

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", f"[{final_label}]",
        "-t", f"{duration:.4f}",
        *codecs[fmt],
        str(out),
    ]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-2200:])
    return out


@router.post("/api/daw/projects")
async def create_project(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
):
    user_id = _uid(x_telegram_init_data)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return _public(_new_project(user_id, str(payload.get("name") or "Untitled")))


@router.get("/api/daw/projects/{project_id}")
async def get_project(
    project_id: str,
    x_telegram_init_data: str | None = Header(default=None),
):
    return _public(_project(project_id, _uid(x_telegram_init_data)))


@router.patch("/api/daw/projects/{project_id}")
async def patch_project(
    project_id: str,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
):
    project = _project(project_id, _uid(x_telegram_init_data))
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Некорректный проект.")
    _apply_patch(project, payload)
    return _public(project)


@router.post("/api/daw/projects/{project_id}/tracks")
async def add_track(
    project_id: str,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
):
    project = _project(project_id, _uid(x_telegram_init_data))
    if len(project["tracks"]) >= MAX_TRACKS:
        raise HTTPException(status_code=409, detail=f"Максимум {MAX_TRACKS} дорожек.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    project["tracks"].append(_track(str(payload.get("name") or f"Track {len(project['tracks']) + 1}")))
    project["updated_at"] = time.time()
    return _public(project)


@router.delete("/api/daw/projects/{project_id}/tracks/{track_id}")
async def delete_track(
    project_id: str,
    track_id: str,
    x_telegram_init_data: str | None = Header(default=None),
):
    project = _project(project_id, _uid(x_telegram_init_data))
    if len(project["tracks"]) <= 1:
        raise HTTPException(status_code=409, detail="Должна остаться хотя бы одна дорожка.")
    if any(c["track_id"] == track_id for c in project["clips"]):
        raise HTTPException(status_code=409, detail="Сначала удали клипы с дорожки.")
    before = len(project["tracks"])
    project["tracks"] = [t for t in project["tracks"] if t["id"] != track_id]
    if len(project["tracks"]) == before:
        raise HTTPException(status_code=404, detail="Дорожка не найдена.")
    return _public(project)


@router.post("/api/daw/projects/{project_id}/clips")
async def upload_clip(
    project_id: str,
    track_id: str = Query(...),
    file: UploadFile = File(...),
    x_telegram_init_data: str | None = Header(default=None),
):
    project = _project(project_id, _uid(x_telegram_init_data))
    if track_id not in {t["id"] for t in project["tracks"]}:
        raise HTTPException(status_code=404, detail="Дорожка не найдена.")
    if len(project["clips"]) >= MAX_CLIPS:
        raise HTTPException(status_code=409, detail=f"Максимум {MAX_CLIPS} клипа.")

    filename = file.filename or "audio.mp3"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Поддерживаются MP3, WAV, M4A, AAC, OGG и FLAC.")

    cid = uuid.uuid4().hex[:12]
    target = Path(project["dir"]) / f"{cid}{ext}"
    total = 0
    with target.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CLIP_BYTES:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Один клип — максимум 20 МБ.")
            if project["total_bytes"] + total > MAX_PROJECT_BYTES:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Проект превысил лимит 80 МБ.")
            out.write(chunk)

    try:
        info = await probe(str(target))
        duration = float(info.get("format", {}).get("duration") or 0)
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Не получилось прочитать аудиофайл.") from exc

    if duration <= 0 or duration > MAX_RENDER_SECONDS:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Некорректная или слишком длинная дорожка.")

    clip = {
        "id": cid,
        "track_id": track_id,
        "filename": filename[:120],
        "path": str(target),
        "duration": duration,
        "start": 0.0,
        "trim_start": 0.0,
        "trim_end": duration,
        "gain": 1.0,
        "rate": 1.0,
        "fade_in": 0.0,
        "fade_out": 0.0,
        "size_bytes": total,
    }
    project["clips"].append(clip)
    project["total_bytes"] += total
    project["updated_at"] = time.time()
    return _public(project)


@router.get("/api/daw/projects/{project_id}/clips/{clip_id}/audio")
async def clip_audio(
    project_id: str,
    clip_id: str,
    x_telegram_init_data: str | None = Header(default=None),
):
    project = _project(project_id, _uid(x_telegram_init_data))
    clip = next((c for c in project["clips"] if c["id"] == clip_id), None)
    if not clip:
        raise HTTPException(status_code=404, detail="Клип не найден.")
    return FileResponse(
        clip["path"],
        filename=clip["filename"],
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.delete("/api/daw/projects/{project_id}/clips/{clip_id}")
async def delete_clip(
    project_id: str,
    clip_id: str,
    x_telegram_init_data: str | None = Header(default=None),
):
    project = _project(project_id, _uid(x_telegram_init_data))
    keep = []
    removed = None
    for clip in project["clips"]:
        if clip["id"] == clip_id:
            removed = clip
        else:
            keep.append(clip)
    if not removed:
        raise HTTPException(status_code=404, detail="Клип не найден.")

    project["clips"] = keep
    if not any(c["path"] == removed["path"] for c in keep):
        Path(removed["path"]).unlink(missing_ok=True)
        project["total_bytes"] = max(0, project["total_bytes"] - int(removed["size_bytes"]))
    project["updated_at"] = time.time()
    return _public(project)


@router.post("/api/daw/projects/{project_id}/clips/{clip_id}/duplicate")
async def duplicate_clip(
    project_id: str,
    clip_id: str,
    x_telegram_init_data: str | None = Header(default=None),
):
    project = _project(project_id, _uid(x_telegram_init_data))
    if len(project["clips"]) >= MAX_CLIPS:
        raise HTTPException(status_code=409, detail=f"Максимум {MAX_CLIPS} клипа.")
    source = next((c for c in project["clips"] if c["id"] == clip_id), None)
    if not source:
        raise HTTPException(status_code=404, detail="Клип не найден.")
    clone = dict(source)
    clone["id"] = uuid.uuid4().hex[:12]
    audible = max(0.03, source["trim_end"] - source["trim_start"]) / max(0.5, float(source.get("rate", 1.0)))
    clone["start"] = min(MAX_RENDER_SECONDS, float(source["start"]) + audible)
    project["clips"].append(clone)
    project["updated_at"] = time.time()
    return _public(project)


@router.post("/api/daw/projects/{project_id}/clips/{clip_id}/split")
async def split_clip(
    project_id: str,
    clip_id: str,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
):
    project = _project(project_id, _uid(x_telegram_init_data))
    if len(project["clips"]) >= MAX_CLIPS:
        raise HTTPException(status_code=409, detail=f"Максимум {MAX_CLIPS} клипа.")
    clip = next((c for c in project["clips"] if c["id"] == clip_id), None)
    if not clip:
        raise HTTPException(status_code=404, detail="Клип не найден.")
    payload = await request.json()
    position = float(payload.get("position", 0))
    rate = max(0.5, float(clip.get("rate", 1.0)))
    visual_duration = (clip["trim_end"] - clip["trim_start"]) / rate
    local = position - float(clip["start"])
    if local <= 0.03 or local >= visual_duration - 0.03:
        raise HTTPException(status_code=409, detail="Поставь playhead внутрь клипа.")
    source_split = float(clip["trim_start"]) + local * rate

    second = dict(clip)
    second["id"] = uuid.uuid4().hex[:12]
    second["start"] = position
    second["trim_start"] = source_split
    second["fade_in"] = 0.0

    clip["trim_end"] = source_split
    clip["fade_out"] = 0.0

    project["clips"].append(second)
    project["updated_at"] = time.time()
    return _public(project)


@router.post("/api/daw/projects/{project_id}/render")
async def render_project(
    project_id: str,
    fmt: str = Query("mp3"),
    x_telegram_init_data: str | None = Header(default=None),
):
    project = _project(project_id, _uid(x_telegram_init_data))
    fmt = fmt.lower()
    if fmt not in {"mp3", "wav", "flac", "m4a"}:
        raise HTTPException(status_code=400, detail="Формат экспорта не поддерживается.")

    try:
        async with RENDER_SEMAPHORE:
            output = await asyncio.to_thread(_render_sync, project, fmt)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Рендер занял слишком много времени.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Рендер не удался: {str(exc)[-500:]}") from exc

    media = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "flac": "audio/flac",
        "m4a": "audio/mp4",
    }[fmt]
    safe = "".join(c for c in project["name"] if c not in '\\/:*?"<>|').strip() or "TagPhonk"
    return FileResponse(output, media_type=media, filename=f"{safe}.{fmt}")


@router.delete("/api/daw/projects/{project_id}")
async def delete_project(
    project_id: str,
    x_telegram_init_data: str | None = Header(default=None),
):
    project = _project(project_id, _uid(x_telegram_init_data))
    PROJECTS.pop(project_id, None)
    shutil.rmtree(project["dir"], ignore_errors=True)
    return {"ok": True}


def install_daw(app: FastAPI, bot_token: str) -> None:
    global BOT_TOKEN
    BOT_TOKEN = bot_token
    app.include_router(router)
