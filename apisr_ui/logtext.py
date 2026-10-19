"""Plain-English log translation (Pango markup). No GTK imports."""

import re

from .system import escape_markup as esc


def _tag(color, label, body, bold_body=False):
    b = f"<b>{body}</b>" if bold_body else body
    return (f'<span foreground="{color}" weight="bold">{label}</span> '
            f'<span foreground="#f1f5f9">{b}</span>')


def translate_to_plain_english(line):
    clean = line.strip()
    if not clean:
        return None
    if any(k in clean for k in ("return F.conv2d", "Conv_v8.cpp", "it/s", "s/it")):
        return None

    if clean.startswith("$ ") and "inference.py" in clean:
        return _tag("#38bdf8", "⚡ [KI FOCUS]", "Channeling Tensor Cores — upscaling anime frames with APISR neural model...")
    if clean.startswith("$ ") and "-f concat" in clean:
        return _tag("#c084fc", "📜 [SEALING JUTSU]", "Assembling remastered video, audio tracks, and subtitles into final container...")
    if clean.startswith("$ ") and "hevc_nvenc" in clean:
        return _tag("#fb923c", "🔥 [KAI BURST]", "Encoding 10-bit master video via NVIDIA NVENC hardware accelerator...")
    if clean.startswith("$ ") and "rife-ncnn-vulkan" in clean:
        return _tag("#facc15", "🌬 [WIND STEP]", "Interpolating in-between frames with RIFE...")
    if clean.startswith("$ ") and "ffmpeg" in clean and "-vf" in clean:
        return _tag("#34d399", "🌀 [SAGE FLOW]", "Executing video filter graph and format conversion...")
    if clean.startswith("Starting. Weights:"):
        return _tag("#38bdf8", "⚡ [CHAKRA AWAKENING]", "Loaded AI super-resolution weights into GPU memory.")
    if clean.startswith("PyTorch") and "can use" in clean:
        return _tag("#34d399", "⚡ [HARDWARE SENSE]",
                    f"Neural Engine connected to {esc(clean.split(':')[-1].strip())}", True)
    if clean.startswith("Checking that APISR starts with"):
        return _tag("#38bdf8", "🔍 [CHAKRA SENSE]", "Verifying Python virtual environment and dependencies...")
    if clean.startswith("Checking PyTorch"):
        return _tag("#38bdf8", "🔍 [CHAKRA SENSE]", "Testing GPU acceleration and Tensor Core availability...")
    if clean.startswith("=== ["):
        m = re.search(r"=== \[\d+/\d+\] (.*?) ===", clean)
        name = m.group(1) if m else "Video"
        return (f'<span foreground="#facc15" weight="bold">🎬 [EPISODE ENGAGED]</span> '
                f'<span foreground="#ffffff">Remastering <b>{esc(name)}</b></span>')
    if clean.startswith("Source:"):
        return f'<span foreground="#94a3b8">ℹ️ [SOURCE] {esc(clean)}</span>'
    if clean.startswith("Frames to process:"):
        return f'<span foreground="#38bdf8">📊 [TARGET] {esc(clean)}</span>'
    if clean.startswith("-- Chunk"):
        m = re.search(r"-- Chunk (\d+): (.*?) --", clean)
        if m:
            return _tag("#34d399", f"🌀 [CHAKRA WAVE {m.group(1)}]", f"Processing {esc(m.group(2))}...")
        return f'<span foreground="#34d399">{esc(clean)}</span>'
    if clean.startswith("Resuming: found"):
        return _tag("#38bdf8", "🔄 [CHAKRA RECOVERY]", esc(clean))
    if clean.startswith("Settings changed since the last run"):
        return _tag("#fb923c", "♻ [FRESH START]", esc(clean))
    if clean.startswith("Downscaling + encoding chunk"):
        return _tag("#fb923c", "🔥 [KAI BURST]", "Downscaling and encoding finished chunk in the background...")
    if clean.startswith("Applying AI dialogue clarity"):
        return _tag("#c084fc", "🎧 [SONIC RESTORATION]", "Enhancing vocal frequencies and applying RNNoise neural speech isolation...")
    if clean.startswith("Done:"):
        path = clean.replace("Done:", "").strip()
        return (f'<span foreground="#4ade80" weight="bold">🏆 [MISSION COMPLETE]</span> '
                f'<span foreground="#ffffff">Remaster finished: <b>{esc(path)}</b></span>')
    if clean.startswith("Running test frame upscale"):
        return _tag("#c084fc", "✨ [KI EXPERIMENT]", "Rendering test frame preview...")
    if clean.startswith("Test frame rendered successfully"):
        return _tag("#34d399", "✨ [KI SUCCESS]", "Test frame ready! Compare source vs upscaled above.")
    if clean.startswith("Downloading "):
        return _tag("#facc15", "📥 [SCROLL SUMMON]", esc(clean))
    if clean.startswith("Saved: "):
        return _tag("#34d399", "✅ [STORED]", esc(clean))
    if clean.startswith(("NOTE:", "WARNING:")):
        return f'<span foreground="#fbbf24" weight="bold">⚠ [HEADS UP]</span> <span foreground="#fde68a">{esc(clean)}</span>'
    if "error" in clean.lower() or "failed" in clean.lower():
        return f'<span foreground="#f87171" weight="bold">❌ [NOTICE]</span> <span foreground="#fecaca">{esc(clean)}</span>'
    return f'<span foreground="#94a3b8">· {esc(clean)}</span>'
