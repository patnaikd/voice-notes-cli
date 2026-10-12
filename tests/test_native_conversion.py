"""Exercise the actual Swift converter with a USB-style discrete channel layout."""
import platform
import shutil
import subprocess
from pathlib import Path


def test_discrete_microphone_channel_survives_mono_conversion(tmp_path):
    if platform.system() != "Darwin" or not shutil.which("xcrun"):
        import pytest
        pytest.skip("Requires the macOS Swift SDK")
    capture = (Path(__file__).parents[1] / "src/voice_notes/native/Capture.swift").read_text()
    converter = capture[capture.index("final class Converter {"):capture.index("final class AppleTrack {")]
    source = tmp_path / "ConversionCheck.swift"
    source.write_text('import AVFoundation\nimport AudioToolbox\nimport Foundation\n'
                      'struct Failure: Error { let message: String }\n' + converter + r'''
let layout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | 2)!
let inputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000,
                               interleaved: false, channelLayout: layout)
let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000,
                          channels: 1, interleaved: false)!
let input = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: 4800)!
input.frameLength = 4800
for frame in 0..<4800 {
    input.floatChannelData![0][frame] = Float(sin(Double(frame) * 2 * Double.pi * 440 / 48000) * 0.25)
    input.floatChannelData![1][frame] = 0
}
let output = try Converter(target: target).convert(input)
precondition(output.frameLength > 0, "No converted frames")
var sum: Double = 0
for frame in 0..<Int(output.frameLength) {
    let sample = Double(output.floatChannelData![0][frame])
    sum += sample * sample
}
let rms = sqrt(sum / Double(output.frameLength))
precondition(rms > 0.1, "Microphone signal was lost during mono conversion")
print("PASS")
''')
    executable = tmp_path / "conversion-check"
    compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5",
                               "-module-cache-path", str(tmp_path / "module-cache"),
                               str(source), "-o", str(executable)],
                              capture_output=True, text=True, timeout=120)
    assert compiled.returncode == 0, compiled.stderr
    checked = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == "PASS"
