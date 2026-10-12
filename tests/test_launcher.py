"""Exercise the installed launcher's fallback without any .pth processing."""
import json
from pathlib import Path
import shutil
import subprocess
import sys


def test_editable_launcher_works_without_import_path_file(tmp_path):
    checkout = tmp_path / "Checkout with spaces and ’"
    package = checkout / "src" / "voice_notes"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text('def main():\n    print("editable source imported")\n    return 0\n')
    installed = tmp_path / "site-packages"
    metadata = installed / "voice_notes_cli-0.1.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: voice-notes-cli\nVersion: 0.1.0\n")
    (metadata / "direct_url.json").write_text(json.dumps({
        "url": checkout.as_uri(), "dir_info": {"editable": True},
    }))
    launcher = Path(__file__).parents[1] / "packaging" / "_voice_notes_launcher.py"
    shutil.copyfile(launcher, installed / launcher.name)
    script = "import sys; sys.path.insert(0, sys.argv[1]); import _voice_notes_launcher; sys.exit(_voice_notes_launcher.main())"
    # -I -S ensures neither the current directory nor .pth files add the source.
    result = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(installed)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "editable source imported"
