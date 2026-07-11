"""voice_io — speech-to-text, text-to-speech, and delivery-metrics computation.

Local-first, same "two brains" cost split as the rest of Meridian: matching
(here, delivery metrics) is always local and free; only the interviewer's
reasoning costs tokens. Voice I/O sits in the middle — local by default, API
optional, chosen the same way LLM providers are (Settings, not code).

Hard boundary: raw audio never leaves the laptop unless the user explicitly
selects an API STT/TTS provider. Local engines are lazy-imported so the rest
of the app works even if faster-whisper/pyttsx3 aren't installed — matching
the sentence-transformers-optional pattern already used in score.py.

Nothing in this module can be exercised end-to-end in a sandbox with no audio
hardware. Every function accepts an injectable engine/fetcher for offline
testing, exactly like apply_prep.fetch_schema and llm.chat. Real audio must be
verified on the user's machine (see HOW_TO_RUN.md).
"""

import io
import os
import re
import wave

UA = {"User-Agent": "Meridian personal job search"}
TIMEOUT = 60

_FILLER_WORDS = ("um", "uh", "umm", "uhh", "erm", "hmm", "like", "basically",
                 "actually", "literally", "you know", "sort of", "kind of")
_HEDGE_PHRASES = ("i guess", "maybe", "i think", "probably", "i suppose",
                  "not really sure", "i'm not sure", "kind of", "sort of")


# --------------------------------------------------------------- registry ---
STT_ENGINES = {
    "local_whisper": {"label": "Local (faster-whisper) — free, offline", "kind": "local"},
    "openai_api":    {"label": "OpenAI transcription API — metered", "kind": "api"},
}
TTS_ENGINES = {
    "local_pyttsx3": {"label": "Local system voice (pyttsx3) — free, offline", "kind": "local"},
    "openai_api":    {"label": "OpenAI speech API — metered, higher quality", "kind": "api"},
}


def local_engine_status():
    """Check whether the local STT/TTS packages are actually importable —
    WITHOUT loading a model or touching audio hardware, so this is cheap
    enough to call on every render of the setup screen. Lets the UI warn
    upfront (before a session starts) instead of only failing mid-interview,
    which is confusing and wastes a turn."""
    stt_ok = tts_ok = True
    stt_err = tts_err = ""
    try:
        import faster_whisper  # noqa: F401
    except ImportError as e:
        stt_ok, stt_err = False, str(e)
    try:
        import pyttsx3  # noqa: F401
    except ImportError as e:
        tts_ok, tts_err = False, str(e)
    return {"local_whisper": {"available": stt_ok, "error": stt_err},
           "local_pyttsx3": {"available": tts_ok, "error": tts_err}}


class VoiceError(Exception):
    """Raised on unrecoverable STT/TTS failure. Callers should degrade to text
    mode rather than crash the session — voice is an enhancement, not a
    single point of failure for the interview itself."""


# ------------------------------------------------------------------- STT ---
def transcribe(audio_bytes, engine="local_whisper", api_key=None, base_url=None,
               model=None, whisper_fn=None, http_post_fn=None, model_size="base"):
    """Return {"text": str, "duration_s": float, "words": [{"word","start","end"}]}.
    `words` may be empty if the engine doesn't provide timestamps — callers
    must degrade gracefully (see compute_delivery_metrics). Injectable
    `whisper_fn`/`http_post_fn` make this fully testable without real audio.
    """
    if not audio_bytes:
        raise VoiceError("no audio captured")

    if engine == "local_whisper":
        return _transcribe_local(audio_bytes, whisper_fn=whisper_fn, model_size=model_size)
    if engine == "openai_api":
        return _transcribe_api(audio_bytes, api_key, base_url, http_post_fn=http_post_fn)
    raise VoiceError(f"unknown STT engine '{engine}'")


