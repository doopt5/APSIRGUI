"""Configuration: dataclass, XDG paths, load/save, profiles. No GTK imports."""

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_ID = "dev.apisr.Upscaler"


def xdg_dir(env_name, fallback):
    val = os.environ.get(env_name, "")
    base = Path(val) if val and os.path.isabs(val) else Path.home() / fallback
    return base


CONFIG_DIR = xdg_dir("XDG_CONFIG_HOME", ".config")
DATA_DIR = xdg_dir("XDG_DATA_HOME", ".local/share")
CONFIG_PATH = CONFIG_DIR / "apisr_gui.json"
ICON_CACHE_DIR = DATA_DIR / "icons" / "hicolor" / "scalable" / "apps"
APPLICATIONS_DIR = DATA_DIR / "applications"
MODELS_CACHE_DIR = DATA_DIR / "apisr" / "models"

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ENCODERS = ["hevc_nvenc", "h264_nvenc", "libx265", "libx264", "libsvtav1"]
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
           "medium", "slow", "slower", "veryslow"]
HEIGHTS = ["720", "1080", "1440", "2160", "keep"]
FPS_SMOOTH_OPTIONS = ["Off", "48 fps (Cinematic 2x)", "60 fps (Ultra Smooth)"]
ON_DONE_OPTIONS = ["Do nothing", "Desktop notification", "Suspend PC", "Shutdown PC"]
DEVICE_AUTO = "Auto (default)"
DEVICE_CPU = "CPU only (slow)"
DEFAULT_FRAME_FORMAT = "JPEG - near-lossless (4:4:4, ~5x smaller)"


@dataclass
class Config:
    apisr_dir: str = ""
    weight: str = ""
    python: str = sys.executable
    out_dir: str = ""
    work_dir: str = ""
    extra: str = ""
    height: str = "1080"
    encoder: str = "hevc_nvenc"
    crf: int = 18
    preset: str = "medium"
    tenbit: bool = True
    chunk: int = 1000
    test_seconds: int = 0
    keep: bool = False
    frame_format: str = DEFAULT_FRAME_FORMAT
    opacity: float = 0.88
    appearance: str = "system"
    device: str = DEVICE_AUTO
    gpu_stats: bool = True
    fp16: bool = True
    deband: bool = True
    sharpen: bool = True
    on_done: str = "Desktop notification"
    # CUDA
    nvdec: bool = True
    cuda_filters: bool = False
    tensorrt: bool = False
    persistent_worker: bool = False
    # Advanced processing
    rife_fps: str = "Off"
    hdr10: bool = False
    ai_audio: bool = False
    deinterlace: bool = False
    grain: bool = False
    derainbow: bool = False
    ramdisk: bool = False
    sound_cues: bool = True
    confirm_start: bool = True
    # State
    videos: list = field(default_factory=list)
    profiles: dict = field(default_factory=dict)
    bench: dict = field(default_factory=dict)

    # --- dict-like helpers so older call sites keep working -------------
    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def to_dict(self):
        return asdict(self)

    def copy(self):
        return Config.from_dict(json.loads(json.dumps(self.to_dict())))

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


# Settings that a profile may carry (everything user-tunable, nothing machine-specific)
PROFILE_KEYS = [
    "height", "encoder", "crf", "preset", "tenbit", "chunk", "frame_format", "fp16",
    "deband", "sharpen", "nvdec", "cuda_filters", "tensorrt", "rife_fps", "hdr10",
    "ai_audio", "deinterlace", "grain", "derainbow", "ramdisk", "persistent_worker",
]

BUILTIN_PROFILES = {
    "Fast preview (720p, NVENC)": {
        "height": "720", "encoder": "hevc_nvenc", "crf": 24, "tenbit": False,
        "deband": False, "sharpen": False, "grain": False, "hdr10": False,
        "deinterlace": False, "derainbow": False, "rife_fps": "Off",
    },
    "Balanced 1080p (HEVC NVENC)": {
        "height": "1080", "encoder": "hevc_nvenc", "crf": 18, "tenbit": True,
        "deband": True, "sharpen": True, "grain": False, "hdr10": False,
        "deinterlace": False, "derainbow": False, "rife_fps": "Off",
    },
    "Archive quality (x265, CRF 16)": {
        "height": "1080", "encoder": "libx265", "preset": "slow", "crf": 16,
        "tenbit": True, "deband": True, "sharpen": True, "grain": False,
        "hdr10": False, "rife_fps": "Off",
    },
    "DVD restore (deinterlace + chroma cleanup)": {
        "height": "1080", "encoder": "hevc_nvenc", "crf": 18, "tenbit": True,
        "deinterlace": True, "derainbow": True, "deband": True, "sharpen": True,
        "grain": False, "hdr10": False, "rife_fps": "Off",
    },
}


def load_config(find_pythons=None, guess_apisr_dir=None):
    cfg = Config()
    if guess_apisr_dir:
        cfg.apisr_dir = guess_apisr_dir()
    try:
        data = json.loads(CONFIG_PATH.read_text())
        if isinstance(data, dict):
            # Drop keys from older versions that no longer exist (silently ignored
            # by from_dict) and keep the guessed APISR dir if nothing was saved.
            merged = cfg.to_dict()
            merged.update({k: v for k, v in data.items() if k in merged})
            cfg = Config.from_dict(merged)
    except Exception:
        pass
    if find_pythons and str(cfg.python).startswith(("/usr/", "/bin/", "/sbin/")):
        for cand in find_pythons(cfg.apisr_dir):
            if not str(cand).startswith(("/usr/", "/bin/", "/sbin/")) and "apisr" in cand.lower():
                cfg.python = cand
                break
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg.to_dict(), indent=2))
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass
