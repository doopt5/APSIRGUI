"""System helpers: Python/torch detection, GPU monitor, memory, downloads. No GTK imports."""

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from .config import APPLICATIONS_DIR, ICON_CACHE_DIR, APP_ID

GUI_MODULES = {"gi", "cairo", "gtk", "adw", "glib", "gobject"}
PIP_NAMES = {"cv2": "opencv-python", "PIL": "pillow", "skimage": "scikit-image",
             "yaml": "pyyaml", "sklearn": "scikit-learn"}
NO_AUTO_INSTALL = {"torch", "torchvision", "torchaudio"}

TORCH_HINT_LEGACY = ("pip install torch==2.1.1 torchvision==0.16.1 "
                     "--index-url https://download.pytorch.org/whl/cu118")
TORCH_HINT_BLACKWELL = ("pip install torch torchvision "
                        "--index-url https://download.pytorch.org/whl/cu128")


def is_system_python(p):
    return str(p).startswith(("/usr/", "/bin/", "/sbin/"))


def guess_apisr_dir():
    here = Path(__file__).resolve().parent.parent
    for p in (here, Path.cwd(), Path.home() / "APISR"):
        if (p / "test_code" / "inference.py").exists():
            return str(p)
    return ""


def find_pythons(apisr_dir=""):
    cands = []

    def add(path):
        path = str(path)
        if os.path.isfile(path) and os.access(path, os.X_OK) and path not in cands:
            cands.append(path)

    for var in ("CONDA_PREFIX", "VIRTUAL_ENV"):
        if os.environ.get(var):
            add(Path(os.environ[var]) / "bin" / "python")
    if apisr_dir:
        for d in (".venv", "venv", "env"):
            add(Path(apisr_dir) / d / "bin" / "python")
    home = Path.home()
    for root in (".conda", "miniconda3", "anaconda3", "miniforge3", "mambaforge",
                 "micromamba", ".local/share/mamba", "opt/conda"):
        envs = home / root / "envs"
        if envs.is_dir():
            for e in sorted(envs.iterdir()):
                add(e / "bin" / "python")
    cands.sort(key=lambda c: "apisr" not in c.lower())
    add(sys.executable)
    return cands


# --------------------------------------------------------------------------
# GPU compute capability / torch install hint (works without torch)
# --------------------------------------------------------------------------
_cc_cache = {"value": "unset"}


def max_compute_capability():
    """Highest CUDA compute capability reported by the driver, e.g. (12, 0), or None."""
    if _cc_cache["value"] != "unset":
        return _cc_cache["value"]
    best = None
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=6).stdout
            for line in out.splitlines():
                m = re.match(r"\s*(\d+)\.(\d+)", line)
                if m:
                    cc = (int(m.group(1)), int(m.group(2)))
                    best = cc if best is None or cc > best else best
        except Exception:
            pass
    _cc_cache["value"] = best
    return best


def torch_install_hint(devices=None):
    """Pick a PyTorch install command that matches the GPU architecture.

    The old pinned torch 2.1.1+cu118 wheels stop at sm_90, so Blackwell (RTX 50xx,
    compute capability 10.x / 12.x) needs a CUDA 12.8+ build.
    """
    cc = None
    if devices:
        caps = [(d.get("major", 0), d.get("minor", 0)) for d in devices if "major" in d]
        cc = max(caps) if caps else None
    if cc is None:
        cc = max_compute_capability()
    if cc is not None and cc[0] >= 10:
        return TORCH_HINT_BLACKWELL
    return TORCH_HINT_LEGACY


def explain_failure(py, text):
    m = re.search(r"No module named '([^']+)'", text)
    if not m:
        return ""
    mod = m.group(1).split(".")[0]
    if mod in NO_AUTO_INSTALL:
        return (f"The Python used for APISR ({py}) is missing '{mod}'. Install the GPU build of "
                f"PyTorch that matches your GPU into that environment, e.g.:  {torch_install_hint()}")
    return (f"The Python used for APISR ({py}) is missing the package '{mod}'. Click "
            "'Check / install packages' to install everything APISR needs, or run: "
            f"{py} -m pip install {PIP_NAMES.get(mod, mod)}")