def _wav_duration_seconds(audio_bytes):
    """Best-effort duration from WAV header; 0.0 if not a parseable WAV (e.g.
    webm from a browser fallback recorder) — callers must handle 0.0."""
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def _transcribe_local(audio_bytes, whisper_fn=None, model_size="base"):
    """Local faster-whisper. Lazy-imported so the app runs without it installed.
    `whisper_fn` is an injectable stand-in for `faster_whisper.WhisperModel(...).transcribe`
    with the same (segments, info) return shape, for offline testing.

    Speed choices (matter a lot on a no-GPU laptop):
    - beam_size=1 (greedy): 2-5x faster than faster-whisper's default beam of 5,
      with only a minor accuracy cost — the right trade for interview answers,
      where the user reviews/edits the transcript before submitting anyway.
    - vad_filter=True: skips silent stretches instead of decoding them.
    - model_size defaults to 'base' (~2-3x faster than 'small' on CPU); user-
      selectable in Settings for those who prefer accuracy over latency."""
    duration = _wav_duration_seconds(audio_bytes)
    if whisper_fn is None:
        try:
            from faster_whisper import WhisperModel  # noqa: F401
        except ImportError as e:
            raise VoiceError(
                "faster-whisper isn't installed. Run: pip install faster-whisper "
                "--break-system-packages") from e
        model = _get_local_whisper_model(model_size)
        segments, info = model.transcribe(io.BytesIO(audio_bytes), word_timestamps=True,
                                          beam_size=1, vad_filter=True)
    else:
        segments, info = whisper_fn(audio_bytes)

    words, text_parts = [], []
    for seg in segments:
        text_parts.append(seg.text.strip())
        for w in (getattr(seg, "words", None) or []):
            words.append({"word": w.word.strip(), "start": w.start, "end": w.end})
    return {"text": " ".join(text_parts).strip(),
           "duration_s": duration or (info.duration if hasattr(info, "duration") else 0.0),
           "words": words}


_LOCAL_WHISPER_MODELS = {}
_WHISPER_LOCK = None

def _get_whisper_lock():
    global _WHISPER_LOCK
    if _WHISPER_LOCK is None:
        import threading
        _WHISPER_LOCK = threading.Lock()
    return _WHISPER_LOCK

def _get_local_whisper_model(model_size="base"):
    """Per-size model cache, guarded by a lock so a background prewarm thread
    and a foreground transcription can never double-load the same model."""
    with _get_whisper_lock():
        if model_size not in _LOCAL_WHISPER_MODELS:
            from faster_whisper import WhisperModel
            _LOCAL_WHISPER_MODELS[model_size] = WhisperModel(
                model_size, device="cpu", compute_type="int8")
        return _LOCAL_WHISPER_MODELS[model_size]


def prewarm_local_whisper(model_size="base"):
    """Best-effort: load the model NOW (e.g. in a background thread the moment
    a voice session starts) so the user's FIRST answer doesn't pay the 5-15s
    model-load cost on top of transcription. Never raises — returns True if
    the model is ready, False if faster-whisper isn't installed."""
    try:
        _get_local_whisper_model(model_size)
        return True
    except Exception:
        return False


def _transcribe_api(audio_bytes, api_key, base_url, http_post_fn=None):
    """OpenAI-compatible transcription endpoint (batch, file upload — no
    streaming needed since we already have a complete clip from st.audio_input)."""
    if not api_key:
        raise VoiceError("no API key configured for API speech-to-text")
    url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/audio/transcriptions"
    duration = _wav_duration_seconds(audio_bytes)

    if http_post_fn is not None:
        data = http_post_fn(url, audio_bytes, api_key)
    else:
        import requests
        files = {"file": ("answer.wav", audio_bytes, "audio/wav")}
        data_form = {"model": "whisper-1", "response_format": "verbose_json",
                    "timestamp_granularities[]": "word"}
        r = requests.post(url, headers={"Authorization": f"Bearer {api_key}"},
                          files=files, data=data_form, timeout=TIMEOUT)
        if r.status_code != 200:
            raise VoiceError(f"transcription API error {r.status_code}: {r.text[:300]}")
        data = r.json()

    words = [{"word": w.get("word", "").strip(), "start": w.get("start", 0), "end": w.get("end", 0)}
            for w in (data.get("words") or [])]
    return {"text": (data.get("text") or "").strip(),
           "duration_s": data.get("duration", duration) or duration, "words": words}


