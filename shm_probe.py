# -*- coding: utf-8 -*-
"""读 BestCam 共享内存，统计写入帧率与分辨率（验证 companion 链路）"""
import ctypes
import time
import sys
from ctypes import wintypes

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0

GENERIC_READ = 0x80000000
FILE_MAP_READ = 0x0004

h = ctypes.windll.kernel32.OpenFileMappingW(FILE_MAP_READ, False, "Global\\BestCam_SharedMem")
if not h:
    print("OpenFileMapping 失败, GetLastError =", ctypes.windll.kernel32.GetLastError())
    sys.exit(1)

p = ctypes.windll.kernel32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 32)
if not p:
    print("MapViewOfFile 失败, GetLastError =", ctypes.windll.kernel32.GetLastError())
    sys.exit(1)

arr = (ctypes.c_uint32 * 6).from_address(p)
idx_arr = (ctypes.c_uint64 * 1).from_address(p + 16)

prev = -1
count = 0
t0 = time.perf_counter()
last_t = t0
while time.perf_counter() - t0 < DURATION:
    idx = idx_arr[0]
    if idx != prev:
        count += 1
        prev = idx
        if count == 1:
            last_t = time.perf_counter()
wall = time.perf_counter() - t0
w, hgt, stride, frame_size = arr[0], arr[1], arr[2], arr[3]
print(f"共享内存: {w}x{hgt} (stride {stride}, frame {frame_size}B), 帧数 {count} / {wall:.1f}s = {count/wall:.1f} fps")
ctypes.windll.kernel32.UnmapViewOfFile(p)
ctypes.windll.kernel32.CloseHandle(h)
