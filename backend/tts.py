"""Kiosk speech: ElevenLabs text to speech with a file cache for fixed phrases (docs/voice.md, "Kiosk voice").

Every line the kiosk says is registered here first (register() -> clip id) and the kiosk fetches the audio from
GET /api/kiosk/tts/{clip_id}, so the kiosk can only ever play text the backend wrote: the route looks the text up
by id and never takes text from the browser.

- Fixed phrases (no digits: the tour, the offer, first visit greetings, the exit reminder) are generated once and
  kept as files under data/tts_cache/, named by a hash of voice, model, format and text. A cache hit never calls
  ElevenLabs.
- Dynamic lines (anything with a number: prices, totals, budgets) are generated on demand with a 4 s timeout and
  kept only in a small in-memory cache, so a repeated fetch does not pay twice.
- Any failure (feature off, no key or voice id, timeout, HTTP error) returns None; the kiosk then shows the line as
  a caption only. Nothing here ever raises into a caller.

The API key stays here; the browser only ever sees audio bytes.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from pathlib import Path

import httpx

from backend import db, eventlog
from backend.settings import settings

# https://elevenlabs.io/docs/api-reference/text-to-speech/convert
TTS_ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
# Flash v2.5: the lowest latency model ("~75ms"), https://elevenlabs.io/docs/models
MODEL_ID = "eleven_flash_v2_5"
OUTPUT_FORMAT = "mp3_44100_128"  # the endpoint's default, sent explicitly so the cache key stays honest
DYNAMIC_TIMEOUT_S = 4.0  # a dynamic line that takes longer is shown as a caption only
WARM_TIMEOUT_S = 15.0  # fixed phrases are generated in the background, so they may take longer
MAX_TEXT_CHARS = 400
MEMORY_CLIPS = 24  # dynamic audio kept in memory (a visit says a handful of these)
MAX_REGISTERED = 256  # clip id -> text; the oldest ids are forgotten first

_lock = threading.Lock()
_texts: OrderedDict[str, str] = OrderedDict()  # clip id -> text, for GET /api/kiosk/tts/{clip_id}
_memory: OrderedDict[str, bytes] = OrderedDict()  # clip id -> mp3 bytes of dynamic lines
_warming: set[str] = set()  # clip ids a background warm-up is generating right now


def _norm(text: str) -> str:
    return " ".join(str(text).split())[:MAX_TEXT_CHARS]


def cache_dir() -> Path:
    """data/tts_cache/ (follows db.DATA_DIR, so tests get their own temp folder)."""
    return db.DATA_DIR / "tts_cache"


def available() -> bool:
    """Speech needs the voice feature (ElevenLabs), an API key and a voice id. Captions work without any of it."""
    env = settings.env
    return bool(settings.features.voice and env.elevenlabs_api_key and env.elevenlabs_voice_id)


def is_fixed(text: str) -> bool:
    """Fixed phrases have no digits: no prices, totals or budgets. They are cached as files."""
    return not any(ch.isdigit() for ch in text)


def clip_id(text: str) -> str:
    """Stable id for this text in this voice, model and format (also the cache file name)."""
    key = "|".join((settings.env.elevenlabs_voice_id, MODEL_ID, OUTPUT_FORMAT, text))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def register(text: str) -> str:
    """Remember `text` under its clip id so the kiosk can fetch its audio. Returns the id."""
    text = _norm(text)
    cid = clip_id(text)
    with _lock:
        _texts[cid] = text
        _texts.move_to_end(cid)
        while len(_texts) > MAX_REGISTERED:
            _texts.popitem(last=False)
    return cid


def text_for(cid: str) -> str | None:
    with _lock:
        return _texts.get(cid)


def _cache_file(cid: str) -> Path:
    return cache_dir() / f"{cid}.mp3"


def cached(text: str) -> bytes | None:
    """Audio for `text` without calling ElevenLabs: the file cache for fixed phrases, memory for dynamic ones."""
    text = _norm(text)
    cid = clip_id(text)
    with _lock:
        if cid in _memory:
            _memory.move_to_end(cid)
            return _memory[cid]
    path = _cache_file(cid)
    if is_fixed(text) and path.exists():
        try:
            return path.read_bytes()
        except OSError:
            return None
    return None


def synthesize(text: str, timeout_s: float) -> bytes:
    """One ElevenLabs text to speech call. Raises on any HTTP, timeout or shape problem."""
    env = settings.env
    r = httpx.post(TTS_ENDPOINT.format(voice_id=env.elevenlabs_voice_id),
                   params={"output_format": OUTPUT_FORMAT},
                   headers={"xi-api-key": env.elevenlabs_api_key, "accept": "audio/mpeg"},
                   json={"text": text, "model_id": MODEL_ID},
                   timeout=timeout_s)
    r.raise_for_status()
    audio = r.content
    if not audio or not r.headers.get("content-type", "audio/mpeg").startswith("audio/"):
        raise ValueError("not_audio")
    return audio


def _store(text: str, audio: bytes) -> None:
    cid = clip_id(text)
    if is_fixed(text):
        folder = cache_dir()
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"{cid}.tmp"
        tmp.write_bytes(audio)
        tmp.replace(_cache_file(cid))
        return
    with _lock:
        _memory[cid] = audio
        _memory.move_to_end(cid)
        while len(_memory) > MEMORY_CLIPS:
            _memory.popitem(last=False)


def speak(text: str, timeout_s: float = DYNAMIC_TIMEOUT_S) -> bytes | None:
    """MP3 bytes for `text`, or None (caption only). Cache first, then one ElevenLabs call within timeout_s."""
    text = _norm(text)
    if not text:
        return None
    hit = cached(text)
    if hit is not None:
        return hit
    if not available():
        return None
    try:
        audio = synthesize(text, timeout_s)
    except httpx.HTTPStatusError as e:
        eventlog.log("tts_error", reason=f"http_{e.response.status_code}", fixed=is_fixed(text))
        return None
    except Exception as e:  # timeout, network, not audio
        eventlog.log("tts_error", reason=type(e).__name__, fixed=is_fixed(text))
        return None
    try:
        _store(text, audio)
    except OSError as e:  # a full disk must not cost us this clip
        eventlog.log("tts_cache_error", error=repr(e))
    eventlog.log("tts_generated", fixed=is_fixed(text), chars=len(text), bytes=len(audio))
    return audio


def warm(texts: list[str]) -> None:
    """Generate the fixed phrases that are not cached yet, in a background thread (one per phrase at a time)."""
    if not available():
        return
    todo = []
    for text in map(_norm, texts):
        if not text or not is_fixed(text) or cached(text) is not None:
            continue
        cid = clip_id(text)
        with _lock:
            if cid in _warming:
                continue
            _warming.add(cid)
        todo.append(text)
    if not todo:
        return

    def run() -> None:
        for text in todo:
            try:
                speak(text, timeout_s=WARM_TIMEOUT_S)
            finally:
                with _lock:
                    _warming.discard(clip_id(text))

    threading.Thread(target=run, name="tts-warm", daemon=True).start()


def reset() -> None:
    """Forget registered ids and in-memory audio (tests). Files on disk stay."""
    with _lock:
        _texts.clear()
        _memory.clear()
        _warming.clear()
