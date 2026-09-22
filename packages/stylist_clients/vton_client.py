"""Virtual try-on against a Hugging Face Gradio Space (Phase 10).

WHY THIS EXISTS AND WHAT IT IS NOT
-----------------------------------
Phase 10 opens with "benchmark before you build", and an earlier pass read that
as a reason to build nothing. That was wrong, and the distinction is worth
stating because it is the whole design of this module:

  a ROUTING table maps  garment category -> which model is best
  a PROFILE table maps  model -> how that model is called

The first is a quality judgement. It needs the 10-body x 16-garment grid,
because "which model renders a kurta best" is not answerable from a datasheet.
The second is documented fact, and every signature in PROFILES below was read
from the provider's own /info endpoint on 2026-09-17, not inferred.

Refusing to build the second because the first is unmeasured is backwards:
you cannot run the benchmark without a working render path. This module IS the
prerequisite for the grid, not a substitute for it. There is exactly one
provider at a time (`VTON_PROVIDER`), no fallback and no per-category
selection, because those are the parts that need the data.

THE TRANSPORT IS HTTP, NOT gradio_client
-----------------------------------------
Same reasoning as google_calendar.py: the SDK brings a large dependency tree
and a credential-discovery layer that reads ambient environment. The Gradio
protocol is three calls, verified end to end:

  POST {prefix}/upload                  multipart -> ["/tmp/gradio/<hash>/f.png"]
  POST {prefix}/call/{api_name}         {"data":[...]} -> {"event_id": "..."}
  GET  {prefix}/call/{api_name}/{id}    SSE: event: complete / data: [...]

`{prefix}` is `/gradio_api` on Gradio 5 and empty on Gradio 4. Both are live
today (Leffa is 5, IDM-VTON is 4), so it is detected once and cached rather
than assumed — guessing wrong turns every call into an opaque 404.

ZEROGPU NEEDS A TOKEN
---------------------
All three candidate Spaces run on `zero-a10g`. An anonymous programmatic call
is rejected in well under a second, which is how you tell it apart from a
render that failed: a diffusion model that actually ran takes 30s+. Gradio
returns `{"error": null}` with tracebacks suppressed, so the provider's own
message is the only diagnostic there is and this client propagates it verbatim
rather than replacing it with a summary of its own.

A RENDER IS NOT AN HTTP REQUEST
-------------------------------
30-120s on shared hardware, queued behind other users. That is a job, which is
why nothing here is called from a request handler — see stylist_worker.tryon.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Generous, because the wait is a queue on shared hardware rather than our
# latency. The job owns the deadline; this only stops an abandoned stream from
# holding the connection forever.
CONNECT_TIMEOUT = 10.0
UPLOAD_TIMEOUT = 180.0
RENDER_TIMEOUT = 900.0

# The three categories every one of these models accepts. Deliberately NOT an
# enum over our own slots: this is the provider's vocabulary, and collapsing
# the two is how a mapping becomes invisible.
UPPER, LOWER, DRESS = "upper_body", "lower_body", "dresses"

# Our slot -> the provider's category.
#
# `drape` IS ABSENT, AND THAT IS THE POINT. A saree is not upper-body, not
# lower-body and not a dress; every one of these models was trained on
# VITON-HD or DressCode, both Western catalogues. Mapping `drape` to `dresses`
# would produce a confident, wrong render — and §C3's whole argument is that a
# confident wrong answer costs more than an absent one. Which category (if
# any) serves a saree is exactly what the benchmark is for.
#
# head/feet/bag/accessory are absent because these models do not render them
# at all; they are not a gap to close, they are out of scope for these models entirely.
SLOT_TO_CATEGORY: dict[str, str] = {
    "upper_base": UPPER,
    "upper_layer": UPPER,
    "lower": LOWER,
    "full_body": DRESS,
}


class VTONUnavailable(RuntimeError):  # noqa: N818 - a state, not an error type
    """The provider could not render. Says nothing about the garment.

    Mirrors MLUnavailable deliberately: the caller degrades to a board either
    way, but only this distinction tells you whether retrying is pointless.
    """


@dataclass(frozen=True, slots=True)
class Profile:
    """How one provider is called. Read from its /info, not inferred."""

    space: str
    api_name: str
    # Built as a function of (person_path, garment_path, category) because the
    # three providers disagree on argument ORDER, on whether a category is
    # accepted at all, and on whether a file is a bare path or a FileData dict.
    build_data: Any
    # IDM-VTON takes no category: it is VITON-HD, upper-body only. Recording
    # that here means the caller can refuse a lower-body render rather than
    # silently sending trousers to a model that will put them on a torso.
    categories: frozenset[str]
    result_index: int = 0


def _file(path: str) -> dict[str, Any]:
    """Gradio's FileData envelope for an already-uploaded server path."""
    return {"path": path, "meta": {"_type": "gradio.FileData"}}


