"""Processing pipeline. No GTK imports, so it can be unit-tested headless.

Everything that builds an ffmpeg / inference command lives in module-level builder
functions, shared by the real pipeline, the test-frame renderer, the "copy command
lines" feature and the pre-run summary, so they can never drift apart.
"""

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

from .config import (DEFAULT_FRAME_FORMAT, DEVICE_AUTO, IMG_EXTS, MODELS_CACHE_DIR)
from .system import (GpuMonitor, estimate_peak_temp_bytes, explain_failure, fmt_time,
                     free_bytes, mem_available_bytes, safe_split, setup_nvrtc_env,
                     shm_free_bytes, detect_torch)

WORKER_SCRIPT = Path(__file__).resolve().parent / "inference_worker.py"


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# Frame formats
# --------------------------------------------------------------------------
FRAME_FORMATS = {
    "JPEG - near-lossless (4:4:4, ~5x smaller)": {
        "ext": ".jpg",
        "vf": ["format=rgb24", "scale=out_color_matrix=bt601:out_range=pc", "format=yuvj444p"],
        "args": ["-c:v", "mjpeg", "-q:v", "2"]},
    "JPEG - high quality (4:2:0, ~8x smaller)": {
        "ext": ".jpg",
        "vf": ["format=rgb24", "scale=out_color_matrix=bt601:out_range=pc", "format=yuvj420p"],
        "args": ["-c:v", "mjpeg", "-q:v", "3"]},
    "BMP - uncompressed (fastest I/O, large disk)": {
        "ext": ".bmp", "vf": ["format=bgr24"], "args": ["-c:v", "bmp"]},
    "PNG - lossless": {
        "ext": ".png", "vf": [], "args": ["-c:v", "png"]},
    "PNG - lossless, max compression (slower)": {
        "ext": ".png", "vf": [], "args": ["-c:v", "png", "-compression_level", "9"]},
}


def frame_format(cfg):
    return FRAME_FORMATS.get(cfg.get("frame_format"), FRAME_FORMATS[DEFAULT_FRAME_FORMAT])


