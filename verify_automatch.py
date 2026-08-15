"""verify_automatch.py — End-to-end test of auto-resolution matching.

Launches BestCamHost + the new companion (auto-match logic), then simulates a
client (cv2) negotiating each supported resolution and verifies that:
  1. The driver publishes the negotiated size in the header (desired*).
  2. The companion switches its output to match (header width/height).
  3. The received frame is color-sane (no NV12 mismatch -> no black/garbled).

Usage: python verify_automatch.py <companion.py path> <host path>
"""
import ctypes
import queue
import struct
import subprocess
import sys
import threading
import time

import cv2

SHARED_MEM_NAME = "Global\\BestCam_SharedMem"
REQS = [(1920, 1080), (1280, 720), (640, 480), (800, 600)]


class Hdr:
    def __init__(self):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenFileMappingW(0x0002 | 0x0004, False, SHARED_MEM_NAME)
        if not h:
            raise RuntimeError("shared memory not found")
        ptr = k32.MapViewOfFile(h, 0x0002 | 0x0004, 0, 0, 32)
        if not ptr:
            raise RuntimeError("MapViewOfFile failed")
        self._k32, self._h, self._ptr = k32, h, ptr

    def read(self):
        raw = ctypes.string_at(self._ptr, 32)
        w, h, _, _, idx, dw, dh = struct.unpack("<4IQII", raw)
        return w, h, idx, dw, dh


def wait_for(hdr, want_out, want_desired, timeout=8.0, label=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        w, h, idx, dw, dh = hdr.read()
        out_ok = (w, h) == want_out
        des_ok = (dw, dh) == want_desired
        if out_ok and des_ok:
            print(f"  [{label}] OK after {time.time()-t0:.1f}s: "
                  f"out={w}x{h} desired={dw}x{dh} idx={idx}")
            return True
        time.sleep(0.3)
    w, h, idx, dw, dh = hdr.read()
    print(f"  [{label}] TIMEOUT: out={w}x{h} (want {want_out}) "
          f"desired={dw}x{dh} (want {want_desired}) idx={idx}")
    return False


def main():
    companion_py, host_exe = sys.argv[1], sys.argv[2]
    # The host blocks on getchar(), so it must keep a console: use SW_HIDE
    # (CREATE_NO_WINDOW would make getchar() return immediately and exit).
    # stdin=PIPE keeps the inherited stdin from hitting EOF (which would
    # otherwise return from getchar() instantly when run from a pipe shell).
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    host = subprocess.Popen([host_exe], startupinfo=startupinfo,
                            stdin=subprocess.PIPE)
    try:
        companion = subprocess.Popen(
            [sys.executable, "-u", companion_py],  # -u: unbuffered output
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except Exception as exc:
        print("companion failed:", exc)
        return 1

    # Pump companion output through a queue so the wait loop below can time
    # out (readline() on a pipe would block forever on Windows).
    out_q = queue.Queue()

    def pump():
        for line in companion.stdout:
            out_q.put(line)

    threading.Thread(target=pump, daemon=True).start()

    try:
        # Wait for the companion to start streaming (phone stream present?)
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                line = out_q.get(timeout=0.5)
            except queue.Empty:
                continue
            print("  [companion]", line.rstrip())
            if "Stream connected" in line:
                break
        else:
            print("companion never connected to the phone stream")
            return 1

        time.sleep(2)
        hdr = Hdr()
        w, h, idx, dw, dh = hdr.read()
        print(f"initial header: out={w}x{h} idx={idx} desired={dw}x{dh}")

        all_ok = True
        for rw, rh in REQS:
            print(f"== client requests {rw}x{rh}")
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not cap.isOpened():
                print(f"  request {rw}x{rh}: FAILED to open camera")
                all_ok = False
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, rw)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, rh)
            time.sleep(1.0)
            if not wait_for(hdr, (rw, rh), (rw, rh), timeout=8.0, label="match"):
                all_ok = False
            # color sanity: read a frame, mean BGR should be near-neutral.
            # Skip the first few frames: DSHOW buffers frames from the probe
            # phase (old negotiated size) and re-parses them after the client
            # switches resolution, which can briefly render a garbled frame
            # even though the driver itself never delivers mismatched data.
            ok_f = False
            for _ in range(10):
                ret, frame = cap.read()
                if ret:
                    if _ < 3:
                        continue
                    ok_f = True
                    break
            cap.release()
            if ok_f:
                b, g, r = (frame[:, :, i].mean() for i in range(3))
                spread = max(b, g, r) - min(b, g, r)
                print(f"  frame {frame.shape[1]}x{frame.shape[0]} "
                      f"BGR=({b:.0f},{g:.0f},{r:.0f}) spread={spread:.0f} "
                      f"-> {'OK' if spread < 60 else 'GARBLED'}")
                if spread >= 60:
                    all_ok = False
            else:
                print(f"  request {rw}x{rh}: NO FRAME")
                all_ok = False

        print("=" * 50)
        print("ALL PASS" if all_ok else "FAILURES PRESENT")
        return 0 if all_ok else 1
    finally:
        companion.kill()
        host.kill()


if __name__ == "__main__":
    sys.exit(main())
