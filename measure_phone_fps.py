# -*- coding: utf-8 -*-
"""测手机流帧率：adb forward tcp:8080 直连手机 MJPEG 服务，数 6 秒 JPEG 帧"""
import socket
import time
import struct
import sys

PORT = 8080
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0

sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
# consume until first image boundary
first = sock.recv(4096)
while b"\r\n\r\n" not in first:
    first += sock.recv(4096)
pending = first.split(b"\r\n\r\n", 1)[1]
sock.settimeout(10)

count = 0
sizes = set()
intervals = []
prev = None
t0 = time.perf_counter()
while time.perf_counter() - t0 < DURATION:
    if len(pending) < 4:
        pending += sock.recv(4096)
    soi = pending.find(b"\xff\xd8")
    if soi < 0:
        pending += sock.recv(4096)
        continue
    head = pending[:soi]
    cl = None
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            cl = int(line.split(b":")[1].strip())
    if cl is None:
        pending = pending[soi:] + sock.recv(4096)
        continue
    while len(pending) - soi < cl:
        pending += sock.recv(4096)
    jpeg = pending[soi: soi + cl]
    pending = pending[soi + cl:]
    now = time.perf_counter()
    if prev is not None:
        intervals.append((now - prev) * 1000)
    prev = now
    count += 1
    sizes.add(len(jpeg))

wall = time.perf_counter() - t0
if intervals:
    intervals.sort()
    n = len(intervals)
    med = intervals[n // 2]
    p95 = intervals[min(n - 1, int(n * 0.95))]
    print(f"手机流: {count} 帧/{wall:.1f}s = {count/wall:.1f} fps | "
          f"帧间隔 中位 {med:.1f}ms 均值 {sum(intervals)/n:.1f}ms p95 {p95:.1f}ms | "
          f"JPEG 大小 {sizes}")
else:
    print(f"手机流: 0 帧（{wall:.1f}s）")
sock.close()
