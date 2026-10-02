"""HAL：编码器（gpiozero RotaryEncoder）。原子能力：双轮脉冲计数 + 里程。"""
from __future__ import annotations

from leko.core.config import CONFIG


class Encoders:
    def __init__(self):
        from gpiozero import RotaryEncoder
        pins = CONFIG["pins"]
        self.left = RotaryEncoder(**pins["encoder_left"], max_steps=0)
        self.right = RotaryEncoder(**pins["encoder_right"], max_steps=0)

    def reset(self):
        self.left.steps = self.right.steps = 0

    def meters(self, calib: dict) -> tuple[float, float]:
        """左右轮已走里程（米，绝对值），calib 来自 core.config.load_calib()。"""
        return abs(self.left.steps) / calib["l_spm"], abs(self.right.steps) / calib["r_spm"]
