import io

from voice_notes.meter import AudioMeter, level_db


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_meter_distinguishes_silence_from_missing_frames(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    meter = AudioMeter(["mic", "system"], stream=Terminal())
    assert meter.render() == "Mic [............] no audio | Speaker [............] no audio"
    meter.update("mic", 0, 0)
    assert "Mic [............] silent" in meter.render()
    assert "Speaker [............] no audio" in meter.render()
    received = meter.levels["mic"][2]
    assert "Mic [............] no audio" in meter.render(now=received + 2)


def test_meter_shows_volume_and_clipping_independently(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    meter = AudioMeter(["mic", "system"], stream=Terminal())
    meter.update("mic", 0.1, 0.2)
    meter.update("system", 0.5, 1)
    rendered = meter.render()
    assert "Mic [########....]  -20 dB" in rendered
    assert "Speaker [###########.] CLIP" in rendered


def test_redirected_output_contains_no_terminal_control_sequences():
    stream = io.StringIO()
    meter = AudioMeter(["mic"], stream=stream)
    meter.start()
    meter.update("mic", 0.1, 0.2)
    meter.print("Transcript line", stream=stream)
    meter.close()
    assert stream.getvalue() == "Transcript line\n"


def test_decibels_are_bounded_for_silence_and_invalid_samples():
    assert level_db(0) == -60
    assert level_db(float("nan")) == -60
    assert level_db(2) == 0
