# -*- coding: utf-8 -*-
"""companion 主循环内存泄漏定位：连接手机流，模拟 imdecode+转换循环（不写共享内存），
tracemalloc 每 30s 打印增长最快的对象类型。"""
import socket
import time
import tracemalloc
import cv2
import numpy as np

PORT = 8080

sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
first = sock.recv(4096)
while b"\r\n\r\n" not in first:
    first += sock.recv(4096)
pending = first.split(b"\r\n\r\n", 1)[1]
sock.settimeout(10)

target_w, target_h = 800, 450


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def bgr_to_nv12(bgr):
    yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
    flat = yuv.ravel()
    nv12 = np.empty(target_w * target_h * 3 // 2, dtype=np.uint8)
    nv12[: target_w * target_h] = flat[: target_w * target_h]
    nv12[target_w * target_h::2] = flat[target_w * target_h: target_w * target_h + target_w * target_h // 4]
    nv12[target_w * target_h + 1::2] = flat[target_w * target_h + target_w * target_h // 4: target_w * target_h + target_w * target_h // 2]
    return nv12


tracemalloc.start(10)
t0 = time.perf_counter()
count = 0
snap0 = None

while time.perf_counter() - t0 < 180:
    if len(pending) < 4:
        pending += recv_exact(sock, 4 - len(pending))
    soi = pending.find(b"\xff\xd8")
    if soi < 0:
        pending += recv_exact(sock, 4096)
        continue
    head = pending[:soi]
    cl = None
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            cl = int(line.split(b":")[1].strip())
    if cl is None:
        pending = pending[soi:] + recv_exact(sock, 4096)
        continue
    while len(pending) - soi < cl:
        pending += recv_exact(sock, 4096)
    jpeg = pending[soi: soi + cl]
    pending = pending[soi + cl:]

    bgr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        continue
    if bgr.shape[1] != target_w or bgr.shape[0] != target_h:
        bgr = cv2.resize(bgr, (target_w, target_h), interpolation=cv2.INTER_AREA)
    nv12 = bgr_to_nv12(bgr)
    count += 1

    now = time.perf_counter()
    if int(now - t0) % 30 == 0 and int(now - t0) != int((now - 0.1) - t0):
        snap = tracemalloc.take_snapshot()
        if snap0 is None:
            snap0 = snap
            print(f"t={int(now-t0)}s 基线已记录 ({count}帧)")
        else:
            diff = snap.compare_to(snap0, "lineno")
            total_growth = sum(d.size_diff for d in diff)
            print(f"t={int(now-t0)}s 帧={count} 相对基线总增长={total_growth/1024:.0f}KB")
            for stat in diff[:8]:
                if stat.size_diff > 0:
                    print(f"  {stat.size_diff/1024:8.0f}KB {stat.count_diff:6d} {stat.traceback.format()[-1] if stat.traceback else '?'}")
            snap0 = snap

sock.close()
print(f"完成: {count} 帧")
