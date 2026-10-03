#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拍照对焦探针：MJPG 2K，3s 预热连拍两张，存盘 + 拉普拉斯方差。"""
import time
from pathlib import Path

import cv2

OUT = Path.home() / "leko-ai" / "av_out"
OUT.mkdir(parents=True, exist_ok=True)
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 2560)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1440)
for i in range(30):                       # ~3s 预热等自动对焦
    cap.grab()
    time.sleep(0.1)
for n in (1, 2):
    ok, f = cap.read()
    if not ok:
        print(f"#{n} FAIL")
        continue
    p = OUT / f"focus_probe_{n}.jpg"
    cv2.imwrite(str(p), f, [cv2.IMWRITE_JPEG_QUALITY, 92])
    blur = cv2.Laplacian(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
    print(f"#{n} {f.shape[1]}x{f.shape[0]} 清晰度={blur:.0f} → {p.name}")
    time.sleep(1.0)
cap.release()