DETECT_SNIPPET = r'''
import json, sys
def out(d): print("APISR_JSON:" + json.dumps(d))
try:
    import torch
except Exception as e:
    out({"error": "Could not import torch: %s" % e}); sys.exit(0)
info = {"torch": torch.__version__, "cuda": torch.version.cuda,
        "hip": getattr(torch.version, "hip", None),
        "available": bool(torch.cuda.is_available()), "devices": [], "arch_list": []}
try:
    info["arch_list"] = list(torch.cuda.get_arch_list())
except Exception:
    pass
if info["available"]:
    for i in range(torch.cuda.device_count()):
        p = torch.cuda.get_device_properties(i)
        info["devices"].append({"index": i, "name": p.name,
                                "mem_gb": round(p.total_memory / 1e9, 1),
                                "major": p.major, "minor": p.minor})
out(info)
'''


def setup_nvrtc_env(py, env):
    try:
        py_path = Path(py).resolve()
        prefix = py_path.parent.parent
        search_paths = [
            prefix / "lib",
            prefix / "lib64",
            *prefix.glob("lib/python*/site-packages/nvidia/cuda_nvrtc/lib"),
            *prefix.glob("lib/python*/site-packages/torch/lib"),
            Path("/usr/local/cuda/lib64"),
            Path("/usr/lib64"),
        ]
        extra_paths = []
        for d in search_paths:
            if d.is_dir():
                extra_paths.append(str(d))
                generic = d / "libnvrtc.so"
                if not generic.exists():
                    candidates = [f for f in d.glob("libnvrtc.so.*")
                                  if not f.is_symlink() or f.resolve().exists()]
                    if candidates:
                        try:
                            os.symlink(candidates[0].name, generic)
                        except OSError:
                            pass
        if extra_paths:
            cur = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = os.pathsep.join(extra_paths + ([cur] if cur else []))
    except Exception:
        pass
    return env


def detect_torch(py, env=None):
    env = dict(env if env is not None else os.environ)
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    try:
        r = subprocess.run([py, "-c", DETECT_SNIPPET], env=env,
                           capture_output=True, text=True, timeout=180)
    except Exception as e:
        return {"error": f"Could not run {py}: {e}"}
    for line in r.stdout.splitlines():
        if line.startswith("APISR_JSON:"):
            try:
                res = json.loads(line[len("APISR_JSON:"):])
                if isinstance(res, dict):
                    return res
            except Exception:
                pass
    return {"error": "Detection failed:\n" + (r.stderr or r.stdout)[-1500:]}


def arch_ok(archs, major, minor):
    for a in archs:
        m = re.fullmatch(r"sm_(\d+)", a)
        if m:
            digits = m.group(1)
            if int(digits[:-1]) == major and int(digits[-1]) <= minor:
                return True
    return False


