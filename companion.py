# -*- coding: utf-8 -*-
"""
companion.py — BestCam Virtual Webcam Bridge (configurable resolution)
======================================================================

Receives the MJPEG stream from the Android phone (ADB-forwarded TCP :8080),
decodes each JPEG, resizes it to the target resolution, converts to NV12 and
writes it into the shared memory segment "Global\\BestCam_SharedMem" that the
Media Foundation virtual camera source (BestCamSource.dll) delivers.

The target resolution is configurable via environment variables so the whole
chain can run below 1080p:

    BESTCAM_WIDTH   (default 1920)
    BESTCAM_HEIGHT  (default 1080)
    BESTCAM_ADB     (optional, path to adb.exe; defaults to "adb" on PATH)

Switching resolution is MANUAL: pick BESTCAM_WIDTH/HEIGHT (or the tray menu
in the UI build) and make the client (e.g. ExVR) request the same size. The
MF source advertises 1920x1080 / 1280x720 / 800x600 / 800x450 / 640x480
(NV12, 30fps). If the client requests a different size, the driver delivers
black frames until the two sides match (the driver still publishes the
client's negotiated resolution in desiredWidth/desiredHeight, but it is only
informational now).

Shared memory layout (32-byte header + NV12 frame):
  Offset  0 : UINT32 width
  Offset  4 : UINT32 height
  Offset  8 : UINT32 stride
  Offset 12 : UINT32 frameSize
  Offset 16 : UINT64 frameIndex (monotonic)
  Offset 24 : UINT32 desiredWidth  (written by the driver)
  Offset 28 : UINT32 desiredHeight (written by the driver; 0 = none)
  Offset 32 : NV12 data (Y plane + interleaved UV)

Usage:
  python companion.py            # 1080p output (manual)
  BESTCAM_WIDTH=1280 BESTCAM_HEIGHT=720 python companion.py
"""
import ctypes
import os
import socket
import struct
import subprocess
import time

import cv2
import numpy as np

target_w = int(os.environ.get("BESTCAM_WIDTH", "1920"))
target_h = int(os.environ.get("BESTCAM_HEIGHT", "1080"))
ADB = os.environ.get("BESTCAM_ADB", "adb")

SHARED_MEM_NAME = "Global\\BestCam_SharedMem"
MUTEX_NAME = "Global\\BestCam_Mutex"
HEADER_SIZE = 24      # metadata portion of the header (width..frameIndex)
DESIRED_OFFSET = 24   # desiredWidth/desiredHeight (UINT32 x2), driver -> companion
DATA_OFFSET = 32      # NV12 frame data starts after the full 32-byte header
# Map the full 1080p-size mapping created by the driver; small resolutions
# only touch the beginning of it.
TOTAL_SIZE = 32 + 1920 * 1080 * 3 // 2
PORT = 8080


def frame_size():
    return target_w * target_h * 3 // 2


_header = struct.pack("<4IQ", target_w, target_h, target_w, frame_size(), 0)


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


