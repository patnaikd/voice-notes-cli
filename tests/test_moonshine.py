import pytest

np = pytest.importorskip("numpy")
from voice_notes.moonshine import Timeline


class Sink:
    def __init__(self):
        self.audio = []

    def add_audio(self, samples, rate):
        self.audio.extend(samples)


def test_late_remote_audio_keeps_meeting_time():
    timeline, sink = Timeline(), Sink()
    timeline.feed(np.ones(10), start=2, rate=100, transcriber=sink)
    assert sink.audio[:200] == [0] * 200
    assert sink.audio[200:] == [1] * 10
    assert timeline.samples == 210


def test_overlapping_chunks_do_not_duplicate_audio():
    timeline, sink = Timeline(), Sink()
    timeline.feed(np.ones(10), start=0, rate=100, transcriber=sink)
    timeline.feed(np.ones(10) * 2, start=0.05, rate=100, transcriber=sink)
    assert sink.audio == [1] * 10 + [2] * 5
