# -*- coding: utf-8 -*-
"""
companion_ui.py — BestCam Virtual Webcam Bridge with tray UI
============================================================

Same streaming core as companion.py (phone MJPEG -> NV12 -> shared memory),
wrapped in a small tray UI: start/stop, ADB restart, and on-the-fly output
resolution switching (the client must re-open the camera after switching).

Requires: pystray, pillow, pywin32 (all present in the packaged release).
"""
import ctypes
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

from decoder import (
    FFmpegHwDecoder,
    PyAVH264Decoder,
    SoftDecoder,
    create_h264_decoder,
    list_hw_devices,
)

try:
    from pymft_h264.decoder import MFTDecoder
    from pymft_h264 import list_adapters as _list_mft_adapters
except Exception:
    MFTDecoder = None
    _list_mft_adapters = None

SHARED_MEM_NAME = "Global\\BestCam_SharedMem"
MUTEX_NAME = "Global\\BestCam_Mutex"
HEADER_SIZE = 24      # metadata portion of the header (width..frameIndex)
DESIRED_OFFSET = 24   # desiredWidth/desiredHeight (UINT32 x2), driver -> companion
DATA_OFFSET = 32      # NV12 frame data starts after the full 32-byte header
MAX_W, MAX_H = 1920, 1080
TOTAL_SIZE = 32 + MAX_W * MAX_H * 3 // 2
PORT = 8080
CONTROL_PORT = 8081   # phone control channel: capabilities handshake + set_resolution
RESOLUTIONS = [(640, 480), (800, 450), (800, 600), (1280, 720), (1920, 1080)]
MAX_CAP_AGE = 30.0    # seconds after which the capability table is re-fetched


class _SECURITY_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Revision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte),
        ("Control", ctypes.c_ushort),
        ("Owner", ctypes.c_void_p), ("Group", ctypes.c_void_p),
        ("Sacl", ctypes.c_void_p), ("Dacl", ctypes.c_void_p),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int32),
    ]


