"""HAL：摄像头（绿联 2K 一体机 UVC，OpenCV 采集）—— 听写拍照（F2.0）已实装。

capture(path) → (ok, reason)：预热等自动对焦稳定 → 2K 抓帧 → 清晰度自检
（拉普拉斯方差，糊了返回 False，由上层提示重拍）。"""
from __future__ import annotations

from pathlib import Path

_WARMUP_S = 1.5          # 预热时长：自动对焦 + 曝光稳定
_WARMUP_FRAMES = 8
_BLUR_MIN = 60.0          # 拉普拉斯方差下限（实测标定；04_av 的思路）
_JPG_QUALITY = 92


class Camera:
    """绿联一体机 UVC：capture=2560x1440，自动对焦。"""

    def __init__(self, index: int = 0, blur_min: float = _BLUR_MIN):
        self.index = index
        self.blur_min = blur_min
        self._cap = None

    def _open(self):
        import cv2
        if self._cap is None or not self._cap.isOpened():
            self._cap = cv2.VideoCapture(self.index)
            # 实测（device_test/07）：默认 YUYV 只有 960x720，MJPG 才能解锁 2K
            self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 2560)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1440)
        return self._cap

    def capture(self, out: Path) -> tuple[bool, str]:
        """拍一张 → (ok, reason)。糊/失败返回 False，上层语音提示重拍。"""
        import cv2
        import time
        cap = self._open()
        if not cap.isOpened():
            return False, "摄像头打不开（lsusb 查 UVC 设备）"
        # 预热：丢掉前几帧，等自动对焦/曝光稳定
        deadline = time.monotonic() + _WARMUP_S
        while time.monotonic() < deadline:
            cap.grab()
            time.sleep(0.12)
        ok, frame = cap.read()
        if not ok or frame is None:
            return False, "抓帧失败"
        if frame.shape[0] < 1000:                     # 分辨率没起来（被驱动压低）
            pass                                       # 不拦，只在糊时拦
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        import numpy as np
        blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if blur < self.blur_min:
            return False, f"糊了（清晰度 {blur:.0f} < {self.blur_min:.0f}），挪近一点或开灯"
        cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, _JPG_QUALITY])
        return True, f"清晰度 {blur:.0f}，{frame.shape[1]}x{frame.shape[0]}"

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None
