"""HAL：电机（TB1 / TB6612，gpiozero）。原子能力：带符号双轮 PWM + 停。"""
from __future__ import annotations

from leko.core.config import CONFIG


class Motors:
    """双电机。drive(sl, sr)：带符号 PWM，正=车头（前驱驱动轮端）前进。"""

    def __init__(self):
        from gpiozero import Motor
        pins = CONFIG["pins"]

        def make(side: str) -> Motor:
            d = dict(pins[f"motor_{side}"])
            f, b, pwm = d["in1"], d["in2"], d["pwm"]
            if d.get("invert"):          # 接线与定义相反 → 代码翻转，不动线
                f, b = b, f
            return Motor(forward=f, backward=b, enable=pwm, pwm=True)

        self.left, self.right = make("left"), make("right")

    def drive(self, sl: float, sr: float):
        if sl >= 0:
            self.left.forward(sl)
        else:
            self.left.backward(-sl)
        if sr >= 0:
            self.right.forward(sr)
        else:
            self.right.backward(-sr)

    def stop(self):
        self.left.stop()
        self.right.stop()
