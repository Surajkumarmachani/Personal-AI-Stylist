"""Pre-fetch model weights into ./models.

Weights are data, not code: a 176MB checkpoint baked into a container image
makes every deploy slow and every model swap a rebuild (View 2). So the image
carries no weights and this script populates a directory that gets mounted in.

Run once on the host before `make up`:

    python scripts/download_models.py

Phase 3 extends this with SegFormer and FashionSigLIP ONNX exports.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
U2NET_DIR = MODELS_DIR / "u2net"


def main() -> int:
    U2NET_DIR.mkdir(parents=True, exist_ok=True)

    # rembg nests its own models/<name>/ beneath U2NET_HOME, so search rather
    # than assume a fixed path — the layout is a dependency's internal detail.
    existing = sorted(U2NET_DIR.rglob("u2net.onnx"))
    if existing:
        size_mb = existing[0].stat().st_size / 1_048_576
        print(f"already present: {existing[0]} ({size_mb:.0f}MB)")
        return 0

    # rembg resolves its model cache from U2NET_HOME, so point it at our
    # mount directory and let it do the fetch and checksum.
    os.environ["U2NET_HOME"] = str(U2NET_DIR)
    print(f"downloading u2net into {U2NET_DIR} (~176MB, one time)...")
    try:
        from rembg import new_session

        new_session("u2net")
    except ImportError:
        print(
            "rembg is not installed in this environment.\n"
            "  pip install -e '.[dev]'   (or: pip install 'rembg[cpu]')",
            file=sys.stderr,
        )
        return 1

    found = sorted(U2NET_DIR.rglob("u2net.onnx"))
    if not found:
        print(
            f"no u2net.onnx found under {U2NET_DIR} after the download",
            file=sys.stderr,
        )
        return 1
    print(f"done: {found[0]} ({found[0].stat().st_size / 1_048_576:.0f}MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