def scale_for_weight(weight):
    return 2 if "2x" in Path(str(weight)).name.lower() else 4


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------
# Subtitle codecs that Matroska can store as-is, and text codecs that must be converted.
SUB_COPY = {"ass", "ssa", "subrip", "srt", "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
SUB_TO_SRT = {"mov_text", "text", "webvtt", "subviewer", "subviewer1", "microdvd",
              "sami", "realtext", "jacosub", "stl", "mpl2", "pjs", "vplayer"}


def probe_video(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    vids = [s for s in streams if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    if not vids:
        raise RuntimeError("No video stream found in " + str(path))
    s = vids[0]
    fps = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            f = Fraction(s.get(key, "0/0"))
            if f > 0:
                fps = f
                break
        except (ZeroDivisionError, ValueError):
            continue
    if fps is None:
        raise RuntimeError("Could not determine the video's frame rate.")
    sar = None
    try:
        f = Fraction(s.get("sample_aspect_ratio", "1:1").replace(":", "/"))
        if f > 0 and f != 1:
            sar = f
    except (ZeroDivisionError, ValueError):
        pass
    duration = None
    for val in (s.get("duration"), data.get("format", {}).get("duration")):
        try:
            if val not in (None, "N/A"):
                duration = float(val)
                break
        except ValueError:
            pass
    try:
        nb = int(s.get("nb_frames"))
    except (TypeError, ValueError):
        nb = None
    subs = [{"codec": x.get("codec_name", ""), "lang": x.get("tags", {}).get("language", "")}
            for x in streams if x.get("codec_type") == "subtitle"]
    return {"fps": fps, "width": s["width"], "height": s["height"], "sar": sar,
            "duration": duration, "nb_frames": nb, "subs": subs}


# --------------------------------------------------------------------------
# Command builders
# --------------------------------------------------------------------------
def encoder_args(cfg):
    enc, crf, ten = cfg["encoder"], str(cfg["crf"]), cfg["tenbit"]
    is_hdr = cfg.get("hdr10", False)
    if enc == "libx265":
        a = ["-c:v", "libx265", "-crf", crf, "-preset", cfg["preset"]]
        x = ["log-level=error"]
        if is_hdr:
            x += ["hdr10=1", "repeat-headers=1", "colorprim=bt2020", "transfer=smpte2084",
                  "colormatrix=bt2020nc",
                  "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
                  "max-cll=203,50"]
        a += ["-x265-params", ":".join(x)]
        pix = "yuv420p10le" if (ten or is_hdr) else "yuv420p"
    elif enc == "libx264":
        a = ["-c:v", "libx264", "-crf", crf, "-preset", cfg["preset"]]
        pix = "yuv420p10le" if ten else "yuv420p"
    elif enc in ("hevc_nvenc", "h264_nvenc"):
        a = ["-c:v", enc, "-rc", "vbr", "-cq", crf, "-b:v", "0", "-preset", "p5",
             "-spatial_aq", "1", "-temporal_aq", "1", "-aq-strength", "8"]
        pix = "p010le" if (ten or is_hdr or enc == "hevc_nvenc") else "yuv420p"
    else:
        a = ["-c:v", "libsvtav1", "-crf", crf, "-preset", "6"]
        pix = "yuv420p10le" if (ten or is_hdr) else "yuv420p"
    if is_hdr:
        tags = ["-colorspace", "bt2020nc", "-color_primaries", "bt2020",
                "-color_trc", "smpte2084", "-color_range", "tv"]
    else:
        tags = ["-colorspace", "bt709", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-color_range", "tv"]
    return a + ["-pix_fmt", pix] + tags


def validate_cfg(cfg):
    """Return a list of human-readable problems that would make a run fail or be wrong."""
    problems = []
    if cfg.get("hdr10") and cfg["encoder"] in ("libx264", "h264_nvenc"):
        problems.append("HDR10 needs a 10-bit HEVC or AV1 encoder; H.264 cannot carry HDR10 "
                        "metadata properly. Pick hevc_nvenc, libx265 or libsvtav1.")
    try:
        safe_split(cfg.get("extra", ""))
    except ValueError as e:
        problems.append(str(e))
    return problems


def extract_vf(cfg, sar, start_frame, fmt):
    vf = []
    if start_frame:
        vf.append(f"select='gte(n,{start_frame})'")
    if cfg.get("deinterlace", False):
        # Only touches frames that are flagged as interlaced.
        vf.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
    if cfg.get("derainbow", False):
        # Chroma-only cleanup: reduces rainbow / chroma noise without softening line art.
        vf.append("chromanr=thres=40:sizew=7:sizeh=7")
        vf.append("hqdn3d=0:3:0:4")
    if sar:
        vf.append("scale=trunc(iw*sar/2)*2:ih,setsar=1")
    vf += fmt["vf"]
    return vf


def extract_cmd(cfg, video, frames_pattern, fmt, sar, start_frame, test_seconds):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
    if cfg.get("nvdec", True) and shutil.which("nvidia-smi"):
        cmd += ["-hwaccel", "cuda"]
    if test_seconds:
        cmd += ["-t", str(test_seconds)]
    cmd += ["-i", video, "-map", "0:v:0", "-fps_mode", "passthrough"]
    vf = extract_vf(cfg, sar, start_frame, fmt)
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += fmt["args"]
    if start_frame:
        cmd += ["-start_number", str(start_frame + 1)]
    cmd += [frames_pattern]
    return cmd


def inference_cmd(cfg, in_dir, out_dir, extra, apisr_dir):
    w = str(cfg["weight"])
    cmd = [cfg["python"], "-u", "test_code/inference.py",
           "--input_dir", str(in_dir), "--weight_path", w, "--store_dir", str(out_dir)]
    if not any(a.startswith("--scale") for a in extra):
        cmd += ["--scale", str(scale_for_weight(w))]
    if cfg.get("fp16", True) and not any(a.startswith("--float16") for a in extra):
        cmd += ["--float16_inference", "True"]
    if cfg.get("tensorrt", False):
        inf = Path(apisr_dir) / "test_code" / "inference.py"
        try:
            if "--tensorrt" in inf.read_text(errors="ignore"):
                cmd += ["--tensorrt", "True"]
        except Exception:
            pass
    return cmd + list(extra)


def find_rife():
    exe = shutil.which("rife-ncnn-vulkan")
    if not exe:
        for p in (Path.home() / ".local" / "bin" / "rife-ncnn-vulkan",
                  Path.home() / "rife-ncnn-vulkan" / "rife-ncnn-vulkan"):
            if p.is_file() and os.access(p, os.X_OK):
                exe = str(p)
                break
    if not exe:
        return None, None
    exe_dir = Path(exe).resolve().parent
    models = sorted(d.name for d in exe_dir.iterdir() if d.is_dir() and d.name.startswith("rife"))
    return exe, (models[-1] if models else None)


def motion_plan(cfg, fps):
    """Frame-interpolation plan {factor, target, method} or None."""
    opt = cfg.get("rife_fps", "Off")
    if opt == "Off":
        return None
    tgt = 60 if "60" in opt else 48
    if float(fps) >= tgt * 0.9:
        return None
    factor = Fraction(max(round(2 * tgt / float(fps)), 3), 2)  # nearest 0.5x step, >= 1.5x
    exe, model = find_rife()
    return {"factor": factor, "target": fps * factor,
            "method": "rife" if exe else "minterpolate"}


def output_vf(cfg, height, target_fps=None, plan=None, preview=False):
    """Filter chain applied to upscaled frames before encoding."""
    is_hdr = cfg.get("hdr10", False) and not preview
    use_cuda = cfg.get("cuda_filters", False) and not is_hdr and not preview
    ten = cfg["tenbit"] or is_hdr
    vfs = []
    if preview:
        if height != "keep":
            vfs.append(f"scale=-2:{height}:flags=lanczos")
        vfs.append("format=yuv444p")
    elif use_cuda:
        in_pix = "p010le" if ten else "nv12"
        vfs += [f"format={in_pix}", "hwupload_cuda"]
        vfs.append(f"scale_cuda=-2:{height}:interp_algo=lanczos:format={in_pix}"
                   if height != "keep" else f"scale_cuda=format={in_pix}")
        vfs += ["hwdownload", f"format={in_pix}"]
    else:
        # In HDR mode the matrix stays BT.709 here; zscale does the real conversion below.
        if height != "keep":
            vfs.append(f"scale=-2:{height}:flags=lanczos:out_color_matrix=bt709:out_range=tv")
        else:
            vfs.append("scale=iw:ih:out_color_matrix=bt709:out_range=tv")
    if not use_cuda:
        if cfg.get("deband", True):
            vfs.append("deband=1thr=0.04:2thr=0.04:3thr=0.04:range=16:blur=true")
        if cfg.get("sharpen", True):
            vfs.append("unsharp=5:5:0.5:5:5:0.0")
    if plan and plan["method"] == "minterpolate" and not preview:
        t = plan["target"]
        vfs.append(f"minterpolate=fps={t.numerator}/{t.denominator}:mi_mode=mci:mc_mode=aobmc:vsbmc=1")
        # minterpolate drops a few frames at the end of every chunk; pad by repeating the
        # last frame and cap the count in the encoder so A/V sync cannot drift across chunks.
        vfs.append("tpad=stop_mode=clone:stop=8")
    if cfg.get("grain", False) and not use_cuda:
        vfs.append("noise=alls=6:allf=t+u")
    if is_hdr:
        # SDR (BT.709, 203-nit reference white) -> PQ / BT.2020, done in linear light.
        # Explicit properties at every stage: zimg refuses frames whose matrix/primaries
        # are "unspecified" ("no path between colorspaces"), which happens right after
        # the RGB float conversion.
        vfs += ["zscale=tin=bt709:min=bt709:pin=bt709:rin=limited:t=linear:npl=203:p=bt709",
                "format=gbrpf32le", "zscale=p=bt2020:t=linear:m=gbr",
                "zscale=t=smpte2084:p=bt2020:m=bt2020nc:r=limited", "format=yuv420p10le"]
    if preview:
        vfs.append("format=rgb24")
    return ",".join(vfs)


def sub_plan(subs):
    """-> (map_args, codec_args, notes) for subtitle streams going into Matroska."""
    maps, codecs, notes = [], [], []
    out_i = 0
    for i, s in enumerate(subs):
        c = s["codec"]
        if c in SUB_COPY:
            maps += ["-map", f"1:s:{i}"]
            codecs += [f"-c:s:{out_i}", "copy"]
            out_i += 1
        elif c in SUB_TO_SRT:
            maps += ["-map", f"1:s:{i}"]
            codecs += [f"-c:s:{out_i}", "srt"]
            out_i += 1
        else:
            notes.append(f"NOTE: dropped subtitle track {i} ({c or 'unknown'}): not storable in Matroska.")
    return maps, codecs, notes


def mux_cmd(cfg, listfile, video, final, info, test_seconds, log=None):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
           "-f", "concat", "-safe", "0", "-i", listfile]
    if test_seconds:
        cmd += ["-t", str(test_seconds)]
    cmd += ["-i", video, "-map", "0:v:0", "-map", "1:a?"]
    maps, codecs, notes = sub_plan(info.get("subs", []))
    if log:
        for n in notes:
            log(n)
    cmd += maps + ["-map", "1:t?", "-map_chapters", "1", "-c:v", "copy"] + codecs
    cmd += ["-map_metadata", "1"]
    if cfg.get("ai_audio", False):
        if log:
            log("Applying AI dialogue clarity and vocal dynamic normalization...")
        af = ["highpass=f=75", "lowpass=f=12500", "equalizer=f=3000:t=q:w=1.5:g=3.5",
              "dynaudnorm=p=0.9:m=10.0"]
        model = MODELS_CACHE_DIR / "sh.rnnn"
        if not model.exists():
            model = MODELS_CACHE_DIR / "bd.rnnn"
        if model.exists():
            af.insert(0, f"arnndn=m='{model.as_posix()}'")
        cmd += ["-af", ",".join(af), "-c:a", "libopus", "-b:a", "192k"]
    else:
        cmd += ["-c:a", "copy"]
    if test_seconds:
        cmd += ["-shortest", "-max_interleave_delta", "100M"]
    return cmd + [final]


def settings_fingerprint(cfg, video, info, fext, chunk, scale, plan):
    """Hash of everything that changes the content of an encoded chunk."""
    try:
        st = os.stat(video)
        src = [str(video), st.st_size, st.st_mtime_ns]
    except OSError:
        src = [str(video)]
    w = Path(str(cfg["weight"]))
    keys = ["height", "encoder", "crf", "preset", "tenbit", "hdr10", "deband", "sharpen",
            "grain", "derainbow", "deinterlace", "cuda_filters", "fp16", "extra",
            "frame_format", "test_seconds", "rife_fps"]
    blob = {
        "src": src, "chunk": chunk, "scale": scale, "ext": fext, "weight": w.name,
        "weight_size": w.stat().st_size if w.exists() else 0,
        "sar": str(info.get("sar")), "fps": str(info.get("fps")),
        "plan": (str(plan["factor"]), plan["method"]) if plan else None,
        **{k: cfg.get(k) for k in keys},
    }
    return hashlib.sha1(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()


# --------------------------------------------------------------------------
# Environment helpers
# --------------------------------------------------------------------------
def device_mode(cfg):
    label = cfg.get("device", DEVICE_AUTO)
    m = re.match(r"GPU (\d+)", label)
    if m:
        return "gpu", int(m.group(1))
    if label.startswith("CPU"):
        return "cpu", None
    return "auto", None


def apply_device(cfg, env, say=None):
    mode, idx = device_mode(cfg)
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
    env["PYTHONUNBUFFERED"] = "1"
    setup_nvrtc_env(cfg["python"], env)
    if mode == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = "-1"
        env["HIP_VISIBLE_DEVICES"] = "-1"
        if say:
            say("Device: CPU only (GPUs hidden from PyTorch)")
    elif mode == "gpu":
        env["CUDA_VISIBLE_DEVICES"] = str(idx)
        env["HIP_VISIBLE_DEVICES"] = str(idx)
        if say:
            say(f"Device: GPU {idx} (CUDA_VISIBLE_DEVICES={idx})")
    elif say:
        say("Device: auto (PyTorch default)")
    return env


def make_env(cfg, say=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(cfg["apisr_dir"]) + os.pathsep + env.get("PYTHONPATH", "")
    return apply_device(cfg, env, say)


def bench_key(cfg):
    return f"{Path(str(cfg['weight'])).name}|{'fp16' if cfg.get('fp16', True) else 'fp32'}|{cfg.get('device')}"


# --------------------------------------------------------------------------
# Frame extractor (streaming, throttled with SIGSTOP/SIGCONT)
# --------------------------------------------------------------------------
class Extractor:
    def __init__(self, cmd, start_frame, limit, log, pattern):
        self.pattern = pattern
        self.start = start_frame
        self.frames_out = start_frame
        self.limit = limit
        self.paused = False
        self.finished = False
        self.returncode = None
        self.stopping = False
        self.tail = deque(maxlen=15)
        self.t0 = time.time()
        self.log = log
        self.proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._throttle, daemon=True).start()

    def _line(self, raw):
        text = raw.decode(errors="replace").strip()
        if not text:
            return
        m = re.match(r"frame=\s*(\d+)", text)
        if m:
            self.frames_out = self.start + int(m.group(1))
        elif re.match(r"(fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|"
                      r"out_time|dup_frames|drop_frames|speed|progress)=", text):
            return
        else:
            self.tail.append(text)
            self.log(text)

    def _read(self):
        buf = b""
        while True:
            data = self.proc.stdout.read1(4096)
            if not data:
                break
            buf += data
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                self._line(part)
        if buf:
            self._line(buf)
        self.returncode = self.proc.wait()
        self.finished = True

    def _signal(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _throttle(self):
        while not self.finished:
            lim = self.limit
            if not self.stopping and lim is not None:
                reached = os.path.exists(self.pattern % lim)
                if reached and not self.paused:
                    self._signal(signal.SIGSTOP)
                    self.paused = True
                elif not reached and self.paused:
                    self._signal(signal.SIGCONT)
                    self.paused = False
            time.sleep(0.05)

    def stop(self):
        self.stopping = True
        if self.proc.poll() is None:
            self._signal(signal.SIGCONT)
            self.paused = False
            self._signal(signal.SIGTERM)


# --------------------------------------------------------------------------
# Persistent inference worker client (experimental)
# --------------------------------------------------------------------------
class WorkerClient:
    def __init__(self, pipeline, cfg, env, apisr_dir):
        self.pl = pipeline
        w = str(cfg["weight"])
        cmd = [cfg["python"], "-u", str(WORKER_SCRIPT), str(apisr_dir), w,
               str(scale_for_weight(w)), "1" if cfg.get("fp16", True) else "0"]
        self.proc = subprocess.Popen(cmd, cwd=apisr_dir, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, start_new_session=True)
        pipeline._register(self.proc)
        self.lines = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            if line.startswith(("READY", "DONE", "ERR")):
                self.lines.put(line)
            elif line:
                self.pl.log(line)
        self.lines.put("EOF")

    def _wait(self, timeout, monitor=None):
        t0 = time.time()
        last_mon = 0.0
        while True:
            self.pl.check_cancel()
            try:
                return self.lines.get(timeout=0.3)
            except queue.Empty:
                pass
            now = time.time()
            if monitor and now - last_mon > 1.5:
                last_mon = now
                try:
                    monitor()
                except Exception:
                    pass
            if timeout and now - t0 > timeout:
                return "ERR timeout"

    def start(self):
        res = self._wait(600)
        return res == "READY", res

    def upscale_dir(self, in_dir, out_dir, monitor):
        self.proc.stdin.write(f"{in_dir}\t{out_dir}\n")
        self.proc.stdin.flush()
        res = self._wait(0, monitor)
        if not res.startswith("DONE"):
            raise RuntimeError("Persistent worker failed: " + res)

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.stdin.write("QUIT\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
        except Exception:
            pass
        try:
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGTERM)
        except Exception:
            pass
        self.pl._unregister(self.proc)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------
class Pipeline:
    def __init__(self, cfg, emit):
        self.cfg = cfg
        self.emit = emit
        self.cancel_flag = threading.Event()
        self._procs = set()
        self._proc_lock = threading.Lock()
        self.t0 = None
        self.base = 0
        self.gpu_text = ""
        self.low_gpu = 0
        self.warned_idle = False
        self.extractor = None
        self.work_path = Path(".")
        self._preview_emitted = False
        self.gpu = GpuMonitor()
        self.avg_fps = None

    # -- bookkeeping --------------------------------------------------
    def log(self, msg):
        self.emit("log", msg)

    def _register(self, p):
        with self._proc_lock:
            self._procs.add(p)

    def _unregister(self, p):
        with self._proc_lock:
            self._procs.discard(p)

    def check_cancel(self):
        if self.cancel_flag.is_set():
            raise Cancelled()

    def cancel(self):
        self.cancel_flag.set()
        if self.extractor:
            self.extractor.stop()
        with self._proc_lock:
            procs = list(self._procs)
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass

    def device_mode(self):
        return device_mode(self.cfg)

    def gpu_stats(self):
        mode, idx = self.device_mode()
        if mode == "cpu" or not self.cfg.get("gpu_stats"):
            return None
        return self.gpu.stats(idx if mode == "gpu" else None)

    # -- subprocess runner (thread-safe; several can run at once) -------
    def run(self, cmd, cwd=None, env=None, monitor=None, filter_progress=False):
        self.check_cancel()
        self.log("$ " + " ".join(shlex.quote(str(c)) for c in cmd))
        proc = subprocess.Popen([str(c) for c in cmd], cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
        self._register(proc)
        stop = threading.Event()
        if monitor:
            def mon():
                while not stop.wait(1.5):
                    try:
                        monitor()
                    except Exception:
                        pass
            threading.Thread(target=mon, daemon=True).start()
        tail = deque(maxlen=15)

        def handle(raw):
            text = raw.decode(errors="replace").strip()
            if not text:
                return
            if "install nvrtc.so" in text or "Applied workaround for CuDNN issue" in text:
                return
            if filter_progress and ("it/s" in text or "s/it" in text or "%|" in text):
                return
            tail.append(text)
            self.log(text)

        try:
            buf = b""
            while True:
                data = proc.stdout.read1(4096)
                if not data:
                    break
                buf += data
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for part in parts:
                    handle(part)
            if buf:
                handle(buf)
            rc = proc.wait()
        finally:
            stop.set()
            self._unregister(proc)
        self.check_cancel()
        if rc != 0:
            msg = f"Command failed (exit code {rc}): {Path(str(cmd[0])).name}"
            last = "\n".join(tail)
            if last:
                msg += "\n" + last
            hint = explain_failure(cmd[0], last)
            if hint:
                msg += "\n\n" + hint
            raise RuntimeError(msg)

    # -- preflight ----------------------------------------------------
    def preflight(self):
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                raise RuntimeError(
                    f"{tool} not found. On Fedora: enable RPM Fusion, then "
                    "'sudo dnf swap ffmpeg-free ffmpeg --allowerasing'.")
        problems = validate_cfg(self.cfg)
        if problems:
            raise RuntimeError("\n".join(problems))
        enc_list = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                  capture_output=True, text=True).stdout
        if self.cfg["encoder"] not in enc_list:
            raise RuntimeError(
                f"Your ffmpeg build doesn't have the '{self.cfg['encoder']}' encoder. "
                "Pick another encoder (or install the full ffmpeg from RPM Fusion).")
        if self.cfg.get("hdr10") or self.cfg.get("derainbow"):
            filt = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                                  capture_output=True, text=True).stdout
            if self.cfg.get("hdr10") and " zscale " not in filt:
                raise RuntimeError("HDR10 expansion needs ffmpeg built with libzimg (zscale).")
            if self.cfg.get("derainbow") and " chromanr " not in filt:
                raise RuntimeError("Chroma cleanup needs an ffmpeg with the chromanr filter (ffmpeg 5.0+).")

        self.log(f"Checking that APISR starts with: {self.cfg['python']}")
        apisr = Path(self.cfg["apisr_dir"])
        env = make_env(self.cfg)
        try:
            r = subprocess.run([self.cfg["python"], "test_code/inference.py", "--help"],
                               cwd=apisr, env=env, capture_output=True, text=True, timeout=240)
        except Exception as e:
            raise RuntimeError(f"Could not run {self.cfg['python']}: {e}")
        if r.returncode != 0:
            last = "\n".join((r.stdout + r.stderr).strip().splitlines()[-8:])
            raise RuntimeError(f"APISR failed to start with {self.cfg['python']}:\n{last}\n\n"
                               + explain_failure(self.cfg["python"], last))

        mode, idx = self.device_mode()
        if mode != "cpu":
            self.log("Checking PyTorch / GPU availability...")
            info = detect_torch(self.cfg["python"], make_env(self.cfg))
            if "error" in info:
                raise RuntimeError(info["error"])
            if info.get("available", False):
                names = ", ".join(d["name"] for d in info.get("devices", []))
                self.log(f"PyTorch {info.get('torch', 'unknown')} can use: {names}")
            elif mode == "gpu":
                raise RuntimeError(
                    f"GPU {idx} was selected, but PyTorch can't see it (PyTorch {info.get('torch', 'unknown')}). "
                    "Click 'Detect devices' for a diagnosis.")
            else:
                self.log(f"WARNING: PyTorch {info.get('torch', 'unknown')} reports no usable GPU - APISR "
                         "will run on the CPU (very slow). Click 'Detect devices' for a diagnosis.")

    # -- progress -----------------------------------------------------
    def _free_gb(self):
        return free_bytes(self.work_path) / 1e9

    def _emit_progress(self, pct, title, fps=None, eta=None, extra=""):
        self.emit("progress", {"pct": pct, "title": title, "fps": fps, "eta": eta,
                               "gpu": self.gpu_text, "disk": self._free_gb(), "extra": extra})

    def _progress(self, done, total, vidx, vn, extra=""):
        rate = 0.0
        if self.t0 is not None:
            el = time.time() - self.t0
            gained = done - self.base
            if el > 5 and gained > 0:
                rate = gained / el
                self.avg_fps = rate
        eta = fmt_time((total - done) / rate) if rate else None
        pct = min(100.0 * done / max(total, 1), 100.0)
        self._emit_progress(pct, f"Video {vidx}/{vn}  ·  {done:,} / {total:,} frames",
                            rate, eta, extra)

    def count_frames(self, path):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                 "-show_entries", "stream=nb_read_packets", "-of", "default=nw=1:nk=1",
                 str(path)], capture_output=True, text=True, timeout=900).stdout.strip()
            return int(out.splitlines()[0]) if out else None
        except Exception:
            return None

    @staticmethod
    def _dir_avg_size(paths, n=40):
        paths = list(paths)[:n]
        sizes = [os.path.getsize(p) for p in paths if os.path.exists(p)]
        return sum(sizes) / len(sizes) if sizes else 0

    # -- the main job ---------------------------------------------------
    def process(self, video, vidx, vn):
        cfg = self.cfg
        video = Path(video)
        self.log(f"\n=== [{vidx}/{vn}] {video.name} ===")
        info = probe_video(video)
        fps, sar = info["fps"], info["sar"]
        self.log(f"Source: {info['width']}x{info['height']} @ {float(fps):.3f} fps"
                 + (f", SAR {sar}" if sar else ""))

        test = int(cfg["test_seconds"])
        height = cfg["height"]
        out_dir = Path(cfg["out_dir"]) if cfg["out_dir"] else video.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = "APISR" + ("" if height == "keep" else f"_{height}p") + ("_TEST" if test else "")
        final = out_dir / f"{video.stem}_{tag}.mkv"

        fmt = frame_format(cfg)
        fext = fmt["ext"]
        size = max(int(cfg["chunk"]), 10)
        scale = scale_for_weight(cfg["weight"])

        # --- choose where temporary files live -----------------------------
        work_root = Path(cfg["work_dir"]) if cfg.get("work_dir") else out_dir
        if cfg.get("ramdisk", False) and os.path.exists("/dev/shm"):
            need = estimate_peak_temp_bytes(info["width"], info["height"], scale, size, fext)
            usable = min(shm_free_bytes(), max(mem_available_bytes() - 2_000_000_000, 0))
            if need < usable:
                work_root = Path("/dev/shm")
                self.log(f"RAM-disk ingestion: using /dev/shm (needs ~{need / 1e9:.1f} GB, "
                         f"{usable / 1e9:.1f} GB usable).")
            else:
                self.log(f"NOTE: RAM-disk skipped: this chunk size needs ~{need / 1e9:.1f} GB but only "
                         f"{usable / 1e9:.1f} GB of RAM can be used safely. Lower the chunk size to use it.")
        work = (work_root / f"apisr_work_{video.stem}{'_test' if test else ''}").resolve()
        work.mkdir(parents=True, exist_ok=True)
        self.work_path = work
        self.log(f"Frames / temp files are stored in: {work}")
        self.log(f"Free space on that drive: {self._free_gb():.1f} GB")
        self.emit("workdir", str(work))

        # --- frame counting / VFR ----------------------------------------
        est_frames = info["nb_frames"]
        if not est_frames:
            self._emit_progress(None, f"Video {vidx}/{vn}  ·  counting frames…")
            est_frames = self.count_frames(video)
        if not est_frames and info["duration"]:
            est_frames = int(info["duration"] * float(fps))
        full_frames = est_frames
        if test and est_frames:
            est_frames = min(est_frames, int(test * float(fps)))
        if est_frames:
            self.log(f"Frames to process: ~{est_frames:,}")
        if not test and info["duration"] and full_frames:
            actual = full_frames / info["duration"]
            if abs(actual - float(fps)) / float(fps) > 0.02:
                new_fps = Fraction(actual).limit_denominator(1001)
                self.log(f"NOTE: Variable frame rate detected ({actual:.3f} fps). Using {float(new_fps):.3f} fps.")
                fps = new_fps
                info["fps"] = fps

        # --- interpolation plan -------------------------------------------
        plan = motion_plan(cfg, fps)
        if cfg.get("rife_fps", "Off") != "Off" and plan is None:
            self.log("NOTE: source frame rate is already at or above the smoothing target; skipping interpolation.")
        if plan:
            if plan["method"] == "rife":
                exe, model = find_rife()
                self.log(f"Frame interpolation: RIFE ({Path(exe).name}, model {model}) "
                         f"{float(fps):.3f} -> {float(plan['target']):.3f} fps")
            else:
                self.log("NOTE: rife-ncnn-vulkan not found; using ffmpeg's minterpolate "
                         "(real interpolation, but CPU-bound and lower quality). Put "
                         "rife-ncnn-vulkan on PATH for AI interpolation.")

        # --- filter notes ----------------------------------------------------
        if cfg.get("hdr10") and cfg.get("cuda_filters"):
            self.log("NOTE: HDR10 expansion needs the CPU zscale path; CUDA video filters are off for this run.")
        elif cfg.get("cuda_filters"):
            skipped = [n for n, k in (("deband", "deband"), ("sharpen", "sharpen"), ("grain", "grain"))
                       if cfg.get(k)]
            if skipped:
                self.log("NOTE: CUDA filters have no equivalent for: " + ", ".join(skipped) + " (skipped).")
        if cfg.get("hdr10"):
            self.log("NOTE: SDR->HDR10 expansion is experimental: it maps SDR white to 203 nits in PQ/BT.2020. "
                     "It does not invent real highlight detail.")

        # --- resume safety: settings fingerprint -----------------------------
        fp = settings_fingerprint(cfg, video, info, fext, size, scale, plan)
        state_file = work / "state.json"
        state_ok = False
        if state_file.exists():
            try:
                state_ok = json.loads(state_file.read_text()).get("fingerprint") == fp
            except Exception:
                state_ok = False
        has_old = any(work.glob("chunk_*.done"))
        if (state_file.exists() and not state_ok) or (has_old and not state_file.exists()):
            self.log("Settings changed since the last run (or state is missing): discarding old chunks "
                     "and starting this video fresh so the result is consistent.")
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"fingerprint": fp, "src": str(video), "test": test,
                                          "ext": fext, "chunk": size}))

        saved_total = None
        if (work / "total.txt").exists():
            try:
                saved_total = int((work / "total.txt").read_text())
            except Exception:
                saved_total = None
        done_chunks = 0
        while ((work / f"chunk_{done_chunks:04d}.done").exists()
               and (work / f"chunk_{done_chunks:04d}.mkv").exists()):
            done_chunks += 1
        if done_chunks > 0:
            self.log(f"Resuming: found {done_chunks} finished chunk(s). Continuing from chunk {done_chunks + 1}...")

        frames_in = work / "frames_in"
        frames_in.mkdir(parents=True, exist_ok=True)
        start_frame = done_chunks * size

        apisr = Path(cfg["apisr_dir"])
        env = make_env(cfg, self.log)
        extra = safe_split(cfg["extra"])
        self.gpu_text, self.low_gpu, self.warned_idle = "", 0, False
        self._preview_emitted = False

        ex_cmd = extract_cmd(cfg, video, str(frames_in / f"%06d{fext}"), fmt, sar, start_frame, test)
        self.log("Streaming mode: frames are extracted just ahead of the upscaler, so only "
                 f"about 2 chunks ({size * 2:,} frames) of source frames are on disk at any time. "
                 "Encoding of finished chunks overlaps with upscaling of the next one.")

        vf_out = output_vf(cfg, height, plan=plan)
        enc_fps = plan["target"] if plan and plan["method"] == "rife" else fps

        ex = Extractor(ex_cmd, start_frame, (done_chunks + 2) * size, self.log,
                       str(frames_in / f"%06d{fext}"))
        self.extractor = ex
        st = {"total": None}
        done_frames = start_frame
        self.t0, self.base = None, start_frame
        measured = False
        ci = done_chunks

        # Optional persistent worker
        worker = None
        if cfg.get("persistent_worker", False) and not cfg.get("tensorrt", False):
            try:
                self.log("Starting persistent inference worker (experimental)...")
                worker = WorkerClient(self, cfg, env, apisr)
                ok, msg = worker.start()
                if not ok:
                    self.log(f"NOTE: persistent worker unavailable ({msg}); using one process per chunk.")
                    worker.close()
                    worker = None
                else:
                    self.log("Persistent worker ready: model stays loaded between chunks.")
            except Cancelled:
                raise
            except Exception as e:
                self.log(f"NOTE: persistent worker failed to start ({e}); using one process per chunk.")
                worker = None

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="encode")
        pending = {"fut": None, "ci": None}

        def raise_if_failed(wait=False):
            f = pending["fut"]
            if f is None:
                return
            if wait or f.done():
                pending["fut"] = None
                f.result()

        def ex_state():
            if ex.finished:
                return "extraction finished"
            return f"extracted {ex.frames_out:,}" + (" (buffer full, paused)" if ex.paused else "")

        def total_est(done):
            if st["total"] is not None:
                return st["total"]
            return max(est_frames or 0, ex.frames_out, done + 1)

        def last_frame_index():
            best = 0
            with os.scandir(frames_in) as it:
                for e in it:
                    try:
                        best = max(best, int(Path(e.name).stem))
                    except ValueError:
                        pass
            return best

        def wait_chunk(a, b):
            while True:
                self.check_cancel()
                raise_if_failed()
                if (frames_in / f"{b + 1:06d}{fext}").exists():
                    return b
                if ex.finished:
                    self.check_cancel()
                    if ex.returncode != 0:
                        raise RuntimeError("Frame extraction failed:\n" + "\n".join(ex.tail))
                    last = last_frame_index()
                    if last >= a:
                        st["total"] = last
                        return min(b, last)
                    if st["total"] is None:
                        st["total"] = saved_total or (a - 1)
                    return None
                have = max(ex.frames_out - (a - 1), 0)
                rate = (ex.frames_out - ex.start) / max(time.time() - ex.t0, 0.001)
                self._emit_progress(
                    min(100.0 * have / size, 99.0),
                    f"Video {vidx}/{vn}  ·  extracting frames for chunk {ci + 1}",
                    rate, None, f"{min(have, size):,} / {size:,} frames of this chunk ready")
                time.sleep(0.3)

        def link_or_copy(src, dst):
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

        def encode_job(idx, n_frames, seq_dir, oext, cleanup_dirs, chunk_vid, chunk_flag, label):
            self.log(f"Downscaling + encoding chunk {idx + 1} ({n_frames} frames)...")
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats"]
            if "hwupload_cuda" in vf_out:
                cmd += ["-init_hw_device", "cuda"]
            limit = ["-frames:v", str(int(round(n_frames * plan["factor"])))] \
                if plan and plan["method"] == "minterpolate" else []
            cmd += ["-framerate", str(enc_fps), "-i", str(seq_dir / f"%06d{oext}"),
                    "-vf", vf_out] + encoder_args(cfg) + limit + [str(chunk_vid)]
            self.run(cmd)
            chunk_flag.write_text("ok")
            for d in cleanup_dirs:
                shutil.rmtree(d, ignore_errors=True)

        try:
            while True:
                self.check_cancel()
                raise_if_failed()
                a = ci * size + 1
                b = a + size - 1
                ex.limit = (ci + 2) * size
                bb = wait_chunk(a, b)
                if bb is None:
                    break
                if bb < b:
                    st["total"] = bb
                names = [f"{k:06d}{fext}" for k in range(a, bb + 1)]
                chunk_vid = work / f"chunk_{ci:04d}.mkv"
                chunk_flag = work / f"chunk_{ci:04d}.done"
                if self.t0 is None:
                    self.t0 = time.time()

                cin = work / "chunk_in"
                cout = work / f"chunk_out_{ci:04d}"
                seq = work / f"chunk_seq_{ci:04d}"
                cin.mkdir(parents=True, exist_ok=True)
                cout.mkdir(parents=True, exist_ok=True)
                shutil.rmtree(seq, ignore_errors=True)
                seq.mkdir(parents=True, exist_ok=True)

                to_process = []
                for n in names:
                    out_img = cout / (Path(n).stem + ".bmp")
                    if not out_img.exists():
                        out_img = cout / (Path(n).stem + ".png")
                    if not out_img.exists() or out_img.stat().st_size < 1024:
                        to_process.append(n)
                shutil.rmtree(cin, ignore_errors=True)
                cin.mkdir(parents=True, exist_ok=True)
                for n in to_process:
                    link_or_copy(frames_in / n, cin / n)

                def monitor(cout=cout, cin=cin, n=len(names), base=done_frames):
                    outs_now = sorted(p for p in cout.iterdir() if p.suffix.lower() in IMG_EXTS)
                    count = len(outs_now)
                    stats = self.gpu_stats()
                    if stats:
                        gi, util, mem = stats
                        self.gpu_text = f"GPU {gi}: {util}% | {mem / 1024:.1f} GB"
                        if count > 0 and util < 5:
                            self.low_gpu += 1
                            if self.low_gpu >= 5 and not self.warned_idle:
                                self.warned_idle = True
                                self.log("WARNING: GPU idle during upscale.")
                        else:
                            self.low_gpu = 0
                    d = base + min(count, n)
                    pend = " | encoding previous chunk" if (pending["fut"] and not pending["fut"].done()) else ""
                    self._progress(d, total_est(d), vidx, vn, ex_state() + pend)
                    if outs_now and not self._preview_emitted:
                        out_sample = outs_now[0]
                        if out_sample.stat().st_size > 1024:
                            stem = out_sample.stem
                            in_cand = None
                            for folder in (cin, frames_in):
                                for ext in (fext, ".bmp", ".png", ".jpg", ".jpeg"):
                                    cand = folder / f"{stem}{ext}"
                                    if cand.exists() and cand.stat().st_size > 1024:
                                        in_cand = cand
                                        break
                                if in_cand:
                                    break
                            if in_cand:
                                self._preview_emitted = True
                                self.emit("preview", (str(in_cand), str(out_sample)))

                if to_process:
                    done_already = len(names) - len(to_process)
                    if done_already:
                        self.log(f"\n-- Chunk {ci + 1}: resuming ({done_already} frames already done, "
                                 f"{len(to_process)} remaining) --")
                    else:
                        self.log(f"\n-- Chunk {ci + 1}: frames {a}-{bb} --")
                    self._progress(done_frames, total_est(done_frames), vidx, vn, ex_state())
                    if worker:
                        self.log(f"(persistent worker) upscaling {len(to_process)} frames")
                        worker.upscale_dir(cin, cout, monitor)
                    else:
                        cmd = inference_cmd(cfg, cin, cout, extra, apisr)
                        self.run(cmd, cwd=apisr, env=env, monitor=monitor, filter_progress=True)
                else:
                    self.log(f"\n-- Chunk {ci + 1}: all {len(names)} frames already upscaled, proceeding to encoding --")

                outs = sorted(p for p in cout.iterdir() if p.suffix.lower() in IMG_EXTS)
                if len(outs) != len(names):
                    raise RuntimeError(f"APISR produced {len(outs)} frames but {len(names)} were expected.")
                oext = outs[0].suffix

                cleanup = [cout, seq]
                if plan and plan["method"] == "rife":
                    # Interpolate on the GPU in this thread (sequential with upscaling),
                    # then hand the result to the background encoder.
                    exe, model = find_rife()
                    rife_dir = work / f"chunk_rife_{ci:04d}"
                    shutil.rmtree(rife_dir, ignore_errors=True)
                    rife_dir.mkdir(parents=True)
                    n_out = int(round(len(outs) * plan["factor"]))
                    rcmd = [exe, "-i", str(cout), "-o", str(rife_dir), "-n", str(n_out)]
                    if model:
                        rcmd += ["-m", model]
                    self.run(rcmd, cwd=str(Path(exe).resolve().parent))
                    rife_outs = sorted(p for p in rife_dir.iterdir() if p.suffix.lower() in IMG_EXTS)
                    if not rife_outs:
                        raise RuntimeError("RIFE produced no frames.")
                    for k, p in enumerate(rife_outs, 1):
                        os.symlink(p.resolve(), seq / f"{k:06d}{p.suffix}")
                    oext_seq, n_enc = rife_outs[0].suffix, len(rife_outs)
                    cleanup.append(rife_dir)
                else:
                    for k, p in enumerate(outs, 1):
                        os.symlink(p.resolve(), seq / f"{k:06d}{oext}")
                    oext_seq, n_enc = oext, len(outs)

                if not measured:
                    measured = True
                    src_b = self._dir_avg_size(frames_in / n for n in names)
                    out_b = self._dir_avg_size(outs)
                    peak = (2.3 * src_b + 2 * out_b) * size / 1e9
                    self.log(f"Measured: ~{src_b / 1e3:.0f} KB/frame src, ~{out_b / 1e3:.0f} KB/frame out. "
                             f"Peak temp disk use (with overlapped encoding): ~{peak:.1f} GB.")

                # Source frames and this chunk's inputs are no longer needed.
                shutil.rmtree(cin, ignore_errors=True)
                if not cfg["keep"]:
                    for n in names:
                        (frames_in / n).unlink(missing_ok=True)

                # Only one encode may be in flight, which also bounds disk use.
                raise_if_failed(wait=True)
                pending["fut"] = executor.submit(encode_job, ci, n_enc, seq, oext_seq, cleanup,
                                                 chunk_vid, chunk_flag, f"chunk {ci + 1}")
                pending["ci"] = ci
                done_frames += len(names)
                ci += 1
                self._progress(done_frames, total_est(done_frames), vidx, vn,
                               ex_state() + " | encoding in background")

            raise_if_failed(wait=True)
        finally:
            ex.stop()
            self.extractor = None
            if worker:
                worker.close()
            executor.shutdown(wait=True, cancel_futures=True)

        nchunks = ci
        if nchunks == 0:
            raise RuntimeError("No frames were extracted.")
        total = st["total"] if st["total"] is not None else done_frames
        (work / "total.txt").write_text(str(total))
        self.log(f"Processed {total:,} frames in {nchunks} chunk(s).")
        if self.avg_fps:
            self.emit("bench", (bench_key(cfg), self.avg_fps))

        self.check_cancel()
        self.log("\nConcatenating chunks and muxing audio, subtitles, fonts & chapters...")
        self._emit_progress(None, f"Video {vidx}/{vn}  ·  writing final file…")
        listfile = work / "concat.txt"
        lines = []
        for i in range(nchunks):
            p = (work / f"chunk_{i:04d}.mkv").resolve().as_posix().replace("'", "'\\''")
            lines.append(f"file '{p}'")
        listfile.write_text("\n".join(lines) + "\n")
        self.run(mux_cmd(cfg, listfile, video, final, info, test, self.log))

        if not cfg["keep"]:
            shutil.rmtree(work, ignore_errors=True)
        size_mb = final.stat().st_size / 1e6
        self.log(f"\nDone: {final}  ({size_mb:.0f} MB)")
        self.emit("output", str(final))
        self._emit_progress(100.0, f"Video {vidx}/{vn}  ·  finished")


