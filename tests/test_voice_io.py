"""voice_io: delivery-metrics math (fully offline) + injected STT/TTS adapters
(real audio hardware/models can't run in this sandbox — see module docstring)."""
import pytest

import voice_io as vio


def test_delivery_metrics_clean_answer():
    text = "I led the product roadmap and we improved signup conversion by twelve percent."
    words = [{"word": w, "start": i * 0.4, "end": i * 0.4 + 0.35} for i, w in enumerate(text.split())]
    m = vio.compute_delivery_metrics(text, duration_s=len(words) * 0.4, words=words)
    assert m["wpm"] and m["filler_count"] == 0 and m["has_timing_data"]


def test_delivery_metrics_filler_and_hedge_detection():
    text = "Um, so basically I think, like, maybe we improved it, I guess, sort of, you know."
    m = vio.compute_delivery_metrics(text, duration_s=10, words=[])
    assert m["filler_count"] >= 4
    assert m["hedge_count"] >= 3
    assert not m["has_timing_data"]
    assert m["longest_pause_s"] is None  # no fabricated pause data without real timestamps


def test_delivery_metrics_zero_duration_degrades_not_crashes():
    m = vio.compute_delivery_metrics("some words here", duration_s=0, words=[])
    assert m["wpm"] is None


def test_delivery_metrics_empty_text():
    m = vio.compute_delivery_metrics("", duration_s=5, words=[])
    assert m["word_count"] == 0 and m["wpm"] is None


def test_transcribe_empty_audio_raises():
    with pytest.raises(vio.VoiceError):
        vio.transcribe(b"", engine="local_whisper")


def test_transcribe_unknown_engine_raises():
    with pytest.raises(vio.VoiceError):
        vio.transcribe(b"x", engine="not_a_real_engine")


def test_transcribe_local_with_injected_whisper():
    class FakeWord:
        def __init__(self, word, start, end):
            self.word, self.start, self.end = word, start, end

    class FakeSeg:
        def __init__(self, text, words):
            self.text, self.words = text, words

    class FakeInfo:
        duration = 3.2

    def fake_whisper(audio_bytes):
        segs = [FakeSeg("I led the roadmap.",
                       [FakeWord("I", 0, 0.1), FakeWord("led", 0.1, 0.3),
                        FakeWord("the", 0.3, 0.4), FakeWord("roadmap.", 0.4, 0.9)])]
        return segs, FakeInfo()

    result = vio.transcribe(b"fake-bytes", engine="local_whisper", whisper_fn=fake_whisper)
    assert result["text"] == "I led the roadmap."
    assert len(result["words"]) == 4


def test_transcribe_api_with_injected_http():
    def fake_http(url, audio_bytes, api_key):
        assert "audio/transcriptions" in url and api_key == "sk-test"
        return {"text": "hello from the api", "duration": 2.0,
               "words": [{"word": "hello", "start": 0, "end": 0.3}]}

    result = vio.transcribe(b"x", engine="openai_api", api_key="sk-test", http_post_fn=fake_http)
    assert result["text"] == "hello from the api"


def test_transcribe_api_without_key_raises():
    with pytest.raises(vio.VoiceError):
        vio.transcribe(b"x", engine="openai_api", api_key=None)


def test_synthesize_empty_text_raises():
    with pytest.raises(vio.VoiceError):
        vio.synthesize("", engine="local_pyttsx3")


def test_synthesize_unknown_engine_raises():
    with pytest.raises(vio.VoiceError):
        vio.synthesize("hi", engine="not_a_real_engine")


def test_synthesize_local_with_injected_tts():
    def fake_tts(text, voice, rate):
        return b"FAKE_WAV_" + text.encode()

    audio = vio.synthesize("Hello candidate", engine="local_pyttsx3", tts_fn=fake_tts)
    assert audio.startswith(b"FAKE_WAV_")


def test_synthesize_api_with_injected_http():
    def fake_http(url, text, voice, api_key):
        assert "audio/speech" in url
        return b"FAKE_MP3_BYTES"

    audio = vio.synthesize("Hello", engine="openai_api", api_key="sk-test", http_post_fn=fake_http)
    assert audio == b"FAKE_MP3_BYTES"


def test_synthesize_api_without_key_raises():
    with pytest.raises(vio.VoiceError):
        vio.synthesize("hi", engine="openai_api", api_key=None)


def test_local_engine_status_never_raises_and_reports_shape():
    """Must be safe to call on every render of the setup screen — a plain
    import-availability check, never a model load."""
    status = vio.local_engine_status()
    assert set(status.keys()) == {"local_whisper", "local_pyttsx3"}
    for k in status:
        assert isinstance(status[k]["available"], bool)
        assert isinstance(status[k]["error"], str)
