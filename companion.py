# -*- coding: utf-8 -*-
"""
companion.py — BestCam Virtual Webcam Bridge (user-session receiver)
====================================================================

Receives the MJPEG stream from the Android phone (ADB-forwarded TCP :8080),
decodes each JPEG, resizes it to the target resolution, converts to NV12 and
writes it into the shared memory segment "Global\\BestCam_SharedMem" that the
Media Foundation virtual camera source (BestCamSource.dll) delivers.

This completes the architecture described in the README ("Windows App —
receives the stream, decodes each frame ... and feeds it into a virtual
camera driver via Shared Memory").

The output resolution is configurable via environment variables:

    BESTCAM_WIDTH   (default 1920)
    BESTCAM_HEIGHT  (default 1080)
    BESTCAM_ADB     (optional, path to adb.exe; defaults to "adb" on PATH)

Switching resolution is MANUAL: pick BESTCAM_WIDTH/HEIGHT and make the client
(e.g. OBS) request the same size. If the client requests a different size,
the driver delivers black frames until the two sides match.

Shared memory layout (24-byte header + NV12 frame, matches FrameServer.h):
  Offset  0 : UINT32 width
  Offset  4 : UINT32 height
  Offset  8 : UINT32 stride
  Offset 12 : UINT32 frameSize
  Offset 16 : UINT64 frameIndex (monotonic)
  Offset 24 : NV12 data (Y plane + interleaved UV)

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
DATA_OFFSET = 24      # NV12 frame data starts right after the header
# Map the full 1080p-size mapping created by the driver; small resolutions
# only touch the beginning of it.
TOTAL_SIZE = 24 + 1920 * 1080 * 3 // 2
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
        if not self._mutex:
            return
        # Data first, header last: the header (frameIndex) is the "data ready"
        # signal. Writing it first would let the driver read the new frameSize
        # against the previous frame's data -> one garbled frame per switch.
        # try/finally guarantees ReleaseMutex on any exception (e.g. Ctrl+C),
        # otherwise the mutex is left abandoned and later writes run lockless.
        try:
            self._k32.WaitForSingleObject(self._mutex, 5)
            ctypes.memmove(self._ptr + DATA_OFFSET, nv12.ctypes.data, frame_size())
            hdr = struct.pack("<4IQ", target_w, target_h, target_w, frame_size(), self._frame_index)
            ctypes.memmove(self._ptr, hdr, HEADER_SIZE)
        finally:
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


def recv_http_headers(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("connection closed while reading headers")
        data += chunk
    return data


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def main():
    print(f"BestCam companion: target {target_w}x{target_h}, mapping {TOTAL_SIZE} bytes")

    # ADB forward
    subprocess.run([ADB, "forward", f"tcp:{PORT}", f"tcp:{PORT}"], check=False, capture_output=True)

    writer = SharedMemWriter()
    print("Shared memory connected. Waiting for phone stream...")

    frame_counter = 0
    while True:
        try:
            sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
            # consume the multipart head until the first image boundary
            first = sock.recv(4096)
            while b"\r\n\r\n" not in first:
                first += sock.recv(4096)
            # first frame data follows; loop below reads subsequent parts
            pending = first.split(b"\r\n\r\n", 1)[1]
            sock.settimeout(10)
            print("Stream connected. Pushing NV12 frames to shared memory.")
            while True:
                if len(pending) < 4:
                    pending += recv_exact(sock, 4 - len(pending))
                # find SOI marker (0xFFD8) and Content-Length from headers
                # MJPEG parts: headers then JPEG; we simply scan for SOI..EOI
                soi = pending.find(b"\xff\xd8")
                if soi < 0:
                    pending += recv_exact(sock, 4096)
                    continue
                # headers before SOI contain Content-Length of the JPEG body
                head = pending[:soi]
                cl = None
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        cl = int(line.split(b":")[1].strip())
                if cl is None:
                    pending = pending[soi:] + recv_exact(sock, 4096)
                    continue
                # wait for a full JPEG body
                while len(pending) - soi < cl:
                    pending += recv_exact(sock, 4096)
                jpeg = pending[soi: soi + cl]
                pending = pending[soi + cl:]

                bgr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if bgr is None:
                    continue
                if bgr.shape[1] != target_w or bgr.shape[0] != target_h:
                    bgr = cv2.resize(bgr, (target_w, target_h), interpolation=cv2.INTER_AREA)
                writer.write(bgr_to_nv12(bgr))
        except (ConnectionError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            print(f"Stream error: {exc}. Reconnecting in 2 s...")
            time.sleep(2)
        finally:
            try:
                sock.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
