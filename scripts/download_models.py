"""Fetch model weights into ./models.

Weights are data, not code: a checkpoint baked into a container image makes
every deploy slow and every model swap a rebuild (View 2). The image carries no
weights; this script populates a directory that gets mounted in read-only.

    python scripts/download_models.py            # fetch what is missing
    python scripts/download_models.py --verify   # re-check digests, no fetch
    python scripts/download_models.py --force    # re-download everything

Run it once on the host before `make up`. Total ~600MB:

    u2net                    176MB   matting        (via rembg)
    segformer_b2_clothes     ~110MB  segmentation   (pinned commit)
    marqo_fashionsiglip      ~370MB  embeddings     (pinned commit, vision only)
    vit_nsfw_detector        ~340MB  moderation     (pinned commit, runs in-VPC)

Every non-rembg model is pinned to a commit sha rather than a branch, so a
fetch is reproducible even if the upstream repo's main branch moves. Digests
are printed so they can be pasted into the registry, after which a mismatch is
a hard failure rather than a shrug.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "models"

# The runtime resolves weights from MODELS_ROOT (/models in the container).
# Point it at the host directory so registry paths line up when running the
# script and the service on the same machine.
os.environ.setdefault("MODELS_ROOT", str(MODELS_DIR))
sys.path[:0] = [str(REPO_ROOT / "packages"), str(REPO_ROOT / "services")]

from stylist_ml import registry  # noqa: E402 - after sys.path is set up

CHUNK = 1024 * 256


def human(n: int) -> str:
    return f"{n / 1_048_576:.0f}MB"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(spec: registry.ModelSpec, *, force: bool) -> bool:
    target = spec.path
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_file() and not force:
        print(f"  {spec.name}: present ({human(target.stat().st_size)})")
        return True

    # Carriage-return progress is unreadable in a log file — it renders as one
    # enormous line. Only animate on a terminal.
    show_progress = sys.stdout.isatty()

    print(f"  {spec.name}: downloading {spec.remote_file} @ {spec.revision[:12]}…")
    # Download to a temporary name and rename on success. A partial file left
    # at the real path would be indistinguishable from a good one on the next
    # run, and onnxruntime's error for a truncated model is not obviously a
    # download problem.
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        request = urllib.request.Request(spec.url, headers={"User-Agent": "ai-stylist-model-fetch"})
        with urllib.request.urlopen(request, timeout=120) as resp, tmp.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(CHUNK):
                out.write(chunk)
                done += len(chunk)
                if total and show_progress:
                    pct = done * 100 // total
                    print(f"\r    {pct:3d}%  {human(done)}/{human(total)}", end="", flush=True)
            if show_progress:
                print()
            else:
                print(f"    {human(done)} downloaded")
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        print(f"    HTTP {exc.code} fetching {spec.url}", file=sys.stderr)
        return False
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print(f"    failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False

    tmp.replace(target)
    print(f"    saved {target.relative_to(REPO_ROOT)} ({human(target.stat().st_size)})")
    return True


def fetch_u2net(*, force: bool) -> bool:
    """rembg owns u2net's cache layout, so let it do the download.

    It nests its own models/<name>/ beneath U2NET_HOME and the layout has
    changed between releases, which is why the runtime globs for the file
    rather than assuming a path.
    """
    home = MODELS_DIR / "u2net"
    home.mkdir(parents=True, exist_ok=True)
    existing = sorted(home.rglob("u2net.onnx"))
    if existing and not force:
        print(
            f"  u2net: present ({human(existing[0].stat().st_size)}) at "
            f"{existing[0].relative_to(REPO_ROOT)}"
        )
        return True

    os.environ["U2NET_HOME"] = str(home)
    print("  u2net: downloading via rembg (~176MB)…")
    try:
        from rembg import new_session

        new_session("u2net")
    except ImportError:
        print(
            "    rembg is not installed. Install the ml extra:\n      pip install -e '.[dev,ml]'",
            file=sys.stderr,
        )
        return False

    found = sorted(home.rglob("u2net.onnx"))
    if not found:
        print(f"    no u2net.onnx under {home} after the download", file=sys.stderr)
        return False
    print(f"    saved {found[0].relative_to(REPO_ROOT)} ({human(found[0].stat().st_size)})")
    return True


def verify(spec: registry.ModelSpec) -> bool:
    """Compare the file's digest against the registry's pin."""
    if spec.task == "matte":
        found = sorted((MODELS_DIR / "u2net").rglob("u2net.onnx"))
        path = found[0] if found else None
    else:
        path = spec.path if spec.path.is_file() else None

    if path is None:
        print(f"  {spec.name}: MISSING")
        return False

    digest = sha256_of(path)
    if spec.sha256 is None:
        print(f"  {spec.name}: sha256 {digest}")
        print(f'      ^ not yet pinned. Paste into registry.py as sha256="{digest}"')
        return True
    if digest != spec.sha256:
        print(f"  {spec.name}: DIGEST MISMATCH")
        print(f"      registry: {spec.sha256}")
        print(f"      on disk : {digest}")
        return False
    print(f"  {spec.name}: sha256 verified ({digest[:16]}…)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="check digests, do not download")
    ap.add_argument("--force", action="store_true", help="re-download even if present")
    args = ap.parse_args()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    if args.verify:
        print("verifying model weights:")
        ok = all(verify(spec) for spec in registry.ALL_MODELS)
        return 0 if ok else 1

    print("fetching model weights into ./models:")
    ok = fetch_u2net(force=args.force)
    for spec in (registry.SEGFORMER, registry.FASHION_SIGLIP, registry.NSFW):
        ok = fetch(spec, force=args.force) and ok

    print("\ndigests:")
    for spec in registry.ALL_MODELS:
        verify(spec)

    if not ok:
        print("\nsome models failed to download.", file=sys.stderr)
        return 1
    print("\nall models present. `make up` will mount ./models read-only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