PROFILES: dict[str, Profile] = {
    # Verified 2026-09-17 against https://franciszzj-Leffa.hf.space/gradio_api/info
    #   leffa_predict_vt(src_image_path, ref_image_path, ref_acceleration, step,
    #                    scale, seed, vt_model_type, vt_garment_type, vt_repaint)
    # THE DEFAULT, for one measurable reason: it is the only candidate that
    # takes an explicit garment type AND exposes `dress_code` as a model type,
    # so it is the only one that can be pointed at lower-body and full-body
    # garments without pretending they are shirts.
    "leffa": Profile(
        space="franciszzj-Leffa",
        api_name="leffa_predict_vt",
        build_data=lambda person, garment, category: [
            _file(person),
            _file(garment),
            "False",
            30,
            2.5,
            42,
            # DressCode weights for anything that is not a plain top: it is the
            # half of the training data that contains dresses and lower-body
            # garments at all.
            "viton_hd" if category == UPPER else "dress_code",
            category,
            "False",
        ],
        categories=frozenset({UPPER, LOWER, DRESS}),
    ),
    # Verified 2026-09-17 against https://yisol-IDM-VTON.hf.space/info
    #   /tryon(dict, garm_img, garment_des, is_checked, is_checked_crop,
    #          denoise_steps, seed)
    # Gradio 4, so no /gradio_api prefix. NO CATEGORY PARAMETER — VITON-HD
    # weights, upper-body only. Recorded as a one-element set rather than
    # left implicit.
    "idm-vton": Profile(
        space="yisol-IDM-VTON",
        api_name="tryon",
        build_data=lambda person, garment, category: [
            {"background": _file(person), "layers": [], "composite": None},
            _file(garment),
            "a garment",
            True,
            True,
            30,
            42,
        ],
        categories=frozenset({UPPER}),
    ),
    # LEFFA WITH ONLY THE VITON-HD MODEL LOADED.
    #
    # Same nine-argument `leffa_predict_vt` endpoint as `leffa`, so the call
    # shape is identical — the difference is which weights are in memory. On a
    # 16GB GPU the DressCode model is commonly dropped alongside the SDXL
    # pose-transfer one to make Leffa fit at all, and this profile matches that
    # deployment.
    #
    # UPPER BODY ONLY, declared rather than discovered. `leffa` sends
    # `vt_model_type="dress_code"` for lower-body and dresses; against a server
    # where those weights were never loaded that either errors or silently
    # renders with the wrong model. Restricting the category here means the
    # worker skips trousers and sarees and still renders the shirt, instead of
    # producing a confident wrong picture.
    "leffa-hd": Profile(
        space="",  # tunnel-only; always paired with VTON_BASE_URL
        api_name="leffa_predict_vt",
        build_data=lambda person, garment, category: [
            _file(person),
            _file(garment),
            "False",
            30,
            2.5,
            42,
            "viton_hd",  # the only model this deployment has
            category,
            "False",
        ],
        categories=frozenset({UPPER}),
    ),
    # A LEFFA WRAPPER, verified 2026-09-18 against a live tunnel:
    #   lightweight_tryon(person, garment) -> output
    #
    # "Leffa Free Colab Memory-Optimized Tunnel" — the same model behind a
    # two-argument endpoint that drops `vt_garment_type` and `vt_model_type`.
    # Used with VTON_BASE_URL pointed at a Colab/Kaggle share URL, which is how
    # this renders for free: no ZeroGPU gate, no token, no bill.
    #
    # UPPER BODY ONLY, and that is a deduction rather than a limitation of the
    # underlying model. Leffa itself handles lower body and dresses, but this
    # wrapper removed the argument that selects them — so the defaults are
    # baked in and we cannot tell it what it is fitting. Declaring all three
    # would send trousers to a model configured for tops and get back a
    # confident picture of the wrong thing, which is exactly what the category
    # guard exists to prevent.
    "leffa-colab": Profile(
        space="",  # always paired with VTON_BASE_URL; there is no public Space
        api_name="lightweight_tryon",
        build_data=lambda person, garment, category: [_file(person), _file(garment)],
        categories=frozenset({UPPER}),
    ),
    # Verified 2026-09-17 against https://levihsu-OOTDiffusion.hf.space/gradio_api/info
    #   /process_dc(vton_img, garm_img, category, n_samples, n_steps,
    #               image_scale, seed)  -> Gallery
    # `process_dc` rather than `process_hd`: the hd entry point has no category
    # argument. Its capitalised vocabulary is the provider's, mapped here.
    "ootdiffusion": Profile(
        space="levihsu-OOTDiffusion",
        api_name="process_dc",
        build_data=lambda person, garment, category: [
            _file(person),
            _file(garment),
            {UPPER: "Upper-body", LOWER: "Lower-body", DRESS: "Dress"}[category],
            1,
            20,
            5.0,
            -1,
        ],
        categories=frozenset({UPPER, LOWER, DRESS}),
    ),
}


