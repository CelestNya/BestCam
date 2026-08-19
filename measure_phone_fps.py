# -*- coding: utf-8 -*-
"""测手机流帧率：adb forward tcp:8080 直连手机流服务，数 N 秒帧。
同时支持 MJPEG (image/jpeg) 与 H.264 (video/h264) multipart 流。"""
import socket
import time
import sys

PORT = 8080
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
BOUNDARY = b"--boundary"

def recv_until(sock, pending, needle, chunk=4096, timeout_ms=100):
    while needle not in pending:
        pending += sock.recv(chunk)
    idx = pending.find(needle)
    return pending[:idx], pending[idx + len(needle):]

sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
sock.settimeout(10)
buf = b""
# consume HTTP header
_, buf = recv_until(sock, buf, b"\r\n\r\n")

count = 0
sizes = []
intervals = []
prev = None
mime = None
t0 = time.perf_counter()
while time.perf_counter() - t0 < DURATION:
    # find next boundary
    _, buf = recv_until(sock, buf, BOUNDARY)
    # read part headers
    headers, buf = recv_until(sock, buf, b"\r\n\r\n")
    cl = None
    for line in headers.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            cl = int(line.split(b":", 1)[1].strip())
        if line.lower().startswith(b"content-type:"):
            mime = line.split(b":", 1)[1].strip().decode(errors="replace")
    if cl is None:
        continue
    while len(buf) < cl:
        buf += sock.recv(4096)
    frame = buf[:cl]
    buf = buf[cl:]
    now = time.perf_counter()
    if prev is not None:
        intervals.append((now - prev) * 1000)
    prev = now
    count += 1
    sizes.append(len(frame))

wall = time.perf_counter() - t0
if intervals:
    intervals.sort()
    n = len(intervals)
    med = intervals[n // 2]
    p95 = intervals[min(n - 1, int(n * 0.95))]
    codec = mime or "unknown"
    print(f"手机流 ({codec}): {count} 帧/{wall:.1f}s = {count/wall:.1f} fps | "
          f"帧间隔 中位 {med:.1f}ms 均值 {sum(intervals)/n:.1f}ms p95 {p95:.1f}ms | "
          f"帧大小 min={min(sizes)} max={max(sizes)} avg={sum(sizes)//len(sizes)}")
else:
    print(f"手机流: 0 帧（{wall:.1f}s）")
sock.close()
