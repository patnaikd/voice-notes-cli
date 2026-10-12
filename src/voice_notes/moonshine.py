"""Two independent live recognizers fed timestamped PCM from ScreenCaptureKit."""
import base64
from dataclasses import asdict


def line_event(line, source: str) -> dict:
    return {"type": "segment" if line.is_complete else "partial",
            "id": str(line.line_id), "source": source, "start": float(line.start_time),
            "end": float(line.start_time + line.duration), "text": line.text,
            "speakers": [asdict(span) for span in line.speaker_spans or []]}


class Timeline:
    """Preserve meeting offsets when a source is silent or capture drops frames."""
    def __init__(self):
        self.samples = 0

    def feed(self, samples, start: float, rate: int, transcriber):
        import numpy as np

        target = round(start * rate)
        gap = target - self.samples
        # Fill gaps in bounded pieces; jitter under 20 ms is normal.
        if gap > rate // 50:
            while gap > 0:
                count = min(gap, rate)
                transcriber.add_audio(np.zeros(count, dtype=np.float32), rate)
                self.samples += count
                gap -= count
        overlap = self.samples - target
        if overlap > rate // 50:
            samples = samples[min(overlap, len(samples)):]
        if len(samples):
            transcriber.add_audio(samples, rate)
            self.samples += len(samples)


class Moonshine:
    def __init__(self, sources: list[str], language: str, speakers: bool, cache: object, emit):
        try:
            from moonshine_voice import Transcriber, get_model_for_language, get_diarization_model
            from moonshine_voice.transcriber import TranscriptEventListener
        except ImportError as error:
            raise RuntimeError('Install Moonshine: uv sync --extra moonshine') from error

        self.transcribers, self.timelines = {}, {}
        model, architecture = get_model_for_language(language, cache_root=cache,
                                                     include_word_timestamps=speakers)
        diarization = get_diarization_model(cache_root=cache) if speakers else None

        class Listener(TranscriptEventListener):
            def __init__(self, source):
                self.source = source

            def on_line_text_changed(self, event):
                emit(line_event(event.line, self.source))

            def on_line_completed(self, event):
                emit(line_event(event.line, self.source))

            def on_line_speakers_changed(self, event):
                # A completed line may get new speaker labels minutes later.
                emit(line_event(event.line, self.source))

            def on_error(self, event):
                raise RuntimeError(str(event.error))

        try:
            for source in sources:
                options = {"return_audio_data": "false"}
                if speakers and source == "system":
                    options.update(identify_speakers="true", diarization_model_dir=diarization)
                transcriber = Transcriber(model, architecture, options=options)
                self.transcribers[source] = transcriber
                transcriber.add_listener(Listener(source))
                transcriber.start()
                self.timelines[source] = Timeline()
        except BaseException:
            self.close()
            raise

    def audio(self, event: dict):
        import numpy as np

        samples = np.frombuffer(base64.b64decode(event["pcm"], validate=True), dtype="<f4")
        self.timelines[event["source"]].feed(samples, event["start"], event["sample_rate"],
                                            self.transcribers[event["source"]])

    def close(self):
        errors = []
        for transcriber in self.transcribers.values():
            try:
                transcriber.stop()
            except Exception as error:
                errors.append(error)
            finally:
                transcriber.close()
        self.transcribers.clear()
        if errors:
            raise RuntimeError(f"Moonshine finalization failed: {errors[0]}")
