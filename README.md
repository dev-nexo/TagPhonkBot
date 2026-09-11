# TagPhonk Studio V4

TagPhonk is a Telegram-first MP3 metadata and audio toolbox.

## V4

V3 remains the stable ID3 editor. V4 adds a modular `studio/` layer:

- Telegram Mini App dashboard;
- persistent project metadata with Postgres support;
- recent project history;
- user settings and custom presets;
- compact behavior in group chats;
- ZIP batch import with path traversal / zip-bomb guards;
- audio analysis via FFprobe;
- waveform generation;
- Slowed / Super Slowed / Sped Up;
- Bass Boost, Reverb, loudness normalization;
- combined Phonk FX preset;
- 30-second preview;
- FLAC / M4A / OGG conversion;
- dirty filename cleanup;
- Clean Release / Phonk Release / DJ Library / Minimal presets;
- JSON and CSV metadata export;
- AcoustID fingerprint integration when `ACOUSTID_API_KEY` is configured;
- admin statistics endpoint / command;
- rate limiting for heavier Studio actions.

## Existing V3

The original V3 bot UI, MusicBrainz metadata lookup, cover lookup, ID3 editing,
undo, filename templates, batch mode and ZIP export remain available.

## Environment

Required:
- `BOT_TOKEN`
- `PUBLIC_URL`
- `WEBHOOK_SECRET`

Recommended:
- `DATABASE_URL` for persistent Studio history
- `REDIS_URL` for future shared cache / distributed queues
- `ADMIN_KEY`
- `ADMIN_USER_IDS`
- `ACOUSTID_API_KEY`

## FFmpeg

`static-ffmpeg` is used to obtain FFmpeg/FFprobe without requiring system-level package installation.
The binary is warmed in the background after startup.

## Safety

- Mini App requests validate Telegram `initData`.
- webhook continues to use Telegram secret-token validation from V3.
- ZIP imports reject traversal and suspicious compression ratios.
- heavy audio operations use a small concurrency semaphore.
- bot tokens are not logged by HTTPX at INFO level.