class VTONClient:
    """One provider, called over its Gradio HTTP API."""

    def __init__(
        self,
        provider: str,
        *,
        api_token: str = "",
        base_url: str = "",
        timeout: float = RENDER_TIMEOUT,
    ) -> None:
        if provider not in PROFILES:
            raise ValueError(f"unknown VTON provider {provider!r}; known: {sorted(PROFILES)}")
        self.provider = provider
        self.profile = PROFILES[provider]
        self.api_token = api_token
        # An override so a self-hosted or dedicated Inference Endpoint can be
        # pointed at without a code change — the protocol is identical, only
        # the host differs.
        if not base_url and not self.profile.space:
            # A tunnel-only profile with nowhere to go. Caught here rather than
            # as a confusing DNS failure 30 seconds into a render.
            raise ValueError(f"provider {provider!r} requires VTON_BASE_URL to be set")
        self.base_url = (base_url or f"https://{self.profile.space}.hf.space").rstrip("/")
        self.timeout = timeout
        self._prefix: str | None = None

    def _headers(self) -> dict[str, str]:
        # ZeroGPU rejects anonymous API calls. The token is the difference
        # between a render and a sub-second `{"error": null}`.
        return {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}

    def _detect_prefix(self, client: httpx.Client) -> str:
        """`/gradio_api` (Gradio 5) or `` (Gradio 4). Probed once, then cached.

        Detected rather than configured: it is a property of the Space's Gradio
        version, which changes when the author redeploys and is not something
        an operator should have to track in an env var.
        """
        if self._prefix is not None:
            return self._prefix
        for candidate in ("/gradio_api", ""):
            try:
                resp = client.get(f"{self.base_url}{candidate}/info", timeout=CONNECT_TIMEOUT)
            except httpx.HTTPError:
                continue
            if resp.status_code == 200:
                self._prefix = candidate
                return candidate
        raise VTONUnavailable(f"{self.base_url}: no Gradio API at /info or /gradio_api/info")

    def _upload(self, client: httpx.Client, images: list[tuple[str, bytes]]) -> list[str]:
        prefix = self._detect_prefix(client)
        files = [
            ("files", (name, data, mimetypes.guess_type(name)[0] or "image/png"))
            for name, data in images
        ]
        try:
            resp = client.post(
                f"{self.base_url}{prefix}/upload",
                files=files,
                headers=self._headers(),
                timeout=UPLOAD_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise VTONUnavailable(f"upload failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise VTONUnavailable(f"upload failed: HTTP {resp.status_code}: {resp.text[:300]}")
        paths = resp.json()
        if not isinstance(paths, list) or len(paths) != len(images):
            raise VTONUnavailable(f"upload returned {paths!r}, expected {len(images)} paths")
        return [str(p) for p in paths]

    def render(self, *, person_png: bytes, garment_png: bytes, category: str) -> bytes:
        """One garment onto one body. Returns PNG bytes.

        Raises VTONUnavailable for every failure, because the caller's answer is
        the same in all of them — show the board — and the only thing that
        differs is what gets logged.
        """
        if category not in self.profile.categories:
            # Refused rather than coerced. Sending trousers to an upper-body
            # model returns a confident picture of the wrong thing, which is
            # worse than the board it would have degraded to.
            raise VTONUnavailable(
                f"{self.provider} does not render {category!r} "
                f"(supports: {sorted(self.profile.categories)})"
            )

        with httpx.Client(follow_redirects=True) as client:
            prefix = self._detect_prefix(client)
            person_path, garment_path = self._upload(
                client, [("person.png", person_png), ("garment.png", garment_png)]
            )
            payload = {"data": self.profile.build_data(person_path, garment_path, category)}

            try:
                started = client.post(
                    f"{self.base_url}{prefix}/call/{self.profile.api_name}",
                    json=payload,
                    headers={**self._headers(), "Content-Type": "application/json"},
                    timeout=CONNECT_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise VTONUnavailable(f"call failed: {type(exc).__name__}: {exc}") from exc
            if started.status_code != 200:
                raise VTONUnavailable(
                    f"call failed: HTTP {started.status_code}: {started.text[:300]}"
                )
            event_id = started.json().get("event_id")
            if not event_id:
                raise VTONUnavailable(f"no event_id in {started.text[:200]}")

            result = self._await_result(client, prefix, event_id)
            return self._fetch_image(client, prefix, result)

    def _await_result(self, client: httpx.Client, prefix: str, event_id: str) -> Any:
        """Consume the SSE stream until the run completes.

        Streamed rather than polled because Gradio offers no status endpoint
        for these events — the stream IS the status, and holding it open is how
        you learn the run finished.
        """
        url = f"{self.base_url}{prefix}/call/{self.profile.api_name}/{event_id}"
        try:
            with client.stream("GET", url, headers=self._headers(), timeout=self.timeout) as stream:
                event: str | None = None
                for line in stream.iter_lines():
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and event in ("complete", "error"):
                        raw = line.split(":", 1)[1].strip()
                        data = json.loads(raw) if raw and raw != "null" else None
                        if event == "error":
                            # Gradio suppresses tracebacks, so `data` is very
                            # often null. Saying so beats inventing a cause —
                            # and a null here with a sub-second turnaround is
                            # the ZeroGPU auth signature described above.
                            raise VTONUnavailable(
                                f"{self.provider} returned an error: {data!r} "
                                "(a null error with no token set is usually "
                                "ZeroGPU refusing an anonymous API call)"
                            )
                        return data
        except httpx.HTTPError as exc:
            raise VTONUnavailable(f"stream failed: {type(exc).__name__}: {exc}") from exc
        raise VTONUnavailable("stream ended without a result")

    def _fetch_image(self, client: httpx.Client, prefix: str, result: Any) -> bytes:
        """Pull the rendered PNG out of whatever shape the provider returned.

        Three shapes in play: a bare path (IDM-VTON), a FileData dict (Leffa),
        and a Gallery list of dicts (OOTDiffusion). Handled by structure rather
        than by provider, so a provider that changes its component type does
        not need a new branch.
        """
        item: Any = result
        if isinstance(item, list):
            if not item:
                raise VTONUnavailable("provider returned an empty result")
            item = item[self.profile.result_index if len(item) > 1 else 0]
        if isinstance(item, list):  # Gallery: [[{image: ...}], ...]
            item = item[0] if item else None
        if isinstance(item, dict):
            item = item.get("image", item)
        if isinstance(item, dict):
            url, path = item.get("url"), item.get("path")
        elif isinstance(item, str):
            url, path = (item, None) if item.startswith("http") else (None, item)
        else:
            raise VTONUnavailable(f"unrecognised result shape: {type(item).__name__}")

        target = url or f"{self.base_url}{prefix}/file={path}"
        try:
            resp = client.get(target, headers=self._headers(), timeout=UPLOAD_TIMEOUT)
        except httpx.HTTPError as exc:
            raise VTONUnavailable(f"result fetch failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200 or not resp.content:
            raise VTONUnavailable(f"result fetch: HTTP {resp.status_code}, {len(resp.content)}b")
        return resp.content