# --------------------------------------------------------------------------
# Test frame (mirrors the real pipeline, including filters)
# --------------------------------------------------------------------------
def render_test_frame(cfg, video, log):
    """Extract one frame, upscale it, and apply the same output filters as a real run.
    Returns (source_png, result_png) or raises RuntimeError."""
    apisr = Path(cfg["apisr_dir"])
    work_base = Path(cfg["work_dir"] or cfg["out_dir"] or Path(video).parent)
    test_dir = work_base / f"apisr_test_sample_{Path(video).stem}"
    cin, cout = test_dir / "in", test_dir / "out"
    shutil.rmtree(test_dir, ignore_errors=True)
    cin.mkdir(parents=True, exist_ok=True)
    cout.mkdir(parents=True, exist_ok=True)

    info = probe_video(video)
    dur = info["duration"] or 0
    ss = "00:00:30" if dur > 60 else "00:00:05" if dur > 10 else "00:00:01"
    in_f = cin / "000001.png"
    png_fmt = {"ext": ".png", "vf": ["format=rgb24"], "args": ["-c:v", "png"]}
    vf = ",".join(extract_vf(cfg, info["sar"], 0, png_fmt))

    def grab(seek):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if seek:
            cmd += ["-ss", seek]
        cmd += ["-i", str(video), "-frames:v", "1", "-vf", vf, str(in_f)]
        return subprocess.run(cmd, capture_output=True, text=True)

    r = grab(ss)
    if not in_f.exists() or in_f.stat().st_size == 0:
        r = grab(None)
    if not in_f.exists() or in_f.stat().st_size == 0:
        raise RuntimeError("ffmpeg failed to extract a test frame:\n" + (r.stderr or ""))

    extra = safe_split(cfg["extra"])
    cmd = inference_cmd(cfg, cin, cout, extra, apisr)
    log("Running test frame upscale: " + " ".join(shlex.quote(str(c)) for c in cmd))
    res = subprocess.run(cmd, cwd=apisr, env=make_env(cfg), capture_output=True, text=True, timeout=900)
    if res.returncode != 0:
        raise RuntimeError(f"Test frame upscale failed (exit code {res.returncode}):\n"
                           f"{(res.stderr or res.stdout)[-1500:]}")
    outs = sorted(p for p in cout.iterdir() if p.suffix.lower() in IMG_EXTS)
    if not outs or outs[0].stat().st_size < 1024:
        raise RuntimeError(f"Inference completed but no output images were found in {cout}")

    result = test_dir / "preview.png"
    vfo = output_vf(cfg, cfg["height"], preview=True)
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(outs[0]),
                        "-vf", vfo, "-frames:v", "1", str(result)], capture_output=True, text=True)
    if not result.exists() or result.stat().st_size < 1024:
        log("NOTE: could not apply output filters to the preview; showing the raw upscale. "
            + (r.stderr or "")[-300:])
        result = outs[0]
    return str(in_f), str(result)