def _host_path():
    """Locate BestCamHost.exe: next to the packaged exe, or via BESTCAM_HOST."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        for cand in (os.path.join(base, "_internal", "BestCamHost.exe"),
                     os.path.join(base, "BestCamHost.exe")):
            if os.path.exists(cand):
                return cand
    return os.environ.get("BESTCAM_HOST", "BestCamHost.exe")


def _make_icon():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 16, 56, 52], radius=6, fill=(30, 120, 220))
    d.rounded_rectangle([20, 24, 44, 48], radius=4, outline=(255, 255, 255), width=3)
    d.ellipse([28, 30, 36, 38], fill=(255, 255, 255))
    return img


class MjpegFrameReader:
    """MJPEG multipart 帧读取器。

    以 --boundary 帧边界定位 + Content-Length 精确取帧，免疫：
      - JPEG 体内假 SOI (0xFFD8) 导致的解析错位
      - 头部残缺 / 缺 Content-Length 时旧实现的缓冲无界增长（SOI 永远停
        在偏移 0，死循环直至内存耗尽）
    缓冲超过 MAX_BUFFER 时从下一个 boundary 重同步，保证内存有界。

    read_frame() 返回完整 JPEG；数据不足返回 None（调用方继续 recv 后重试）；
    流不可恢复时抛 ConnectionError。
    """

    BOUNDARY = b"--boundary"
    MAX_BUFFER = 16 * 1024 * 1024
    MAX_JPEG = 8 * 1024 * 1024

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes):
        self._buf += data

    def read_frame(self):
        """Return (frame_bytes, content_type) or None."""
        while True:
            # 1) 上限保护：超限从下一个 boundary 重新同步（丢弃垃圾）
            if len(self._buf) > self.MAX_BUFFER:
                i = self._buf.find(self.BOUNDARY)
                if i < 0:
                    self._buf.clear()
                    return None
                del self._buf[:i]
                continue

            # 2) 定位帧头 boundary；之前的垃圾（含 HTTP 握手头）一并丢弃
            b = self._buf.find(self.BOUNDARY)
            if b < 0:
                return None  # 数据不足，等调用方 recv
            if b > 0:
                del self._buf[:b]
                b = 0

            # 3) 找头部结束 \r\n\r\n
            h_end = self._buf.find(b"\r\n\r\n", 0)
            if h_end < 0:
                return None
            if h_end > 4096:
                # 假 boundary（头异常大）：丢掉一个字节继续找真边界
                del self._buf[:1]
                continue

            # 4) 解析 Content-Length 与 Content-Type；无法确定帧长则跳过该头
            cl = None
            mime = "image/jpeg"
            for line in self._buf[0:h_end].split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    try:
                        cl = int(line.split(b":", 1)[1].strip())
                    except ValueError:
                        cl = None
                elif line.lower().startswith(b"content-type:"):
                    mime = line.split(b":", 1)[1].strip().decode("ascii", "ignore")
            if cl is None or cl <= 0 or cl > self.MAX_JPEG:
                del self._buf[:h_end + 4]
                continue

            # 5) 等待完整帧体
            jpeg_start = h_end + 4
            if len(self._buf) < jpeg_start + cl:
                return None
            jpeg = bytes(self._buf[jpeg_start:jpeg_start + cl])
            del self._buf[:jpeg_start + cl]

            # 6) 格式校验：JPEG 必须以 0xFFD9 结束；H.264 直接放行
            if mime == "image/jpeg" and (len(jpeg) < 4 or jpeg[-2:] != b"\xff\xd9"):
                continue
            return jpeg, mime


class Bridge:
    """Streaming core: ADB forward + MJPEG receive + NV12 -> shared memory.

    Async by design: start()/stop()/restart_adb() never block the caller;
    status transitions are pushed to the UI via the on_status callback
    (mirroring the official release: Connecting to ADB -> Starting ADB
    server -> Waiting for USB debug authorisation -> Device authorised ->
    Connecting to Android stream -> Streaming).
    """

    def __init__(self, width=1920, height=1080, on_status=None):
        self._width = width
        self._height = height
        self._fps = 30
        self._codec = os.environ.get("BESTCAM_CODEC", "h264").lower()
        self._hwaccel = os.environ.get("BESTCAM_HWACCEL", "d3d11va").lower()
        self._use_hw = os.environ.get("BESTCAM_USE_HW", "").lower() in ("1", "true", "yes")
        self._mft_adapter_luid = None
        self._frame_size = width * height * 3 // 2
        self._on_status = on_status
        # Phone capability table: list of (w, h, max_fps, encode_ms, codec).
        self._caps = []
        self._caps_t0 = 0.0
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Explicit signatures: without them ctypes truncates 64-bit handles
        # and pointers to 32-bit ints on x64.
        self._k32.CreateFileMappingW.restype = ctypes.c_void_p
        self._k32.CreateFileMappingW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_wchar_p,
        ]
        self._k32.CreateMutexW.restype = ctypes.c_void_p
        self._k32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.c_wchar_p,
        ]
        self._k32.MapViewOfFile.restype = ctypes.c_void_p
        self._k32.MapViewOfFile.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t,
        ]
        self._k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._k32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        self._k32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        self._writer = None
        self._thread = None
        self._stop = threading.Event()
        self._sock = None
        self._mutex = None
        self._decoder = None
        self._decoder_lock = threading.Lock()
        self.status_text = "Idle"
        self.decoder_text = "CPU"
        self.fps = 0.0
        self._fps_frames = 0
        self._fps_t0 = time.time()

    # -- status helper -----------------------------------------------------
    def _status(self, text):
        self.status_text = text
        if self._on_status:
            self._on_status(text)

    # -- shared memory -----------------------------------------------------
    def _open_shared_mem(self):
        h_map = None
        sa = sd = None
        for _ in range(60):  # wait up to 30 s for BestCamHost / driver
            if self._stop.is_set():
                raise RuntimeError("stopped while waiting for shared memory")
            # Create (or reuse) the mapping from the user session: a NULL DACL
            # lets the Session 0 Frame Server open it too. Creating here keeps
            # a single canonical object alive as long as the companion runs;
            # the driver only ever reuses it, so the two sides can never end
            # up on different mapping objects (which would silently split the
            # frame stream).
            if sa is None:
                sa, sd = self._null_dacl_sa()
            h_map = self._k32.CreateFileMappingW(
                ctypes.c_void_p(0xFFFFFFFFFFFFFFFF),  # INVALID_HANDLE_VALUE
                ctypes.byref(sa), 0x04,               # PAGE_READWRITE
                0, TOTAL_SIZE, SHARED_MEM_NAME)
            if h_map:
                break
            time.sleep(0.5)
        if not h_map:
            raise RuntimeError("Shared memory not found. Is BestCamHost.exe running?")
        ptr = self._k32.MapViewOfFile(h_map, 0x0002 | 0x0004, 0, 0, TOTAL_SIZE)
        if not ptr:
            raise RuntimeError("MapViewOfFile failed")
        self._sd = sd  # keep the security descriptor alive for the mutex below
        return h_map, ptr

    def _null_dacl_sa(self):
        """SECURITY_ATTRIBUTES whose DACL allows everyone (incl. the Session 0
        Frame Server) to open the mapping. Returns (sa, sd): the caller must
        keep `sd` alive (holding the reference) for as long as `sa` is used,
        otherwise the DACL pointer dangles and CreateFileMappingW fails."""
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        sd = _SECURITY_DESCRIPTOR()
        if not advapi.InitializeSecurityDescriptor(ctypes.byref(sd), 1):
            return None, None
        if not advapi.SetSecurityDescriptorDacl(ctypes.byref(sd), True, None, False):
            return None, None
        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = ctypes.cast(ctypes.byref(sd), ctypes.c_void_p).value
        return sa, sd

    def set_resolution(self, width, height, fps=None, codec=None):
        """Switch output resolution and codec; the client must request the
        same size (ExVR/OBS setting or re-open), otherwise frames stay black."""
        old_w, old_h, old_codec = self._width, self._height, self._codec
        if codec is None:
            cap = self._find_cap(width, height, prefer_codec="h264")
            codec = (cap[4] if cap else old_codec).lower()
        if fps is None:
            cap = self._find_cap(width, height, codec=codec)
            fps = cap[2] if cap else self._fps
        self._codec = codec
        self._send_control(f"set_resolution {width} {height} {fps} {codec}")
        changed = (width, height, codec) != (old_w, old_h, old_codec)
        self._width, self._height, self._fps = width, height, fps
        self._frame_size = width * height * 3 // 2
        self._release_decoder()
        if not changed:
            return
        running = self.running
        self.stop()
        if running:
            # The old thread may still be alive (recv shutdown takes a
            # moment); start() refuses to spawn a second stream, so wait
            # for it to exit first.
            deadline = time.time() + 15
            while self._thread is not None and self._thread.is_alive() and time.time() < deadline:
                time.sleep(0.05)
            self.start()

    # -- phone capability table -------------------------------------------
    @property
    def capabilities(self):
        """Cached capability table, re-fetched when stale or empty."""
        now = time.time()
        if (self._caps and now - self._caps_t0 < MAX_CAP_AGE) or self._stop.is_set():
            return self._caps
        try:
            self._caps = self._fetch_capabilities()
            self._caps_t0 = now
        except OSError:
            pass  # phone/ADB temporarily unreachable; keep the last table
        return self._caps

    def _fetch_capabilities(self):
        """GET /capabilities on the phone's control channel; parse JSON."""
        import json
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=5) as s:
            s.sendall(b"GET /capabilities HTTP/1.1\r\n\r\n")
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        body = data.split(b"\r\n\r\n", 1)[-1]
        caps = []
        try:
            for r in json.loads(body).get("resolutions", []):
                caps.append((int(r["w"]), int(r["h"]), int(r["max_fps"]),
                             int(r.get("encode_ms", 0)), str(r.get("codec", "mjpeg"))))
        except (ValueError, TypeError, KeyError):
            pass
        return caps

    def _send_control(self, cmd):
        """Fire a control command at the phone; best-effort (no raise)."""
        try:
            with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=5) as s:
                s.sendall(cmd.encode() + b"\r\n")
                s.settimeout(5)
                s.recv(64)
        except OSError:
            pass

    def _find_cap(self, width, height, codec=None, prefer_codec="h264"):
        """Best capability for (w,h). Prefer the requested codec, then the
        preferred codec, then any codec for this resolution, then closest."""
        caps = self.capabilities
        if not caps:
            return None
        candidates = [c for c in caps if c[0] == width and c[1] == height]
        if not candidates:
            candidates = [c for c in caps if c[0] * height == c[1] * width]
        if not candidates:
            candidates = caps
        if codec:
            for c in candidates:
                if c[4].lower() == codec.lower():
                    return c
        for c in candidates:
            if c[4].lower() == prefer_codec.lower():
                return c
        return candidates[0]

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def resolution(self):
        return self._width, self._height

    @property
    def codec(self):
        return self._codec

    def set_hwaccel(self, hwaccel: str):
        """Set the preferred decoder ('cpu', 'mft', or a FFmpeg hwaccel)."""
        self._hwaccel = hwaccel.lower()
        self._use_hw = self._hwaccel not in ("cpu", "mft")
        self._release_decoder()

    def set_mft_adapter(self, luid: int | None):
        """Select a specific GPU for Windows MFT decoding (None = auto)."""
        self._mft_adapter_luid = luid
        self._release_decoder()

    def _release_decoder(self):
        with self._decoder_lock:
            self._release_decoder_unsafe()

    def _release_decoder_unsafe(self):
        if self._decoder is not None:
            try:
                self._decoder.release()
            except Exception:
                pass
            self._decoder = None
        self.decoder_text = "CPU"

    def _decoder_for(self, mime: str):
        """Return a decoder suitable for the incoming MIME type."""
        h264_decoders = [PyAVH264Decoder, FFmpegHwDecoder]
        if MFTDecoder is not None:
            h264_decoders.append(MFTDecoder)
        h264_decoders = tuple(h264_decoders)

        with self._decoder_lock:
            if self._decoder is not None:
                # Reuse if MIME matches the decoder's supported input.
                if mime == "image/jpeg" and isinstance(self._decoder, SoftDecoder):
                    return self._decoder
                if mime == "video/h264" and isinstance(self._decoder, h264_decoders):
                    return self._decoder
                self._release_decoder_unsafe()
            if mime == "image/jpeg":
                self._decoder = SoftDecoder()
                self.decoder_text = "CPU"
            elif mime == "video/h264":
                if self._use_hw:
                    try:
                        self._decoder = FFmpegHwDecoder(self._hwaccel, self._width, self._height, self._fps)
                        self.decoder_text = f"HW {self._hwaccel.upper()}"
                    except Exception:
                        self._decoder = create_h264_decoder(width=self._width, height=self._height)
                        self.decoder_text = getattr(self._decoder, "name", lambda: "fallback")()
                elif self._hwaccel == "cpu":
                    self._decoder = create_h264_decoder(width=self._width, height=self._height, enable_hardware=False)
                    self.decoder_text = getattr(self._decoder, "name", lambda: "CPU")()
                else:
                    self._decoder = create_h264_decoder(width=self._width, height=self._height,
                                                          adapter_luid=self._mft_adapter_luid)
                    self.decoder_text = getattr(self._decoder, "name", lambda: "CPU")()
            else:
                return None
            return self._decoder

    # -- streaming ---------------------------------------------------------
    def start(self):
        if self.running:
            return
        # Fresh event per start: a stale thread blocked in recv() holds its
        # own event object and can never see this one get cleared, so it must
        # exit when it eventually wakes up (guards against duplicate streams).
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        # Unblock a thread stuck in recv(): shutdown makes the socket error
        # out immediately so join() completes instead of timing out and
        # leaving a half-dead thread behind.
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=3)
            if not self._thread.is_alive():
                self._thread = None
        self._status("Stopped")

    def restart_adb(self):
        """Async ADB restart: never blocks the UI."""
        threading.Thread(target=self._restart_adb_impl, daemon=True).start()

    def _restart_adb_impl(self):
        self._status("Restarting ADB…")
        was_running = self.running
        if was_running:
            self.stop()
        self._ensure_adb()
        if was_running:
            self.start()

    def _run(self):
        try:
            self._open_shared_mem_and_stream()
        except Exception as exc:
            if not self._stop.is_set():
                self._status(f"Error: {exc}")

    def _run_adb(self, *args):
        # CREATE_NO_WINDOW: the packaged exe is windowed; without this flag
        # every adb call spawns a flashing console window.
        subprocess.run(["adb", *args], check=False, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)

    def _host_running(self):
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq BestCamHost.exe"],
                             check=False, capture_output=True,
                             creationflags=subprocess.CREATE_NO_WINDOW
                             ).stdout.decode(errors="ignore")
        return "BestCamHost.exe" in out

    def _ensure_host(self):
        """Start BestCamHost if it isn't running (mirrors the official
        release where the driver follows the app's lifecycle).
        Note: the host blocks on getchar(), so it must get a console; we
        hide it via STARTUPINFO instead of CREATE_NO_WINDOW (which would
        make getchar() return immediately and the host exit)."""
        if self._host_running():
            return
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        subprocess.Popen([_host_path()], startupinfo=startupinfo)
        self._status("Starting camera driver…")

    def _ensure_adb(self):
        """kill/start adb server, wait for the phone, set up the forward.
        Every step honors the stop flag so Stop always wins, even mid-wait."""
        self._status("Killing ADB server…")
        self._run_adb("kill-server")
        if self._stop.is_set():
            return
        self._status("Starting ADB server…")
        self._run_adb("start-server")
        if self._stop.is_set():
            return
        for _ in range(30):  # wait up to 15 s for the device
            if self._stop.is_set():
                return
            out = subprocess.run(["adb", "devices"], check=False,
                                 capture_output=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW
                                 ).stdout.decode(errors="ignore")
            if any(line.strip().endswith("\tdevice") for line in out.splitlines()):
                break
            self._status("Waiting for USB debug authorisation on phone…")
            time.sleep(0.5)
        if self._stop.is_set():
            return
        self._status("Device authorised")
        self._run_adb("forward", f"tcp:{PORT}", f"tcp:{PORT}")
        self._run_adb("forward", f"tcp:{CONTROL_PORT}", f"tcp:{CONTROL_PORT}")

    def _open_shared_mem_and_stream(self):
        self._ensure_host()
        h_map, ptr = self._open_shared_mem()
        # The mutex is created by the driver together with the mapping; when
        # the companion created the mapping first (see _open_shared_mem) the
        # driver only reuses it and never creates the mutex, so create it here
        # instead of waiting for it. CreateMutexW reuses an existing one, so
        # both orders work.
        sa, sd = self._null_dacl_sa()
        self._sd = sd
        self._mutex = self._k32.CreateMutexW(ctypes.byref(sa), False, MUTEX_NAME)
        self._frame_index = 0
        self._status("Shared memory connected. Connecting to ADB…")
        self._ensure_adb()
        while not self._stop.is_set():
            try:
                self._stream_loop(h_map, ptr)
            except (ConnectionError, ConnectionRefusedError, socket.timeout, OSError) as exc:
                if self._stop.is_set():
                    break
                self._status(f"Stream error: {exc}. Reconnecting…")
                time.sleep(2)
        self._k32.UnmapViewOfFile(ctypes.c_void_p(ptr))
        self._k32.CloseHandle(h_map)

    def _stream_loop(self, h_map, ptr):
        sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        self._sock = sock
        try:
            sock.settimeout(10)
            reader = MjpegFrameReader()
            self._status(f"Streaming {self._width}x{self._height} [{self._codec}] -> BestCam driver")
            while not self._stop.is_set():
                frame = reader.read_frame()
                if frame is None:
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("connection closed")
                    reader.feed(chunk)
                    continue

                jpeg, mime = frame
                decoder = self._decoder_for(mime)
                if decoder is None:
                    continue
                bgr = decoder.decode(jpeg, mime)
                if bgr is None:
                    continue
                if bgr.shape[1] != self._width or bgr.shape[0] != self._height:
                    bgr = self._letterbox(bgr)
                self._write_frame(ptr, bgr)
        finally:
            sock.close()
            if self._sock is sock:
                self._sock = None
            self._release_decoder()

    def _letterbox(self, bgr):
        """Fit the source into the negotiated output preserving aspect ratio
        (black bars, never stretch). The source is the phone's native frame
        (e.g. 16:9 720p while the client asked for 4:3 640x480); the canvas is
        cached per target size."""
        sw, sh = bgr.shape[1], bgr.shape[0]
        tw, th = self._width, self._height
        scale = min(tw / sw, th / sh)
        nw, nh = max(1, int(round(sw * scale))), max(1, int(round(sh * scale)))
        resized = cv2.resize(
            bgr, (nw, nh),
            interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
        if not hasattr(self, "_canvas") or self._canvas.shape[:2] != (th, tw):
            self._canvas = np.zeros((th, tw, 3), dtype=np.uint8)
        canvas = self._canvas
        x0, y0 = (tw - nw) // 2, (th - nh) // 2
        canvas[:] = 0
        canvas[y0:y0 + nh, x0:x0 + nw] = resized
        return canvas

    def _write_frame(self, ptr, bgr):
        # Slices in flat byte offsets (w*h, w*h/4): the I420 U/V planes are
        # (w/2)*(h/2) bytes each; row slicing breaks for odd heights (800x450
        # -> h//4=112 rows = 89600 bytes vs 90000-byte U plane, numpy
        # broadcast error).
        yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)  # (H*3/2, W)
        flat = yuv.ravel()
        w, h = self._width, self._height
        nv12 = np.empty(self._frame_size, dtype=np.uint8)
        nv12[:w * h] = flat[:w * h]
        nv12[w * h::2] = flat[w * h: w * h + w * h // 4]
        nv12[w * h + 1::2] = flat[w * h + w * h // 4: w * h + w * h // 2]

        if self._mutex:
            self._k32.WaitForSingleObject(self._mutex, 5)
        # Data first, header last: the header (frameIndex) is the "data ready"
        # signal. Writing it first would let the driver read the new frameSize
        # against the previous frame's data -> one garbled frame per switch.
        ctypes.memmove(ptr + DATA_OFFSET, nv12.ctypes.data, self._frame_size)
        hdr = struct.pack("<4IQ", w, h, w, self._frame_size, self._frame_index)
        ctypes.memmove(ptr, hdr, HEADER_SIZE)
        if self._mutex:
            self._k32.ReleaseMutex(self._mutex)
        self._frame_index += 1

        self._fps_frames += 1
        now = time.time()
        if now - self._fps_t0 >= 1.0:
            self.fps = self._fps_frames / (now - self._fps_t0)
            self._fps_frames = 0
            self._fps_t0 = now


def main():
    import tkinter as tk
    import pystray
    from pystray import Menu, MenuItem

    root = tk.Tk()
    root.title("BestCam Bridge")
    root.geometry("320x240")
    root.resizable(False, False)

    status_var = tk.StringVar(value="Idle")
    res_var = tk.StringVar(value="1920x1080")
    fps_var = tk.StringVar(value="FPS: 0")
    dec_var = tk.StringVar(value="Decoder: CPU")

    bridge = Bridge()

    def update_ui():
        status_var.set(bridge.status_text)
        w, h = bridge.resolution
        res_var.set(f"{w}x{h} ({bridge.codec.upper()})")
        fps_var.set(f"FPS: {bridge.fps:.1f}")
        dec_var.set(f"Decoder: {bridge.decoder_text}")
        try:
            icon.title = f"BestCam Bridge — {bridge.status_text}"
        except Exception:
            pass
        root.after(500, update_ui)

    def start():
        bridge.start()

    def stop():
        bridge.stop()

    def restart_adb():
        bridge.restart_adb()  # async, never freezes the UI

    def set_res(w, h, fps=None, codec=None):
        bridge.set_resolution(w, h, fps, codec)
        root.after(0, update_ui)

    def res_action(w, h, fps, codec):
        return lambda icon, item: set_res(w, h, fps, codec)

    def res_checked(w, h):
        return lambda item: bridge.resolution == (w, h)

    def build_res_menu(item=None):
        # Conditional options straight from the phone's capability table.
        # Group by resolution and prefer H.264 if the phone offers it.
        caps = bridge.capabilities
        grouped = {}
        for c in caps:
            w, h, fps, enc, codec = c
            key = (w, h)
            if key not in grouped:
                grouped[key] = c
            else:
                # Prefer H.264 over MJPEG for the same resolution.
                if codec.lower() == "h264":
                    grouped[key] = c
        items = []
        for (w, h), c in sorted(grouped.items(), key=lambda x: x[0][0] * x[0][1]):
            label = f"{w}x{h} @ {c[2]} fps ({c[4].upper()})"
            items.append(MenuItem(label, res_action(w, h, c[2], c[4]),
                                  checked=res_checked(w, h)))
        if not items:
            items.append(MenuItem("(phone offline)", None, enabled=False))
        return Menu(*items)

    def set_hwaccel(hwaccel: str):
        bridge.set_hwaccel(hwaccel)
        root.after(0, update_ui)

    def hw_action(hwaccel: str):
        return lambda icon, item: set_hwaccel(hwaccel)

    def hw_checked(hwaccel: str):
        return lambda item: bridge._hwaccel == hwaccel

    def build_dec_menu(item=None):
        items = [MenuItem("CPU (software)", hw_action("cpu"), checked=hw_checked("cpu"))]
        if MFTDecoder is not None:
            items.append(MenuItem("Windows MFT", hw_action("mft"), checked=hw_checked("mft")))
        for dev in list_hw_devices():
            name = dev["name"]
            label = dev["label"]
            items.append(MenuItem(label, hw_action(name), checked=hw_checked(name)))
        return Menu(*items)

    def set_adapter(luid: int | None):
        bridge.set_mft_adapter(luid)
        root.after(0, update_ui)

    def adapter_action(luid: int | None):
        return lambda icon, item: set_adapter(luid)

    def adapter_checked(luid: int | None):
        return lambda item: bridge._mft_adapter_luid == luid

    def build_adapter_menu(item=None):
        items = [MenuItem("Auto (prefer iGPU)", adapter_action(None), checked=adapter_checked(None))]
        if _list_mft_adapters is not None:
            for a in _list_mft_adapters():
                name = a.name.decode("utf-8", "ignore").rstrip("\x00")
                kind = "iGPU" if a.is_integrated else ("SW" if a.is_software else "dGPU")
                label = f"{name} ({kind})"
                items.append(MenuItem(label, adapter_action(a.luid), checked=adapter_checked(a.luid)))
        if len(items) == 1:
            items.append(MenuItem("(no adapters found)", None, enabled=False))
        return Menu(*items)

    def on_quit(icon=None, item=None):
        bridge.stop()
        subprocess.run(["taskkill", "/IM", "BestCamHost.exe", "/F"],
                       check=False, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        root.quit()
        root.destroy()

    tk.Label(root, textvariable=status_var, font=("Segoe UI", 11)).pack(pady=8)
    tk.Label(root, textvariable=res_var, font=("Segoe UI", 9)).pack()
    tk.Label(root, textvariable=fps_var, font=("Segoe UI", 9)).pack()
    tk.Label(root, textvariable=dec_var, font=("Segoe UI", 9)).pack()
    btn_row = tk.Frame(root)
    btn_row.pack(pady=10)
    tk.Button(btn_row, text="Start", width=8, command=start).pack(side="left", padx=4)
    tk.Button(btn_row, text="Stop", width=8, command=stop).pack(side="left", padx=4)
    tk.Button(btn_row, text="Restart ADB", width=12, command=restart_adb).pack(side="left", padx=4)

    # Dynamic submenus: rebuilt on open, so the phone's capability table is
    # always current.
    menu = pystray.Menu(
        MenuItem("Show Window", lambda icon, item: root.deiconify(), default=True),
        MenuItem("Start", lambda icon, item: start(), enabled=lambda item: not bridge.running),
        MenuItem("Stop", lambda icon, item: stop(), enabled=lambda item: bridge.running),
        MenuItem("Resolution", Menu(build_res_menu)),
        MenuItem("Decoder Device", Menu(build_dec_menu)),
        MenuItem("MFT Adapter", Menu(build_adapter_menu)),
        MenuItem("Restart ADB", lambda icon, item: restart_adb()),
        pystray.Menu.SEPARATOR,
        MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon("BestCam", _make_icon(), "BestCam Bridge", menu)

    root.protocol("WM_DELETE_WINDOW", lambda: root.withdraw())
    threading.Thread(target=icon.run, daemon=True).start()
    root.after(500, update_ui)
    root.mainloop()


if __name__ == "__main__":
    main()
