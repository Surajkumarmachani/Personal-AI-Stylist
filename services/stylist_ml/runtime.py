"""ONNX Runtime session management.

One session per model per process, built at startup rather than on first
request. The lazy version cost 20 seconds on whichever user happened to upload
first — a real Phase 2 finding, and it gets worse here because there are now
three models to load rather than one.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass

import onnxruntime as ort

from stylist_ml.registry import ModelSpec

logger = logging.getLogger(__name__)


class ModelUnavailable(RuntimeError):  # noqa: N818 - a state, not an error type
    """Weights are not on disk. Readiness reports it; nothing crash-loops."""


@dataclass(slots=True)
class LoadedModel:
    spec: ModelSpec
    session: ort.InferenceSession
    sha256: str
    input_name: str
    output_names: tuple[str, ...]


def _session_options() -> ort.SessionOptions:
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    # Pin thread counts instead of letting onnxruntime guess.
    #
    # It sizes its pools from detected core count, and under a VM it can fail
    # to identify the CPU at all (OrbStack logs "Unknown CPU vendor") and pick
    # badly. More importantly, the default assumes it owns the machine: with
    # several ml pods per node, each spawning a thread per core, they oversubscribe
    # and every request gets slower under load — the opposite of what
    # horizontal scaling is supposed to buy.
    threads = int(os.environ.get("ORT_INTRA_OP_THREADS", "0")) or min(4, os.cpu_count() or 1)
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1  # one request at a time per session

    # Memory arena, env-gated. Default `true` is onnxruntime's own default, so
    # the shipped behaviour is unchanged; the flag exists so the P9 retune can
    # A/B it without a code change, and so a memory-tight deploy can trade
    # latency for footprint.
    #
    # Context: this container's ANONYMOUS memory grows with use (~3.9GiB
    # against a 4GiB limit; page cache excluded, so it is real). The arena is
    # the prime suspect, since it caches freed blocks and never returns them.
    # Not yet proven — see docs for why the first attempt to measure it failed.
    opts.enable_cpu_mem_arena = os.environ.get("ORT_ENABLE_CPU_ARENA", "true").lower() == "true"
    return opts


def load(spec: ModelSpec) -> LoadedModel:
    """Build a session and record the digest of the bytes that produced it.

    The checksum is captured at load, not at fetch: it describes the weights
    THIS process is actually serving, which is what you need when an accuracy
    regression turns up weeks later and the question is which file was live.
    """
    path = spec.path
    if not path.is_file():
        raise ModelUnavailable(
            f"{path} is missing. Run `python scripts/download_models.py` on the "
            "host and mount ./models into the container — weights are "
            "deliberately not baked into the image."
        )

    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if spec.sha256 and digest != spec.sha256:
        raise ModelUnavailable(
            f"{path} checksum mismatch: registry pins {spec.sha256[:16]}…, "
            f"file is {digest[:16]}…. Refusing to serve unverified weights."
        )

    session = ort.InferenceSession(
        raw, sess_options=_session_options(), providers=["CPUExecutionProvider"]
    )
    inputs = session.get_inputs()
    if len(inputs) != 1:
        raise ModelUnavailable(
            f"{spec.name} expects {len(inputs)} inputs; this runtime handles single-input models"
        )

    logger.info(
        "loaded %s (%s) sha256=%s… threads=%d",
        spec.name,
        spec.task,
        digest[:16],
        session.get_session_options().intra_op_num_threads,
    )
    return LoadedModel(
        spec=spec,
        session=session,
        sha256=digest,
        input_name=inputs[0].name,
        output_names=tuple(o.name for o in session.get_outputs()),
    )
