"""Generate a short clip from a text prompt on a local GPU (LTX-Video via diffusers).

Runs in its own environment (.venv-video) as a subprocess of the app:
  .venv-video/bin/python video_gen.py --prompt "..." --out clip.mp4 [--seconds 4] [--seed 7]
Prints JSON on the last line: {"ok": true, "path": ..., "seconds": ...} or {"ok": false, "error": ...}.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

MODEL = os.environ.get("VIDEO_MODEL", "Lightricks/LTX-Video")
NEGATIVE = ("worst quality, inconsistent motion, blurry, jittery, distorted, deformed, text, watermark, "
            "logo, cartoon, illustration, low resolution, extra limbs")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--width", type=int, default=768)
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--steps", type=int, default=int(os.environ.get("VIDEO_STEPS", "30")))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--fps", type=int, default=24)
    a = ap.parse_args()
    t0 = time.time()
    try:
        import torch
        from diffusers import LTXPipeline
        from diffusers.utils import export_to_video

        pipe = LTXPipeline.from_pretrained(MODEL, torch_dtype=torch.bfloat16)
        pipe.to("cuda")
        pipe.vae.enable_tiling()
        frames = int(a.seconds * a.fps)
        frames = max(9, (frames // 8) * 8 + 1)  # LTX wants 8k+1 frames
        gen = torch.Generator("cuda").manual_seed(a.seed)
        video = pipe(prompt=a.prompt, negative_prompt=NEGATIVE, width=a.width, height=a.height,
                     num_frames=frames, num_inference_steps=a.steps, guidance_scale=3.0,
                     generator=gen).frames[0]
        export_to_video(video, a.out, fps=a.fps)
        print(json.dumps({"ok": True, "path": a.out, "seconds": round(frames / a.fps, 2),
                          "wall": round(time.time() - t0, 1), "model": MODEL}))
        return 0
    except Exception as e:  # report, never crash the caller
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}", "wall": round(time.time() - t0, 1)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
