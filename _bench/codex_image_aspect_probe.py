"""Can the prompt steer gpt-image-2's `auto` size through codex's image tool?

codex hardcodes size="auto"/quality="auto" (ext/image-generation/src/tool.rs),
so the prompt is the only lever. Trials (2026-09-12, codex-cli 0.154.0):

  A gen, standing knight, no aspect words        -> 1024x1536 (subject decides)
  B gen, "landscape 3:2, 1536 by 1024"           -> 1536x1024
  C edit, 1536x1024 reference, no aspect words   -> 1536x1024
  D edit, same + "keep landscape 1536x1024"      -> 1536x1024 (tool called TWICE)
  E gen, "exactly 1280 by 720 (16:9)"            -> 1672x941
  F gen, "exactly 800 by 800 (square)"           -> 1254x1254

Reading: the ASPECT RATIO follows explicit prompt wording reliably; the PIXEL
SIZE does not — every output is ~1.57 megapixels (1254^2, 1536x1024, 1672x941
are all 1.573 MP), i.e. gpt-image-2 normalizes to a fixed budget. The
assistant's text claims the requested size ("Generated a 1280x720 ...") even
when the PNG is 1672x941 — trust the IHDR, not the prose. D shows a wordy
"keep exact dimensions" instruction can make the model call the tool twice
(double cost); agentry's wrappers say "exactly once".

Usage: python _bench/codex_image_aspect_probe.py <out_dir> [ref.png]
"""
import base64
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codex_imagegen_probe import Codex, ALLOW, png_dims  # noqa: E402

S = sys.argv[1]
REF = sys.argv[2] if len(sys.argv) > 2 else None   # a 1536x1024 landscape PNG
SUBJECT = ("a lone knight in full plate armour standing upright, full body, "
           "filling the frame, flat cel-shaded illustration, plain background")
TRIALS = [
    ("A gen, no aspect words",      None, f"Generate an image: {SUBJECT}. Then reply with one sentence."),
    ("B gen, explicit landscape",   None, f"Generate an image: {SUBJECT}. The image MUST be landscape orientation, 3:2 aspect ratio, 1536 pixels wide by 1024 pixels tall. Then reply with one sentence."),
    ("C edit, landscape ref, no aspect words", REF, f"Edit the attached image: replace the shape with {SUBJECT}. Then reply with one sentence."),
    ("D edit, landscape ref, explicit keep",   REF, f"Edit the attached image: replace the shape with {SUBJECT}. Keep the attached image's landscape orientation and exact 1536x1024 dimensions. Then reply with one sentence."),
    ("E gen, exact 1280x720",  None, f"Generate an image: {SUBJECT}. Call the image generation tool exactly once. The image MUST be exactly 1280 pixels wide by 720 pixels tall (16:9 landscape). Then reply with one sentence."),
    ("F gen, exact 800x800 square", None, f"Generate an image: {SUBJECT}. Call the image generation tool exactly once. The image MUST be exactly 800 pixels wide by 800 pixels tall (square). Then reply with one sentence."),
]
TRIALS = [t for t in TRIALS if t[1] is None or REF]
scratch = os.path.join(tempfile.gettempdir(), "agentry-codex-imagegen-probe")
for label, ref, prompt in TRIALS:
    print(f"\n=== {label}")
    cx = Codex(scratch, ALLOW)
    try:
        t0 = time.time()
        imgs = cx.run(prompt, "low", ref_png=ref)
        for it in imgs:
            b = base64.b64decode(it["result"])
            open(os.path.join(S, f"aspect_{label.split()[0]}.png"), "wb").write(b)
            print(f"  >>> {label}: dims={png_dims(b)} {len(b)//1024} KB {time.time()-t0:.0f}s")
            print(f"  revised: {it.get('revisedPrompt')!r}")
        if not imgs:
            print("  >>> no image")
    finally:
        cx.close()
