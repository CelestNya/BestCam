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

The negotiated resolution must match what the client (e.g. ExVR) requests:
the MF source advertises 1920x1080 / 1280x720 / 800x450 / 640x480 (NV12, 30fps).
Pick one value for BESTCAM_WIDTH/HEIGHT and request the same in the client.

Shared memory layout (24-byte header + NV12 frame):
  Offset  0 : UINT32 width
  Offset  4 : UINT32 height
  Offset  8 : UINT32 stride
  Offset 12 : UINT32 frameSize
  Offset 16 : UINT64 frameIndex (monotonic)
  Offset 24 : NV12 data (Y plane + interleaved UV)

Usage:
  python companion.py            # 1080p (default)
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

TARGET_W = int(os.environ.get("BESTCAM_WIDTH", "1920"))
TARGET_H = int(os.environ.get("BESTCAM_HEIGHT", "1080"))
ADB = os.environ.get("BESTCAM_ADB", "adb")

SHARED_MEM_NAME = "Global\\BestCam_SharedMem"
MUTEX_NAME = "Global\\BestCam_Mutex"
HEADER_SIZE = 24
# Map the full 1080p-size mapping created by the driver; small resolutions
# only touch the beginning of it.
TOTAL_SIZE = HEADER_SIZE + 1920 * 1080 * 3 // 2
FRAME_SIZE = TARGET_W * TARGET_H * 3 // 2
PORT = 8080

_header = struct.pack("<4IQ", TARGET_W, TARGET_H, TARGET_W, FRAME_SIZE, 0)


class SharedMemWriter:
    def __init__(self):
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h_map = None
        for attempt in range(60):  # wait up to 30 s for the host/driver
            h_map = self._k32.OpenFileMappingW(0x0002 | 0x0004, False, SHARED_MEM_NAME)
            if h_map:
                break
            time.sleep(0.5)
        if not h_map:
            raise RuntimeError(
                "Shared memory not found. Is BestCamHost.exe running and the camera active?"
            )
        ptr = self._k32.MapViewOfFile(h_map, 0x0002 | 0x0004, 0, 0, TOTAL_SIZE)
        if not ptr:
            raise RuntimeError("MapViewOfFile failed")
        self._h_map = h_map
        self._ptr = ptr
        self._mutex = self._k32.OpenMutexW(0x0001, False, MUTEX_NAME)
        self._frame_index = 0

    def write(self, nv12: np.ndarray):
        if self._mutex:
            self._k32.WaitForSingleObject(self._mutex, 5)
        hdr = struct.pack("<4IQ", TARGET_W, TARGET_H, TARGET_W, FRAME_SIZE, self._frame_index)
        ctypes.memmove(self._ptr, hdr, HEADER_SIZE)
        ctypes.memmove(self._ptr + HEADER_SIZE, nv12.ctypes.data, FRAME_SIZE)
        if self._mutex:
            self._k32.ReleaseMutex(self._mutex)
        self._frame_index += 1


def bgr_to_nv12(bgr: np.ndarray) -> np.ndarray:
    """BGR -> NV12 (Y plane + interleaved UV), matching the driver's expectation."""
    yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)  # shape (H*3/2, W)
    y = yuv[:TARGET_H].ravel()
    u = yuv[TARGET_H: TARGET_H + TARGET_H // 4].ravel()
    v = yuv[TARGET_H + TARGET_H // 4: TARGET_H + TARGET_H // 2].ravel()
    nv12 = np.empty(FRAME_SIZE, dtype=np.uint8)
    nv12[:TARGET_W * TARGET_H] = y
    nv12[TARGET_W * TARGET_H::2] = u
    nv12[TARGET_W * TARGET_H + 1::2] = v
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
    print(f"BestCam companion: target {TARGET_W}x{TARGET_H}, mapping {TOTAL_SIZE} bytes")

    # ADB forward
    subprocess.run([ADB, "forward", f"tcp:{PORT}", f"tcp:{PORT}"], check=False, capture_output=True)

    writer = SharedMemWriter()
    print("Shared memory connected. Waiting for phone stream...")

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
                if bgr.shape[1] != TARGET_W or bgr.shape[0] != TARGET_H:
                    bgr = cv2.resize(bgr, (TARGET_W, TARGET_H), interpolation=cv2.INTER_AREA)
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
