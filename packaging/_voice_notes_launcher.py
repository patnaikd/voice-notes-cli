"""Launch editable installs even when macOS hides their .pth path file.

Hatch includes this module directly in site-packages for editable and regular
installs. Python skips UF_HIDDEN .pth files, which can recur in this checkout.
The fallback uses installed package metadata rather than the working directory.
"""
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit


def main():
    if importlib.util.find_spec("voice_notes") is None:
        distribution = importlib.metadata.distribution("voice-notes-cli")
        metadata = json.loads(distribution.read_text("direct_url.json") or "{}")
        url = urlsplit(metadata.get("url", ""))
        if metadata.get("dir_info", {}).get("editable") and url.scheme == "file" and not url.netloc:
            source = Path(unquote(url.path)) / "src"
            if (source / "voice_notes" / "__init__.py").is_file():
                sys.path.insert(0, str(source))
    from voice_notes.cli import main as cli_main
    return cli_main()