def _null_dacl_sa(k32):
    """SECURITY_ATTRIBUTES whose DACL allows everyone (incl. the Session 0
    Frame Server) to open the mapping. Returns (sa, sd): the caller must keep
    `sd` alive (holding the reference) for as long as `sa` is used, otherwise
    the DACL pointer dangles and CreateFileMappingW fails."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    sd = _SECURITY_DESCRIPTOR()
    ok = advapi.InitializeSecurityDescriptor(ctypes.byref(sd), 1)  # SECURITY_DESCRIPTOR_REVISION
    if not ok:
        return None, None
    ok = advapi.SetSecurityDescriptorDacl(ctypes.byref(sd), True, None, False)
    if not ok:
        return None, None
    sa = _SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = ctypes.cast(ctypes.byref(sd), ctypes.c_void_p).value
    return sa, sd


class SharedMemWriter:
    def __init__(self):
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
        h_map = None
        sa = sd = None
        for attempt in range(60):  # wait up to 30 s for the host/driver
            # Create (or reuse) the mapping from the user session: a NULL DACL
            # lets the Session 0 Frame Server open it too. Creating here keeps
            # a single canonical object alive as long as the companion runs;
            # the driver only ever reuses it, so the two sides can never end
            # up on different mapping objects (which would silently split the
            # frame stream).
            if sa is None:
                sa, sd = _null_dacl_sa(self._k32)
            h_map = self._k32.CreateFileMappingW(
                ctypes.c_void_p(0xFFFFFFFFFFFFFFFF),  # INVALID_HANDLE_VALUE
                ctypes.byref(sa), 0x04,               # PAGE_READWRITE
                0, TOTAL_SIZE, SHARED_MEM_NAME)
            if h_map:
                break
            time.sleep(0.5)
        if not h_map:
            raise RuntimeError(
                "Shared memory not found. Is BestCamHost.exe running and the camera active?"
            )
        self._sd = sd  # keep the security descriptor alive for the mutex below
        ptr = self._k32.MapViewOfFile(h_map, 0x0002 | 0x0004, 0, 0, TOTAL_SIZE)
        if not ptr:
            raise RuntimeError("MapViewOfFile failed")
        self._h_map = h_map
        self._ptr = ptr
        # The mutex is created by the driver together with the mapping; when
        # the companion created the mapping first (see above) the driver only
        # reuses it and never creates the mutex, so the companion must create
        # it here instead of waiting for it. CreateMutexW reuses an existing
        # one, so both orders work.
        self._mutex = None
        if sa is None:
            sa, sd = _null_dacl_sa(self._k32)
        self._mutex = self._k32.CreateMutexW(ctypes.byref(sa), False, MUTEX_NAME)
        if not self._mutex:
            raise RuntimeError("CreateMutexW failed")
        self._frame_index = 0

    def write(self, nv12: np.ndarray):
        if self._mutex:
            self._k32.WaitForSingleObject(self._mutex, 5)
        # Data first, header last: the header (frameIndex) is the "data ready"
        # signal. Writing it first would let the driver read the new frameSize
        # against the previous frame's data -> one garbled frame per switch.
        ctypes.memmove(self._ptr + DATA_OFFSET, nv12.ctypes.data, frame_size())
        hdr = struct.pack("<4IQ", target_w, target_h, target_w, frame_size(), self._frame_index)
        ctypes.memmove(self._ptr, hdr, HEADER_SIZE)
        if self._mutex:
            self._k32.ReleaseMutex(self._mutex)
        self._frame_index += 1


def bgr_to_nv12(bgr: np.ndarray) -> np.ndarray:
    """BGR -> NV12 (Y plane + interleaved UV), matching the driver's expectation.

    Slices are computed in flat byte offsets (w*h, w*h/4) instead of rows:
    the I420 U/V planes are (w/2)*(h/2) bytes each, and row slicing breaks
    for odd heights (800x450 has h//4=112 rows = 89600 bytes but the U plane
    is 90000 bytes -> numpy broadcast error).
    """
    yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)  # shape (H*3/2, W)
    flat = yuv.ravel()
    y = flat[:target_w * target_h]
    u = flat[target_w * target_h: target_w * target_h + target_w * target_h // 4]
    v = flat[target_w * target_h + target_w * target_h // 4: target_w * target_h + target_w * target_h // 2]
    nv12 = np.empty(frame_size(), dtype=np.uint8)
    nv12[:target_w * target_h] = y
    nv12[target_w * target_h::2] = u
    nv12[target_w * target_h + 1::2] = v
    return nv12


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

            # 4) 解析 Content-Length；无法确定帧长则跳过该头继续找
            cl = None
            for line in self._buf[0:h_end].split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    try:
                        cl = int(line.split(b":", 1)[1].strip())
                    except ValueError:
                        cl = None
                    break
            if cl is None or cl <= 0 or cl > self.MAX_JPEG:
                del self._buf[:h_end + 4]
                continue

            # 5) 等待完整帧体
            jpeg_start = h_end + 4
            if len(self._buf) < jpeg_start + cl:
                return None
            jpeg = bytes(self._buf[jpeg_start:jpeg_start + cl])
            del self._buf[:jpeg_start + cl]

            # 6) EOI 校验：JPEG 必须以 0xFFD9 结束，坏帧丢弃继续同步
            if len(jpeg) < 4 or jpeg[-2:] != b"\xff\xd9":
                continue
            return jpeg


def main():
    print(f"BestCam companion: target {target_w}x{target_h}, mapping {TOTAL_SIZE} bytes")

    # ADB forward
    subprocess.run([ADB, "forward", f"tcp:{PORT}", f"tcp:{PORT}"], check=False, capture_output=True)

    writer = SharedMemWriter()
    print("Shared memory connected. Waiting for phone stream...")

    while True:
        sock = None
        try:
            # Sync the phone to our target resolution over the control channel
            # (best-effort; the phone picks its closest supported option).
            try:
                with socket.create_connection(("127.0.0.1", 8081), timeout=5) as ctl:
                    ctl.sendall(f"set_resolution {target_w} {target_h} 0\r\n".encode())
                    ctl.settimeout(5)
                    ctl.recv(64)
            except OSError:
                pass
            sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
            sock.settimeout(10)
            reader = MjpegFrameReader()
            print("Stream connected. Pushing NV12 frames to shared memory.")
            canvas = None
            while True:
                jpeg = reader.read_frame()
                if jpeg is None:
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("connection closed")
                    reader.feed(chunk)
                    continue

                bgr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if bgr is None:
                    continue
                if bgr.shape[1] != target_w or bgr.shape[0] != target_h:
                    # letterbox: preserve aspect ratio, never stretch
                    sw, sh = bgr.shape[1], bgr.shape[0]
                    scale = min(target_w / sw, target_h / sh)
                    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
                    resized = cv2.resize(
                        bgr, (nw, nh),
                        interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
                    if canvas is None or canvas.shape[:2] != (target_h, target_w):
                        canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
                    canvas[:] = 0
                    x0, y0 = (target_w - nw) // 2, (target_h - nh) // 2
                    canvas[y0:y0 + nh, x0:x0 + nw] = resized
                    bgr = canvas
                writer.write(bgr_to_nv12(bgr))
        except (ConnectionError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            print(f"Stream error: {exc}. Reconnecting in 2 s...")
            time.sleep(2)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