# ------------------------------------------------------------------- TTS ---
def synthesize(text, engine="local_pyttsx3", voice=None, rate=175, api_key=None,
              base_url=None, tts_fn=None, http_post_fn=None):
    """Return raw audio bytes (WAV for local, whatever the API returns for API
    engines — both are playable via st.audio, which sniffs the format)."""
    if not text or not text.strip():
        raise VoiceError("no text to synthesize")

    if engine == "local_pyttsx3":
        return _synthesize_local(text, voice=voice, rate=rate, tts_fn=tts_fn)
    if engine == "openai_api":
        return _synthesize_api(text, voice, api_key, base_url, http_post_fn=http_post_fn)
    raise VoiceError(f"unknown TTS engine '{engine}'")


def _synthesize_local(text, voice=None, rate=175, tts_fn=None):
    """Local pyttsx3 (Windows SAPI5 / macOS NSSpeechSynthesizer / Linux espeak).
    Always returns bytes via save_to_file, never plays directly on the server
    process — decouples synthesis from playback so the SAME st.audio() call
    path works for both local and API engines."""
    if tts_fn is not None:
        return tts_fn(text, voice, rate)
    try:
        import pyttsx3
    except ImportError as e:
        raise VoiceError("pyttsx3 isn't installed. Run: pip install pyttsx3 "
                         "--break-system-packages") from e
    import tempfile
    engine = pyttsx3.init()
    try:
        engine.setProperty("rate", rate)
        if voice:
            for v in engine.getProperty("voices"):
                if voice.lower() in (v.name or "").lower():
                    engine.setProperty("voice", v.id)
                    break
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        engine.save_to_file(text, path)
        engine.runAndWait()
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
        engine.stop()


def _synthesize_api(text, voice, api_key, base_url, http_post_fn=None):
    if not api_key:
        raise VoiceError("no API key configured for API text-to-speech")
    url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/audio/speech"
    if http_post_fn is not None:
        return http_post_fn(url, text, voice, api_key)
    import requests
    payload = {"model": "tts-1", "voice": voice or "alloy", "input": text}
    r = requests.post(url, headers={"Authorization": f"Bearer {api_key}",
                                     "Content-Type": "application/json"},
                      json=payload, timeout=TIMEOUT)
    if r.status_code != 200:
        raise VoiceError(f"speech API error {r.status_code}: {r.text[:300]}")
    return r.content


# --------------------------------------------------------- delivery metrics ---
def compute_delivery_metrics(text, duration_s, words=None):
    """Local, zero-cost signals a transcript alone misses: pace, fillers,
    hedging, pauses. Degrades gracefully when word timestamps aren't
    available (duration_s=0 or words=[]) rather than fabricating numbers."""
    words = words or []
    tokens = re.findall(r"[a-zA-Z']+", text.lower())
    n_words = len(tokens)

    wpm = None
    if duration_s and duration_s > 0 and n_words:
        wpm = round(n_words / (duration_s / 60.0), 1)

    filler_count = 0
    text_l = " " + text.lower() + " "
    for f in _FILLER_WORDS:
        filler_count += len(re.findall(r"\b" + re.escape(f) + r"\b", text_l))
    filler_rate = round(filler_count / n_words, 3) if n_words else None

    hedge_count = sum(1 for h in _HEDGE_PHRASES if h in text_l)

    pause_gaps = []
    for a, b in zip(words, words[1:]):
        gap = b.get("start", 0) - a.get("end", 0)
        if gap and gap > 0:
            pause_gaps.append(gap)
    longest_pause = round(max(pause_gaps), 2) if pause_gaps else None
    avg_pause = round(sum(pause_gaps) / len(pause_gaps), 2) if pause_gaps else None

    return {
        "word_count": n_words, "duration_s": round(duration_s, 1) if duration_s else None,
        "wpm": wpm, "filler_count": filler_count, "filler_rate": filler_rate,
        "hedge_count": hedge_count, "longest_pause_s": longest_pause, "avg_pause_s": avg_pause,
        "has_timing_data": bool(words),
    }