# --------------------------------------------------------------------------
# Pre-run summary and "copy command lines"
# --------------------------------------------------------------------------
def plan_summary(cfg):
    """Per-video and total estimates (frames, temp disk, time) without running anything."""
    scale = scale_for_weight(cfg["weight"])
    fmt = frame_format(cfg)
    size = max(int(cfg["chunk"]), 10)
    test = int(cfg["test_seconds"])
    rows, tot_frames, peak = [], 0, 0
    for v in cfg["videos"]:
        try:
            info = probe_video(v)
        except Exception as e:
            rows.append({"name": Path(v).name, "error": str(e)})
            continue
        n = info["nb_frames"] or (int(info["duration"] * float(info["fps"])) if info["duration"] else 0)
        if test and n:
            n = min(n, int(test * float(info["fps"])))
        tot_frames += n
        peak = max(peak, estimate_peak_temp_bytes(info["width"], info["height"], scale, size, fmt["ext"]))
        rows.append({"name": Path(v).name, "frames": n, "src": f"{info['width']}x{info['height']}",
                     "fps": float(info["fps"])})
    work = Path(cfg["work_dir"] or cfg["out_dir"] or (Path(cfg["videos"][0]).parent if cfg["videos"] else "."))
    bench = cfg.get("bench", {}).get(bench_key(cfg))
    eta = tot_frames / bench if bench and tot_frames else None
    return {"rows": rows, "frames": tot_frames, "peak_bytes": peak,
            "free_bytes": free_bytes(work), "work": str(work), "bench_fps": bench,
            "eta_seconds": eta}


