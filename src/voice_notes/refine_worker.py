"""Isolated Agent SDK request; the parent enforces the startup deadline."""
import asyncio
import json
import sys

from .refine import _sdk_ask


def main():
    try:
        request = json.load(sys.stdin)
        notes = asyncio.run(_sdk_ask(request["text"], request["model"]))
        print(json.dumps({"notes": notes}), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({"error": str(error) or type(error).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