# --------------------------------------------------------------------------
# Package checks
# --------------------------------------------------------------------------
def local_import_closure(apisr_dir, entry="test_code/inference.py"):
    import ast
    root = Path(apisr_dir)
    seen, names, todo = set(), set(), [root / entry]

    def resolve(dotted):
        parts = dotted.split(".")
        found = []
        for i in range(1, len(parts) + 1):
            base = root.joinpath(*parts[:i])
            if base.with_suffix(".py").is_file():
                found.append(base.with_suffix(".py"))
            elif (base / "__init__.py").is_file():
                found.append(base / "__init__.py")
        return found

    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        try:
            tree = ast.parse(f.read_text(errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for m in mods:
                local = resolve(m)
                top = m.split(".")[0]
                if local:
                    todo.extend(local)
                elif (root / top).exists() or (root / (top + ".py")).exists():
                    continue
                else:
                    names.add(top)
    return sorted(names)


def find_missing_modules(py, names):
    code = ("import importlib.util, json, sys\n"
            "miss = []\n"
            "for n in json.loads(sys.argv[1]):\n"
            "    try:\n"
            "        if importlib.util.find_spec(n) is None: miss.append(n)\n"
            "    except Exception: miss.append(n)\n"
            "print('APISR_JSON:' + json.dumps(miss))\n")
    try:
        r = subprocess.run([py, "-c", code, json.dumps(names)],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        return None, f"Could not run {py}: {e}"
    for line in r.stdout.splitlines():
        if line.startswith("APISR_JSON:"):
            return json.loads(line[len("APISR_JSON:"):]), None
    return None, (r.stderr or r.stdout)[-800:]


# --------------------------------------------------------------------------
# GPU monitor: NVML when available (no process spawn), nvidia-smi fallback
# --------------------------------------------------------------------------
class GpuMonitor:
    """Returns (index, util_percent, mem_used_mib) for the selected GPU, or None."""

    def __init__(self):
        self._nvml = None
        self._lock = threading.Lock()
        self._smi_cache = (0.0, [])
        try:
            import pynvml  # provided by the nvidia-ml-py package
            pynvml.nvmlInit()
            self._nvml = pynvml
        except Exception:
            self._nvml = None

    @property
    def backend(self):
        return "NVML" if self._nvml else ("nvidia-smi" if shutil.which("nvidia-smi") else "none")

    def _rows_nvml(self):
        n = self._nvml
        rows = []
        for i in range(n.nvmlDeviceGetCount()):
            h = n.nvmlDeviceGetHandleByIndex(i)
            util = n.nvmlDeviceGetUtilizationRates(h).gpu
            mem = n.nvmlDeviceGetMemoryInfo(h).used // (1024 * 1024)
            rows.append((i, int(util), int(mem)))
        return rows

    def _rows_smi(self):
        now = time.time()
        ts, rows = self._smi_cache
        if now - ts < 3.0:
            return rows
        rows = []
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4).stdout
            for line in out.strip().splitlines():
                try:
                    i, u, m = [x.strip() for x in line.split(",")]
                    rows.append((int(i), int(u), int(m)))
                except ValueError:
                    continue
        except Exception:
            pass
        self._smi_cache = (now, rows)
        return rows

    def stats(self, only_index=None):
        with self._lock:
            try:
                rows = self._rows_nvml() if self._nvml else (
                    self._rows_smi() if shutil.which("nvidia-smi") else [])
            except Exception:
                rows = []
        if only_index is not None:
            rows = [r for r in rows if r[0] == only_index]
        return max(rows, key=lambda r: r[1]) if rows else None


# --------------------------------------------------------------------------
# Memory / disk
# --------------------------------------------------------------------------
def mem_available_bytes():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def shm_free_bytes():
    try:
        return shutil.disk_usage("/dev/shm").free
    except Exception:
        return 0


def free_bytes(path):
    try:
        p = Path(path)
        while not p.exists() and p != p.parent:
            p = p.parent
        return shutil.disk_usage(p).free
    except Exception:
        return 0


FRAME_BYTES_FACTOR = {".bmp": 1.0, ".png": 0.55, ".jpg": 0.12}


def estimate_frame_bytes(width, height, ext):
    return width * height * 3 * FRAME_BYTES_FACTOR.get(ext, 0.55)


def estimate_peak_temp_bytes(src_w, src_h, scale, chunk, ext, overlap_encode=True):
    """Peak temp usage with streaming extraction (2 chunks of sources) plus the upscaled
    frames of the chunk being upscaled and, when encoding is overlapped, the chunk being encoded."""
    src = estimate_frame_bytes(src_w, src_h, ext)
    out = estimate_frame_bytes(src_w * scale, src_h * scale, ".png")
    out_chunks = 2 if overlap_encode else 1
    return int((2 * src + out_chunks * out) * chunk * 1.1)


# --------------------------------------------------------------------------
# Desktop integration (icon + .desktop file so GNOME shows notifications/icon)
# --------------------------------------------------------------------------
def ensure_desktop_integration(logo_svg, launcher_path):
    try:
        ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        icon_path = ICON_CACHE_DIR / f"{APP_ID}.svg"
        if not icon_path.exists() or icon_path.read_text() != logo_svg:
            icon_path.write_text(logo_svg)
    except Exception:
        pass
    try:
        APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
        desktop = APPLICATIONS_DIR / f"{APP_ID}.desktop"
        launcher = str(launcher_path)
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=APISR Upscaler\n"
            "Comment=Anime super-resolution and remastering\n"
            f"Exec=/usr/bin/python3 {launcher}\n"
            f"Icon={APP_ID}\n"
            "Terminal=false\n"
            "Categories=AudioVideo;Video;GTK;\n"
            f"StartupWMClass={APP_ID}\n"
        )
        if not desktop.exists() or desktop.read_text() != content:
            desktop.write_text(content)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Verified downloads (.part + atomic rename + checksum from the hosting API)
# --------------------------------------------------------------------------
UA = {"User-Agent": "apisr-gui/2.0 (+https://github.com/Kiteretsu77/APISR)"}

APISR_RELEASES = "https://github.com/Kiteretsu77/APISR/releases/download"
APISR_WEIGHTS = [
    ("2x_APISR_RRDB_GAN_generator.pth", f"{APISR_RELEASES}/v0.1.0/2x_APISR_RRDB_GAN_generator.pth"),
    ("4x_APISR_GRL_GAN_generator.pth", f"{APISR_RELEASES}/v0.1.0/4x_APISR_GRL_GAN_generator.pth"),
    ("4x_APISR_RRDB_GAN_generator.pth", f"{APISR_RELEASES}/v0.2.0/4x_APISR_RRDB_GAN_generator.pth"),
]
ARNNDN_REPO = "richardpl/arnndn-models"
ARNNDN_FILES = ["sh.rnnn", "bd.rnnn", "cb.rnnn", "std.rnnn"]


def _http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def github_expected_sha256(owner, repo, tag, asset_name):
    """GitHub exposes a sha256 digest for release assets (digest: 'sha256:...')."""
    try:
        data = _http_json(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}")
        for a in data.get("assets", []):
            if a.get("name") == asset_name:
                dig = a.get("digest") or ""
                if dig.startswith("sha256:"):
                    return dig.split(":", 1)[1], a.get("size")
                return None, a.get("size")
    except Exception:
        pass
    return None, None


def github_blob_sha1(owner, repo, path):
    try:
        data = _http_json(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}")
        return data.get("sha")
    except Exception:
        return None


def git_blob_sha1_of_file(path):
    size = os.path.getsize(path)
    h = hashlib.sha1()
    h.update(f"blob {size}\0".encode())
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_verified(url, dest, expected_sha256=None, expected_git_sha1=None,
                      progress=None, log=None):
    """Download to dest.part, verify, then rename. Returns (ok, message)."""
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            got, last = 0, 0.0
            while True:
                buf = resp.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                got += len(buf)
                now = time.time()
                if progress and total and now - last > 0.4:
                    last = now
                    progress(got / total)
        if expected_sha256:
            actual = sha256_of_file(part)
            if actual.lower() != expected_sha256.lower():
                part.unlink(missing_ok=True)
                return False, f"Checksum mismatch (expected {expected_sha256[:12]}..., got {actual[:12]}...)"
            verified = "SHA-256 verified"
        elif expected_git_sha1:
            actual = git_blob_sha1_of_file(part)
            if actual != expected_git_sha1:
                part.unlink(missing_ok=True)
                return False, "Checksum mismatch (git blob SHA-1)"
            verified = "checksum verified"
        else:
            verified = "no checksum published - size/format not verified"
        if part.stat().st_size < 1024:
            part.unlink(missing_ok=True)
            return False, "Downloaded file is suspiciously small"
        os.replace(part, dest)
        return True, verified
    except Exception as e:
        part.unlink(missing_ok=True)
        return False, str(e)


def safe_split(text):
    """shlex.split that raises ValueError with a readable message instead of crashing."""
    import shlex
    text = (text or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError as e:
        raise ValueError(f"Extra CLI args could not be parsed ({e}). Check for unmatched quotes.")


def fmt_time(seconds):
    s = int(seconds)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def escape_markup(text):
    return html.escape(str(text), quote=False)
