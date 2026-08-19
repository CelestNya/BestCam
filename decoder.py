# -*- coding: utf-8 -*-
"""Companion-side frame decoders.

- SoftDecoder: OpenCV imdecode for MJPEG (CPU, always works).
- PyAVH264Decoder: in-process software H.264 decoder via PyAV/FFmpeg.
- FFmpegHwDecoder: FFmpeg subprocess with a chosen hwaccel for H.264.

Decoders are created per resolution so that the output size is known up
front; a resolution switch destroys the old decoder and creates a new one.
"""
import io
import os
import queue
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from typing import Optional

import cv2
import numpy as np


def find_ffmpeg() -> str:
    """Return the FFmpeg binary packaged with the app, or the system one."""
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except Exception:
        pass
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    # Packaged fallback inside _internal (PyInstaller)
    bundled = os.path.join(os.path.dirname(__file__), "_internal", "ffmpeg.exe")
    if os.path.exists(bundled):
        return bundled
    raise RuntimeError("ffmpeg not found")


def list_hw_devices() -> list:
    """Return hardware-acceleration backends compiled into FFmpeg.

    The returned items are dicts with keys: name (hwaccel string) and label
    (human-readable).
    """
    ffmpeg = find_ffmpeg()
    try:
        out = subprocess.check_output(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-hwaccels"],
            stderr=subprocess.STDOUT, text=True, timeout=10
        )
    except (subprocess.CalledProcessError, OSError):
        return []

    labels = {
        "dxva2": "DXVA2",
        "d3d11va": "D3D11VA",
        "d3d12va": "D3D12VA",
        "cuda": "NVIDIA CUVID",
        "cuvid": "NVIDIA CUVID",
        "qsv": "Intel QSV",
        "opencl": "OpenCL",
        "vulkan": "Vulkan",
    }

    accels = []
    for line in out.splitlines():
        tok = line.strip().lower()
        if not tok or tok in ("hardware", "acceleration", "methods:"):
            continue
        if tok in labels:
            accels.append({"name": tok, "label": labels[tok]})
    return accels


class FrameDecoder(ABC):
    """Abstract companion frame decoder."""

    @abstractmethod
    def decode(self, data: bytes, mime: str) -> Optional[np.ndarray]:
        """Return a BGR numpy array or None on failure."""
        ...

    @abstractmethod
    def name(self) -> str:
        ...

    def release(self):
        pass


class SoftDecoder(FrameDecoder):
    """CPU software decoder; only supports MJPEG."""

    def __init__(self):
        self._buf = np.empty(0, dtype=np.uint8)

    def decode(self, data: bytes, mime: str) -> Optional[np.ndarray]:
        if mime != "image/jpeg":
            return None
        if len(data) > len(self._buf):
            self._buf = np.empty(len(data), dtype=np.uint8)
        self._buf[:len(data)] = np.frombuffer(data, dtype=np.uint8)
        return cv2.imdecode(self._buf[:len(data)], cv2.IMREAD_COLOR)

    def name(self) -> str:
        return "CPU (software)"


class PyAVH264Decoder(FrameDecoder):
    """In-process software H.264 decoder via PyAV/FFmpeg.

    This avoids the rawvideo pipe overhead of the subprocess decoder and is
    usually fast enough for 720p60 on a modern desktop.
    """

    def __init__(self):
        import av
        self._av = av

    def decode(self, data: bytes, mime: str) -> Optional[np.ndarray]:
        if mime != "video/h264" or not data:
            return None
        try:
            # av.open() with a per-frame Annex-B access unit is more robust
            # than CodecContext.parse() for repeated SPS/PPS prefixes.
            bio = io.BytesIO(data)
            with self._av.open(bio, mode="r", format="h264") as container:
                for packet in container.demux():
                    for frame in packet.decode():
                        return frame.to_ndarray(format="bgr24")
        except Exception:
            pass
        return None

    def name(self) -> str:
        return "CPU (PyAV software)"

    def release(self):
        pass


class FFmpegHwDecoder(FrameDecoder):
    """FFmpeg-based H.264 decoder with optional hardware acceleration.

    The subprocess is kept alive and fed one Annex-B access unit at a time.
    Output is raw BGR24 at the configured width/height. If FFmpeg exits, the
    next decode() call respawns it automatically.
    """

    def __init__(self, hwaccel: str, width: int, height: int, fps: int = 60):
        self.hwaccel = hwaccel
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_bytes = width * height * 3
        self.ffmpeg = find_ffmpeg()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._frame_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=4)

    def name(self) -> str:
        return f"FFmpeg {self.hwaccel.upper()}"

    def _spawn(self, mime: str):
        if mime != "video/h264":
            raise RuntimeError(f"FFmpegHwDecoder only supports H.264, got {mime}")
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-probesize", "32",
            "-analyzeduration", "0",
            "-threads", "1",
            "-hwaccel", self.hwaccel,
            "-f", "h264",
            "-r", str(self.fps),
            "-i", "-",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise RuntimeError(f"failed to start ffmpeg: {exc}")

        def drain():
            if self._proc and self._proc.stderr:
                try:
                    for _ in self._proc.stderr:
                        pass
                except Exception:
                    pass

        def reader():
            if self._proc is None or self._proc.stdout is None:
                return
            while True:
                raw = b""
                while len(raw) < self.frame_bytes:
                    if self._proc is None:
                        return
                    try:
                        chunk = self._proc.stdout.read(self.frame_bytes - len(raw))
                    except ValueError:
                        return
                    if not chunk:
                        return
                    raw += chunk
                try:
                    self._frame_queue.put(raw, timeout=0.5)
                except queue.Full:
                    # Drop oldest frame to keep latency low.
                    try:
                        self._frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                    self._frame_queue.put(raw, timeout=0.5)

        self._stderr_thread = threading.Thread(target=drain, daemon=True)
        self._stderr_thread.start()
        self._stdout_thread = threading.Thread(target=reader, daemon=True)
        self._stdout_thread.start()

    def decode(self, data: bytes, mime: str) -> Optional[np.ndarray]:
        if mime != "video/h264":
            return None
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn(mime)
            proc = self._proc
            assert proc is not None
            try:
                proc.stdin.write(data)
            except (BrokenPipeError, OSError):
                self.release()
                return None

        try:
            raw = self._frame_queue.get(timeout=1.0)
        except queue.Empty:
            return None
        if len(raw) != self.frame_bytes:
            return None
        return np.frombuffer(raw, dtype=np.uint8).reshape((self.height, self.width, 3))

    def release(self):
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=1)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None
            # Drop queued frames so a new decoder starts clean.
            while not self._frame_queue.empty():
                try:
                    self._frame_queue.get_nowait()
                except queue.Empty:
                    break
