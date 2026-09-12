"""Does the chat model matter for codex image generation?

The image model is pinned (gpt-image-2, ext/image-generation/src/tool.rs); the
chat model only (a) gets the tool offered or not, (b) rewrites the prompt it
hands the tool, (c) decides how many times to call it. Runs one landscape
knight prompt on every model from model/list and reports dims, wall time,
call count and the rewritten prompt.

Results 2026-09-12 (codex-cli 0.154.0, Plus plan, effort=low):

  gpt-5.6-luna   1536x1024  46 s  1 call  rewrite: near-verbatim (+"clean crisp shapes")
  gpt-5.6-terra  1536x1024  46 s  1 call  rewrite: ~3x longer, adds palette/pose/"no border"
  gpt-5.6-sol    1536x1024  58 s  1 call  rewrite: ~3x longer, adds "no weapon required"
  gpt-6-astra    1536x1024  40 s  1 call  rewrite: ~3x longer, adds margins/crop rules
  gpt-5.5        1536x1024  42 s  1 call  rewrite: ~2x longer; tool item id "call_*" not "exec-*"

Reading: every model gets the tool and honors the aspect; the picture comes
from the same gpt-image-2 either way. The difference is the rewrite — luna
forwards the prompt almost untouched, the bigger models embellish it with
content the user never asked for (sol removed the sword). For a pipeline
that wants ITS prompt to reach the image model, the cheap default is the
best choice, and it is also not slower. Gating (core/src/tools/spec_plan.rs
image_generation_available): feature flag on, plan != Free, model has image
input modality, and ChatGPT-login auth — an OPENAI_API_KEY login does not
get the tool.

Usage: python _bench/codex_image_model_probe.py <out_dir> [model ...]
"""
import base64
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codex_imagegen_probe import Codex, ALLOW, png_dims  # noqa: E402

PROMPT = ("Generate an image: a lone knight in full plate armour standing upright, full body, "
          "filling the frame, flat cel-shaded illustration, plain background. The image MUST be "
          "landscape (wider than tall), aspect ratio 1536:1024, i.e. 1536 pixels wide by 1024 "
          "pixels tall. Call the image generation tool exactly once. Then reply with one sentence.")


class ModelCodex(Codex):
    """Codex probe client that stamps a model on turn/start."""
    model = None

    def _req(self, method, params, timeout=60):
        if method == "turn/start" and self.model:
            params = dict(params, model=self.model)
        return super()._req(method, params, timeout)


def main():
    out = sys.argv[1]
    scratch = os.path.join(tempfile.gettempdir(), "agentry-codex-imagegen-probe")
    models = sys.argv[2:]
    if not models:
        cx = Codex(scratch, ALLOW)
        try:
            models = [m["id"] for m in cx._req("model/list", {}, timeout=20).get("data", [])]
        finally:
            cx.close()
    for model in models:
        print(f"\n=== model={model}")
        cx = ModelCodex(scratch, ALLOW)
        cx.model = model
        try:
            t0 = time.time()
            imgs = cx.run(PROMPT, "low")
            for it in imgs:
                b = base64.b64decode(it["result"])
                open(os.path.join(out, f"model_{model}.png"), "wb").write(b)
                print(f"  >>> {model}: dims={png_dims(b)} {len(b)//1024} KB "
                      f"{time.time()-t0:.0f}s calls={len(imgs)}")
                print(f"  rewrite ({len(it.get('revisedPrompt') or '')} chars): "
                      f"{it.get('revisedPrompt')!r}")
            if not imgs:
                print(f"  >>> {model}: no image")
        except Exception as e:
            print(f"  >>> {model}: error {e}")
        finally:
            cx.close()


if __name__ == "__main__":
    main()