def describe_commands(cfg, video):
    """Shell-ready equivalent of what a run does for one video (chunk 1)."""
    info = probe_video(video)
    fmt = frame_format(cfg)
    fext, size = fmt["ext"], max(int(cfg["chunk"]), 10)
    test = int(cfg["test_seconds"])
    plan = motion_plan(cfg, info["fps"])
    extra = safe_split(cfg["extra"])
    apisr = cfg["apisr_dir"]
    q = lambda c: " ".join(shlex.quote(str(x)) for x in c)
    W = "@@WORK@@"
    out = [f"# APISR run for: {video}", f'WORK="${{WORK:-/tmp/apisr_work}}"; mkdir -p "$WORK"/{{frames_in,chunk_in,chunk_out}}', ""]
    out.append("# 1) extract frames (the GUI throttles this to stay ~2 chunks ahead)")
    out.append(q(extract_cmd(cfg, video, f"{W}/frames_in/%06d{fext}", fmt, info["sar"], 0, test)))
    out.append("")
    out.append(f"# 2) upscale one chunk ({size} frames): link them into chunk_in, then")
    out.append(f"cd {shlex.quote(str(apisr))} && PYTHONPATH={shlex.quote(str(apisr))} " +
               q(inference_cmd(cfg, f"{W}/chunk_in", f"{W}/chunk_out", extra, apisr)))
    if plan and plan["method"] == "rife":
        exe, model = find_rife()
        out.append("")
        out.append("# 2b) interpolate")
        out.append(q([exe, "-i", f"{W}/chunk_out", "-o", f"{W}/chunk_rife", "-n", int(round(size * plan["factor"]))]
                     + (["-m", model] if model else [])))
    out.append("")
    out.append("# 3) filter + encode the chunk (symlink upscaled frames into chunk_seq as %06d first)")
    enc_fps = plan["target"] if plan and plan["method"] == "rife" else info["fps"]
    out.append(q(["ffmpeg", "-y", "-framerate", str(enc_fps), "-i", f"{W}/chunk_seq/%06d.png",
                  "-vf", output_vf(cfg, cfg["height"], plan=plan)] + encoder_args(cfg) + [f"{W}/chunk_0000.mkv"]))
    out.append("")
    out.append("# 4) after all chunks: concat + mux audio/subtitles/chapters")
    out.append(q(mux_cmd(cfg, f"{W}/concat.txt", video, "OUTPUT.mkv", info, test)))
    return "\n".join(out).replace("@@WORK@@", "$WORK") + "\n"
