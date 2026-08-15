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
import threading
import time

import cv2
import numpy as np

SHARED_MEM_NAME = "Global\\BestCam_SharedMem"
MUTEX_NAME = "Global\\BestCam_Mutex"
HEADER_SIZE = 24
MAX_W, MAX_H = 1920, 1080
TOTAL_SIZE = HEADER_SIZE + MAX_W * MAX_H * 3 // 2
PORT = 8080
RESOLUTIONS = [(640, 480), (800, 450), (1280, 720), (1920, 1080)]


def _make_icon():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 16, 56, 52], radius=6, fill=(30, 120, 220))
    d.rounded_rectangle([20, 24, 44, 48], radius=4, outline=(255, 255, 255), width=3)
    d.ellipse([28, 30, 36, 38], fill=(255, 255, 255))
    return img


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
        self._frame_size = width * height * 3 // 2
        self._on_status = on_status
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._writer = None
        self._thread = None
        self._stop = threading.Event()
        self._mutex = None
        self.status_text = "Idle"
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
        for _ in range(60):  # wait up to 30 s for BestCamHost / driver
            h_map = self._k32.OpenFileMappingW(0x0002 | 0x0004, False, SHARED_MEM_NAME)
            if h_map:
                break
            time.sleep(0.5)
        if not h_map:
            raise RuntimeError("Shared memory not found. Is BestCamHost.exe running?")
        ptr = self._k32.MapViewOfFile(h_map, 0x0002 | 0x0004, 0, 0, TOTAL_SIZE)
        if not ptr:
            raise RuntimeError("MapViewOfFile failed")
        return h_map, ptr

    def set_resolution(self, width, height):
        """Switch output resolution; client must re-open the camera."""
        if (width, height) == (self._width, self._height):
            return
        running = self.running
        self.stop()
        self._width, self._height = width, height
        self._frame_size = width * height * 3 // 2
        if running:
            self.start()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def resolution(self):
        return self._width, self._height

    # -- streaming ---------------------------------------------------------
    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
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
            self._status(f"Error: {exc}")

    def _run_adb(self, *args):
        subprocess.run(["adb", *args], check=False, capture_output=True)

    def _ensure_adb(self):
        """kill/start adb server, wait for the phone, set up the forward."""
        self._status("Killing ADB server…")
        self._run_adb("kill-server")
        self._status("Starting ADB server…")
        self._run_adb("start-server")
        for _ in range(30):  # wait up to 15 s for the device
            out = subprocess.run(["adb", "devices"], check=False,
                                 capture_output=True).stdout.decode(errors="ignore")
            if any(line.strip().endswith("\tdevice") for line in out.splitlines()):
                break
            self._status("Waiting for USB debug authorisation on phone…")
            time.sleep(0.5)
        self._status("Device authorised")
        self._run_adb("forward", f"tcp:{PORT}", f"tcp:{PORT}")

    def _open_shared_mem_and_stream(self):
        h_map, ptr = self._open_shared_mem()
        self._mutex = self._k32.OpenMutexW(0x0001, False, MUTEX_NAME)
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
        first = sock.recv(4096)
        while b"\r\n\r\n" not in first:
            first += sock.recv(4096)
        pending = first.split(b"\r\n\r\n", 1)[1]
        sock.settimeout(10)
        self._status(f"Streaming {self._width}x{self._height} -> BestCam driver")
        try:
            while not self._stop.is_set():
                if len(pending) < 4:
                    pending += self._recv_exact(sock, 4 - len(pending))
                soi = pending.find(b"\xff\xd8")
                if soi < 0:
                    pending += self._recv_exact(sock, 4096)
                    continue
                head = pending[:soi]
                cl = None
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        cl = int(line.split(b":")[1].strip())
                if cl is None:
                    pending = pending[soi:] + self._recv_exact(sock, 4096)
                    continue
                while len(pending) - soi < cl:
                    pending += self._recv_exact(sock, 4096)
                jpeg = pending[soi: soi + cl]
                pending = pending[soi + cl:]

                bgr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if bgr is None:
                    continue
                if bgr.shape[1] != self._width or bgr.shape[0] != self._height:
                    bgr = cv2.resize(bgr, (self._width, self._height),
                                     interpolation=cv2.INTER_AREA)
                self._write_frame(ptr, bgr)
        finally:
            sock.close()

    def _recv_exact(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed")
            buf += chunk
        return buf

    def _write_frame(self, ptr, bgr):
        yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)  # (H*3/2, W)
        h, w = self._height, self._width
        nv12 = np.empty(self._frame_size, dtype=np.uint8)
        nv12[:w * h] = yuv[:h].ravel()
        nv12[w * h::2] = yuv[h: h + h // 4].ravel()
        nv12[w * h + 1::2] = yuv[h + h // 4: h + h // 2].ravel()

        if self._mutex:
            self._k32.WaitForSingleObject(self._mutex, 5)
        hdr = struct.pack("<4IQ", w, h, w, self._frame_size, self._frame_index)
        ctypes.memmove(ptr, hdr, HEADER_SIZE)
        ctypes.memmove(ptr + HEADER_SIZE, nv12.ctypes.data, self._frame_size)
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
    root.geometry("320x200")
    root.resizable(False, False)

    status_var = tk.StringVar(value="Idle")
    res_var = tk.StringVar(value="1920x1080")
    fps_var = tk.StringVar(value="FPS: 0")

    bridge = Bridge()

    def update_ui():
        status_var.set(bridge.status_text)
        w, h = bridge.resolution
        res_var.set(f"{w}x{h}")
        fps_var.set(f"FPS: {bridge.fps:.1f}")
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

    def set_res(w, h):
        bridge.set_resolution(w, h)
        root.after(0, update_ui)

    def res_action(w, h):
        # pystray rejects lambdas with default args; use a closure factory
        return lambda icon, item: set_res(w, h)

    def res_checked(w, h):
        return lambda item: bridge.resolution == (w, h)

    def on_quit(icon=None, item=None):
        bridge.stop()
        root.quit()
        root.destroy()

    tk.Label(root, textvariable=status_var, font=("Segoe UI", 11)).pack(pady=8)
    tk.Label(root, textvariable=res_var, font=("Segoe UI", 9)).pack()
    tk.Label(root, textvariable=fps_var, font=("Segoe UI", 9)).pack()
    btn_row = tk.Frame(root)
    btn_row.pack(pady=10)
    tk.Button(btn_row, text="Start", width=8, command=start).pack(side="left", padx=4)
    tk.Button(btn_row, text="Stop", width=8, command=stop).pack(side="left", padx=4)
    tk.Button(btn_row, text="Restart ADB", width=12, command=restart_adb).pack(side="left", padx=4)

    res_menu = pystray.Menu(
        *[MenuItem(f"{w}x{h}", res_action(w, h), checked=res_checked(w, h))
          for w, h in RESOLUTIONS]
    )
    menu = pystray.Menu(
        MenuItem("Show Window", lambda icon, item: root.deiconify(), default=True),
        MenuItem("Start", lambda icon, item: start(), enabled=lambda item: not bridge.running),
        MenuItem("Stop", lambda icon, item: stop(), enabled=lambda item: bridge.running),
        MenuItem("Resolution", res_menu),
        MenuItem("Restart ADB", lambda icon, item: restart_adb()),
        Menu.SEPARATOR,
        MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon("BestCam", _make_icon(), "BestCam Bridge", menu)

    root.protocol("WM_DELETE_WINDOW", lambda: root.withdraw())
    threading.Thread(target=icon.run, daemon=True).start()
    root.after(500, update_ui)
    root.mainloop()


if __name__ == "__main__":
    main()
