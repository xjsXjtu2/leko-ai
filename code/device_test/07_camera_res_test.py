#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UVC 分辨率探测：MJPG 是否解锁 2K（绿联一体机）。"""
import time

import cv2

for name, fourcc, w, h in [("MJPG", cv2.VideoWriter_fourcc(*"MJPG"), 2560, 1440),
                            ("YUYV/default", 0, 2560, 1440),
                            ("MJPG-1080p", cv2.VideoWriter_fourcc(*"MJPG"), 1920, 1080)]:
    cap = cv2.VideoCapture(0)
    if fourcc:
        cap.set(cv2.CAP_PROP_FOURCC, fourcc)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    for _ in range(6):
        cap.grab()
        time.sleep(0.1)
    ok, f = cap.read()
    got = f"{f.shape[1]}x{f.shape[0]}" if ok else "FAIL"
    actual_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    actual_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    print(f"{name:12} 请求 {w}x{h} → 实际 {int(actual_w)}x{int(actual_h)} / 帧 {got}")
    cap.release()
