# -*- coding: utf-8 -*-
"""Python wrapper that exposes pyMFT as a BestCam-compatible FrameDecoder."""

import ctypes
import sys
from typing import Optional

import cv2
import numpy as np

from . import _dll, init, shutdown, _err, _DecoderConfig


class MFTDecoder:
    """Windows Media Foundation H.264 -> BGR24 decoder.

    This class matches the interface expected by BestCam's decoder.py:
      - decode(data: bytes, mime: str) -> np.ndarray | None
      - name() -> str
      - release()
    """

    def __init__(self, width: int = 0, height: int = 0,
                 enable_hardware: bool = True, prefer_igpu: bool = True):
        self.width = width
        self.height = height
        self._handle = None
        self._closed = False
        self._buf: Optional[np.ndarray] = None

        init()

        config = _DecoderConfig(
            width=width,
            height=height,
            enable_hardware=1 if enable_hardware else 0,
            prefer_igpu=1 if prefer_igpu else 0,
        )
        self._handle = _dll.pyMFT_decoder_create(ctypes.byref(config))
        if not self._handle:
            shutdown()
            raise RuntimeError(f"pyMFT_decoder_create failed: {_err()}")

    def name(self) -> str:
        return "MFT (Windows native)"

    def decode(self, data: bytes, mime: str) -> Optional[np.ndarray]:
        if self._closed or not self._handle:
            return None
        if mime != "video/h264" or not data:
            return None

        rc = _dll.pyMFT_decoder_feed(
            self._handle,
            (ctypes.c_uint8 * len(data)).from_buffer_copy(data),
            len(data),
        )
        # rc == 0 (OK) 或 1 (NEED_MORE_INPUT) 都可以继续尝试 read
        if rc < 0:
            return None

        # 分配输出 buffer：NV12 大小 = width * height * 3 / 2
        # 如果构造时未指定宽高，先用一个足够大的 buffer，read 后再调整
        buf_size = (self.width * self.height * 3 // 2) if (self.width and self.height) else (1920 * 1080 * 3 // 2)
        if self._buf is None or self._buf.size < buf_size:
            self._buf = np.empty(buf_size, dtype=np.uint8)

        w = ctypes.c_int(0)
        h = ctypes.c_int(0)
        rc = _dll.pyMFT_decoder_read(
            self._handle,
            self._buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            buf_size,
            ctypes.byref(w),
            ctypes.byref(h),
            0,
        )
        if rc == 2:  # PYMFT_NO_OUTPUT
            return None
        if rc < 0:
            return None

        ow, oh = w.value, h.value
        if ow <= 0 or oh <= 0:
            return None
        self.width = ow
        self.height = oh
        expected = ow * oh * 3 // 2
        if expected > buf_size:
            # 实际分辨率比预分配大，重新分配并重试
            self._buf = np.empty(expected, dtype=np.uint8)
            rc = _dll.pyMFT_decoder_read(
                self._handle,
                self._buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
                expected,
                ctypes.byref(w),
                ctypes.byref(h),
                0,
            )
            if rc != 0:
                return None

        nv12 = self._buf[:expected].reshape((oh * 3 // 2, ow))
        return cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)

    def release(self) -> None:
        if self._handle and not self._closed:
            _dll.pyMFT_decoder_destroy(self._handle)
            self._handle = None
            self._closed = True
            shutdown()

    def __del__(self):
        self.release()


def is_available() -> bool:
    """Return True if pyMFT.dll can be loaded and initialized on this machine."""
    try:
        d = MFTDecoder()
        d.release()
        return True
    except Exception:
        return False
