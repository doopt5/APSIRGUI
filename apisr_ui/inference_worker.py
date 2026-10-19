#!/usr/bin/env python3
"""Persistent APISR inference worker (EXPERIMENTAL).

Runs inside the APISR Python environment (NOT the GUI's Python). Loads the generator once
and then upscales whole directories on request, avoiding a model/CUDA reload per chunk.

Protocol (line based, stdin -> stdout):
    request:   <in_dir>\t<out_dir>\n
    responses: READY\n            (after the model is loaded)
               DONE <count>\n     (request finished)
               ERR <message>\n    (request failed)
The GUI falls back to one-process-per-chunk if this worker cannot start.
"""

import os
import sys
import traceback
from pathlib import Path


def main():
    apisr_dir, weight, scale, fp16 = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1"
    os.chdir(apisr_dir)
    sys.path.insert(0, apisr_dir)
    import torch
    import importlib

    try:
        mod = importlib.import_module("test_code.inference")
    except Exception as e:
        print(f"ERR could not import test_code.inference: {e}", flush=True)
        return 2

    fn = getattr(mod, "super_resolve_img", None)
    load_rrdb = getattr(mod, "load_rrdb", None)
    load_grl = getattr(mod, "load_grl", None)
    load_dat = getattr(mod, "load_dat", None)
    if fn is None:
        print("ERR inference.py exposes no super_resolve_img()", flush=True)
        return 2
    name = Path(weight).name
    loader = (load_grl if "GRL" in name else load_dat if "DAT" in name else load_rrdb)
    if loader is None:
        print("ERR no matching generator loader found in inference.py", flush=True)
        return 2
    try:
        generator = loader(weight, scale=scale)
    except TypeError:
        generator = loader(weight)
    dtype = torch.float16 if fp16 else torch.float32
    if fp16:
        try:
            generator = generator.half()
        except Exception:
            pass
    print("READY", flush=True)

    exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        if line == "QUIT":
            break
        try:
            in_dir, out_dir = line.split("\t")
            out_path = Path(out_dir)
            out_path.mkdir(parents=True, exist_ok=True)
            n = 0
            for p in sorted(Path(in_dir).iterdir()):
                if p.suffix.lower() not in exts:
                    continue
                dst = out_path / (p.stem + ".png")
                with torch.no_grad():
                    res = fn(generator, str(p), str(dst), weight_dtype=dtype)
                if not dst.exists() and res is not None:
                    try:
                        from torchvision.utils import save_image
                        save_image(res, str(dst))
                    except Exception:
                        import numpy as np
                        from PIL import Image
                        arr = res if isinstance(res, np.ndarray) else res.detach().cpu().numpy()
                        Image.fromarray(arr).save(dst)
                n += 1
            print(f"DONE {n}", flush=True)
        except Exception:
            print("ERR " + traceback.format_exc().strip().splitlines()[-1], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
