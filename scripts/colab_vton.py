"""Run a REAL VTON model on Colab's free GPU.

    python scripts/colab_vton.py      # prints the cells to paste

WHY THIS FILE IS CAREFUL ABOUT ONE THING
-----------------------------------------
A widely-circulated "memory-optimized" Colab notebook for this model contains:

    def lightweight_tryon(person, garment):
        return person                      # "passthrough to keep memory at zero"

It builds a Gradio UI with the right endpoint shape and the right labels, so a
client connects, uploads, calls, and gets a valid image back — the person's own
photo, unchanged. Nothing errors. It looks exactly like a working render.

That cost us a false success: the pipeline stored the result and reported
`rendered: True`, because "bytes came back" and "a garment was fitted" were the
same check. `stylist_worker.tryon._looks_unchanged` now separates them, and
CELL 2 below is the same test run at the source, so a stub is caught before it
is ever wired in.

WHAT ACTUALLY RUNS HERE
-----------------------
Leffa's own `app.py`, with ONE line changed, via `exec`. It exposes
`leffa_predict_vt` with the full nine-argument signature — the one
`VTON_PROVIDER=leffa` is profiled against — and ends in
`demo.launch(share=True, ...)`. Weights download properly; a few GB, ~10 min.

THE ONE CHANGE, and why it is not the stub's kind of shortcut: `app.py` builds
THREE diffusion models at import. Two are the try-on models (SD1.5 inpainting);
the third, `pt_model`, is STABLE DIFFUSION XL inpainting and drives POSE
TRANSFER — a separate feature this project never calls. Three at once is what
exhausts a 16 GB T4.

Skipping only `pt_model` keeps BOTH try-on models, so `viton_hd` and
`dress_code` both survive and lower-body and dresses keep working. Nothing on
the try-on path is stubbed, weakened or faked; a capability we do not use is
simply not loaded. The patch asserts it applied, so an upstream change to
`app.py` fails loudly rather than quietly loading all three again.

Leffa is MIT and ungated. CatVTON is lighter but CC-BY-NC-SA-4.0, which is a
licensing problem for a product rather than a demo.

THE HONEST RISK
---------------
Even with SDXL skipped, two SD1.5 models plus DensePose, human parsing and
OpenPose may still not fit a free T4. If CELL 1 dies with CUDA OOM the answer
is Kaggle (P100/T4x2, 30 h/week), dropping `vt_model_dc` too (upper-body only),
or a paid per-render API — NEVER a passthrough.
"""

CELL_1 = r'''
# ============================ CELL 1 — run the real model ====================
# Runtime -> Change runtime type -> T4 GPU  BEFORE running this.

import subprocess, sys, os, types, re
print(subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                      "--format=csv,noheader"], capture_output=True, text=True).stdout or
      "NO GPU — set Runtime > Change runtime type > T4 GPU and rerun")

!pip -q install --upgrade "gradio>=4" gradio-client diffusers transformers accelerate \
    peft safetensors einops omegaconf opencv-python scikit-image timm huggingface_hub \
    av fvcore cloudpickle pycocotools torchmetrics onnxruntime 2>&1 | tail -2
# onnxruntime is NOT optional: human parsing runs parsing_lip.onnx and
# parsing_atr.onnx, and without it app.py dies at import with
# "No module named 'onnxruntime'" AFTER the 16GB download completes.
# It is in Leffa's own requirements.txt; omitting it was an oversight.

# `@spaces.GPU` is Hugging Face's own runtime shim and does not exist off their
# infrastructure. A no-op decorator is the entire requirement.
stub = types.ModuleType("spaces")
def GPU(*a, **k):
    def deco(fn): return fn
    return deco(a[0]) if a and callable(a[0]) else deco
stub.GPU = GPU
sys.modules["spaces"] = stub

!git lfs install -q 2>/dev/null
!git clone -q https://huggingface.co/spaces/franciszzj/Leffa /content/leffa || true
os.chdir("/content/leffa")

src = open("app.py").read()

# ---- THE ONE PATCH THAT MAKES THIS FIT IN 16 GB ---------------------------
# app.py loads THREE diffusion models at import: vt_model_hd and vt_model_dc
# (both SD1.5 inpainting) and pt_model — which is STABLE DIFFUSION XL
# inpainting, far larger than the other two together. Three at once is what
# exhausts a T4, and it is why the passthrough notebook exists.
#
# pt_model drives POSE TRANSFER, a feature unrelated to try-on and one this
# project never calls. Dropping only that keeps BOTH try-on models, so
# viton_hd and dress_code both stay available and lower-body and dresses keep
# working. `pt_inference` is aliased rather than deleted because app.py's UI
# references it at import time.
patched = re.sub(
    r"pt_model = LeffaModel\(.*?\)\s*pt_inference = LeffaInference\(model=pt_model\)",
    "pt_inference = vt_inference_hd  # pose transfer disabled: SDXL will not fit a T4",
    src, flags=re.S,
)
assert "pt_inference = vt_inference_hd" in patched, "patch did not apply — app.py changed upstream"
print("patched: pose-transfer (SDXL) model skipped, both try-on models kept")

# THE REAL APP, otherwise unmodified — it downloads its weights and exposes
# leffa_predict_vt with all nine arguments, then launches with share=True.
exec(patched)
'''

CELL_2 = r'''
# ==================== CELL 2 — prove it is NOT a passthrough =================
# Run this in a SECOND cell while cell 1 is still serving.
# If it says PASSTHROUGH, do not wire the URL up: the model is not running.

import io, requests
from PIL import Image, ImageChops
import numpy as np
from gradio_client import Client, handle_file

URL = "PASTE_YOUR_GRADIO_LIVE_URL_HERE"        # e.g. https://xxxx.gradio.live

person  = "https://huggingface.co/spaces/yisol/IDM-VTON/resolve/main/example/human/00034_00.jpg"
garment = "https://huggingface.co/spaces/yisol/IDM-VTON/resolve/main/example/cloth/04469_00.jpg"

c = Client(URL)
print("endpoints:", [e for e in c.view_api(return_format="dict")["named_endpoints"]])

out = c.predict(
    src_image_path=handle_file(person),
    ref_image_path=handle_file(garment),
    ref_acceleration="False", step=30, scale=2.5, seed=42,
    vt_model_type="viton_hd", vt_garment_type="upper_body", vt_repaint="False",
    api_name="/leffa_predict_vt",
)
result = Image.open(out[0] if isinstance(out, (list, tuple)) else out).convert("RGB")
source = Image.open(io.BytesIO(requests.get(person, timeout=60).content)).convert("RGB")

diff = np.asarray(ImageChops.difference(source.resize(result.size), result), float).mean()
print(f"\nmean pixel difference vs the input photo: {diff:.1f} / 255")
print("PASSTHROUGH — the model is NOT running" if diff < 2
      else "REAL RENDER — safe to wire up")
display(result)
'''

if __name__ == "__main__":
    print(__doc__)
    print("=" * 74)
    print(CELL_1)
    print("=" * 74)
    print(CELL_2)
    print("=" * 74)
    print("""THEN, only if CELL 2 says REAL RENDER:

  .env:
      VTON_PROVIDER=leffa            # NOT leffa-colab — that profile is for
                                     # the two-argument wrapper
      VTON_BASE_URL=https://xxxx.gradio.live
      VTON_API_TOKEN=                # empty; a share URL has no auth gate

  scripts/dc up -d --force-recreate api worker
      (force-recreate matters — compose bakes env in at container creation)
""")
