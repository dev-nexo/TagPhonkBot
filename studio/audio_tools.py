
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
from static_ffmpeg import run as static_ffmpeg_run

from .config import config
from .tags_extra import copy_id3_tags

logger = logging.getLogger("tagphonk.studio.audio")
_audio_semaphore = asyncio.Semaphore(2)
_ffmpeg_paths: tuple[str, str] | None = None


def _ensure_binaries_sync() -> tuple[str, str]:
    global _ffmpeg_paths
    if _ffmpeg_paths is None:
        _ffmpeg_paths = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
    return _ffmpeg_paths


async def warm_ffmpeg() -> None:
    try:
        await asyncio.to_thread(_ensure_binaries_sync)
        logger.info("Studio FFmpeg is ready.")
    except Exception:
        logger.exception("FFmpeg warm-up failed; audio tools will retry lazily.")


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


async def probe(path: str) -> dict[str, Any]:
    async with _audio_semaphore:
        ffmpeg, ffprobe = await asyncio.to_thread(_ensure_binaries_sync)
        proc = await asyncio.to_thread(
            _run,
            [
                ffprobe, "-v", "error",
                "-show_entries",
                "format=duration,size,bit_rate,format_name:stream=index,codec_type,codec_name,sample_rate,channels,channel_layout,bit_rate",
                "-of", "json",
                path,
            ],
            40,
        )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-1500:])
    return json.loads(proc.stdout.decode("utf-8", "replace"))


async def analyze(path: str) -> dict[str, Any]:
    info = await probe(path)
    audio_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = info.get("format", {})
    bitrate = int(audio_stream.get("bit_rate") or fmt.get("bit_rate") or 0)
    sample_rate = int(audio_stream.get("sample_rate") or 0)
    channels = int(audio_stream.get("channels") or 0)
    duration = float(fmt.get("duration") or 0)
    codec = audio_stream.get("codec_name") or "unknown"

    ffmpeg, _ = await asyncio.to_thread(_ensure_binaries_sync)
    async with _audio_semaphore:
        vol = await asyncio.to_thread(
            _run,
            [ffmpeg, "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
            90,
        )
    stderr = vol.stderr.decode("utf-8", "replace")
    mean_match = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", stderr)
    max_match = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", stderr)
    mean_volume = float(mean_match.group(1)) if mean_match else None
    max_volume = float(max_match.group(1)) if max_match else None

    score = 100
    notes: list[str] = []
    kbps = round(bitrate / 1000) if bitrate else 0

    if kbps and kbps < 128:
        score -= 35
        notes.append("низкий битрейт")
    elif kbps and kbps < 192:
        score -= 20
        notes.append("средний битрейт")
    elif kbps and kbps < 256:
        score -= 8

    if sample_rate and sample_rate < 44100:
        score -= 15
        notes.append("sample rate ниже 44.1 kHz")

    if max_volume is not None and max_volume > -0.1:
        score -= 10
        notes.append("пик очень близко к 0 dBFS")

    if duration <= 0:
        score -= 20

    score = max(0, min(100, score))
    return {
        "codec": codec,
        "duration": round(duration, 2),
        "bitrate_kbps": kbps,
        "sample_rate": sample_rate,
        "channels": channels,
        "mean_volume_db": mean_volume,
        "max_volume_db": max_volume,
        "quality_score": score,
        "notes": notes,
        "disclaimer": "Quality Score — эвристика по техническим параметрам, не доказательство исходного качества.",
    }


async def waveform(path: str, output: str) -> str:
    ffmpeg, _ = await asyncio.to_thread(_ensure_binaries_sync)
    async with _audio_semaphore:
        proc = await asyncio.to_thread(
            _run,
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", path,
                "-filter_complex", "showwavespic=s=1200x320:colors=white",
                "-frames:v", "1",
                output,
            ],
            90,
        )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-1500:])
    return output


async def _sample_rate(path: str) -> int:
    info = await probe(path)
    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    try:
        return int(stream.get("sample_rate") or 44100)
    except Exception:
        return 44100


async def transform_mp3(path: str, output: str, preset: str) -> str:
    ffmpeg, _ = await asyncio.to_thread(_ensure_binaries_sync)
    sr = await _sample_rate(path)

    filters = {
        "slowed90": f"asetrate={sr}*0.90,aresample={sr}",
        "super80": f"asetrate={sr}*0.80,aresample={sr}",
        "sped115": f"asetrate={sr}*1.15,aresample={sr}",
        "bass": "equalizer=f=80:t=q:w=1:g=7,equalizer=f=160:t=q:w=1:g=3",
        "reverb": "aecho=0.8:0.88:75|140:0.25|0.16",
        "normalize": "loudnorm=I=-14:TP=-1.5:LRA=11",
        "phonk": f"asetrate={sr}*0.90,aresample={sr},equalizer=f=80:t=q:w=1:g=6,aecho=0.8:0.88:70:0.20",
    }
    if preset not in filters:
        raise ValueError("Unknown audio preset")

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-vn",
        "-af", filters[preset],
        "-c:a", "libmp3lame",
        "-b:a", "320k",
        output,
    ]

    async with _audio_semaphore:
        proc = await asyncio.to_thread(_run, cmd, 180)

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-1800:])

    await asyncio.to_thread(copy_id3_tags, path, output)
    return output


async def preview_30(path: str, output: str) -> str:
    ffmpeg, _ = await asyncio.to_thread(_ensure_binaries_sync)
    async with _audio_semaphore:
        proc = await asyncio.to_thread(
            _run,
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", path, "-t", "30", "-vn",
                "-c:a", "libmp3lame", "-b:a", "192k",
                output,
            ],
            120,
        )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-1500:])
    await asyncio.to_thread(copy_id3_tags, path, output)
    return output


async def convert(path: str, output: str, fmt: str) -> str:
    ffmpeg, _ = await asyncio.to_thread(_ensure_binaries_sync)
    codecs = {
        "flac": ["-c:a", "flac"],
        "ogg": ["-c:a", "libvorbis", "-q:a", "6"],
        "m4a": ["-c:a", "aac", "-b:a", "256k"],
        "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
    }
    if fmt not in codecs:
        raise ValueError("Unsupported conversion format")

    async with _audio_semaphore:
        proc = await asyncio.to_thread(
            _run,
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", path, "-vn", *codecs[fmt], output],
            180,
        )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-1800:])
    return output


async def acoustid_lookup(path: str) -> dict[str, Any]:
    if not config.acoustid_api_key:
        return {
            "enabled": False,
            "reason": "ACOUSTID_API_KEY is not configured.",
        }

    ffmpeg, _ = await asyncio.to_thread(_ensure_binaries_sync)
    async with _audio_semaphore:
        proc = await asyncio.to_thread(
            _run,
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", path,
                "-f", "chromaprint",
                "-fp_format", "base64",
                "-",
            ],
            120,
        )
    if proc.returncode != 0:
        return {
            "enabled": False,
            "reason": "This FFmpeg build does not provide Chromaprint fingerprint output.",
        }

    fingerprint = proc.stdout.decode("utf-8", "replace").strip()
    if not fingerprint:
        return {"enabled": False, "reason": "Fingerprint is empty."}

    info = await probe(path)
    duration = int(float(info.get("format", {}).get("duration") or 0))
    data = {
        "client": config.acoustid_api_key,
        "duration": str(duration),
        "fingerprint": fingerprint,
        "meta": "recordings+releasegroups+compress",
        "format": "json",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post("https://api.acoustid.org/v2/lookup", data=data)
        response.raise_for_status()
        payload = response.json()

    return {"enabled": True, "result": payload}
