"""ctrl：驾驶（里程闭环）—— 直线 / 弧线 / 原地转，50Hz，语音急停可打断。

运动指令统一 schema（07 §3.2，ROS Twist 风格）由 main 下发；
本模块只管"走到目标里程"，安全红线（急停/堵转/限距）内嵌在闭环里。"""
from __future__ import annotations

import math
import queue
import threading
import time

from leko.core.config import CONFIG, load_calib
from leko.ctrl.safety import STALL_MS
from leko.hal.encoder import Encoders
from leko.hal.motor import Motors
from leko.voice.tts import speak

SPEED = CONFIG["motion"]["speed_pwm"]          # 行驶 PWM（无雷达限速）
WHEEL_BASE = CONFIG["motion"]["wheel_base_m"]
STRAIGHT_K = CONFIG["motion"]["straight_k"]   # 直线修正增益
TURN_SIGN = CONFIG["motion"]["turn_sign"]     # 实车转向反了 → config 改 -1
TICK = CONFIG["motion"]["tick_s"]             # 50Hz


class Pilot:
    """差速驾驶（原 05 脚本 Driver，逻辑逐行保留）。"""

    def __init__(self, moving: threading.Event | None = None):
        self.motors = Motors()
        self.enc = Encoders()
        c = load_calib()
        self.l_spm, self.r_spm = c["l_spm"], c["r_spm"]
        self.wheel_base = c["wheel_base"]
        self.moving = moving or threading.Event()   # 行驶中=True：监听保持 ARMED（"停"能打断）
        print(f"   🛞 {self.l_spm:.0f}/{self.r_spm:.0f} 步/米，轮距 {self.wheel_base} m")

    # ---------------- 底层 ----------------

    def _stop(self):
        self.motors.stop()

    def _drive(self, sl, sr):
        self.motors.drive(sl, sr)

    # ---------------- 50Hz 闭环 ----------------

    def _run(self, tl_m, tr_m, cmd_q: queue.Queue, speed=SPEED):
        """左右轮目标里程（米，带符号）→ 闭环：目标达成/堵转/语音急停。"""
        tl, tr = tl_m * self.l_spm, tr_m * self.r_spm
        at_l, at_r = abs(tl), abs(tr)
        if at_l < 1 and at_r < 1:
            return "noop"
        self.enc.reset()
        e_l, e_r = self.enc.left, self.enc.right
        last = (e_l.steps, e_r.steps)
        last_t = time.monotonic()
        pl = pr = speed
        self.moving.set()
        try:
            while True:
                # ① 语音急停（唯一可打断项；行驶中忽略其它指令）
                while not cmd_q.empty():
                    c = cmd_q.get_nowait()
                    if c.get("op") == "stop":
                        self._stop()
                        speak("已停止")
                        print("   ⏹ 语音急停")
                        return "stop"
                # ② 目标达成。到点的那一侧立刻停，不能等慢的一侧
                #    （原地转时慢轮还在走，快轮会多转好几圈）
                prog_l = abs(e_l.steps) / at_l if at_l else 1.0
                prog_r = abs(e_r.steps) / at_r if at_r else 1.0
                if prog_l >= 0.99 and prog_r >= 0.99:
                    break
                # ③ 直线修正：两轮同向且目标相近时，右轮追左轮里程
                straight = (tl * tr > 0) and abs(at_l - at_r) <= max(at_l, at_r) * 0.02
                if straight and prog_l < 0.99 and prog_r < 0.99:
                    pr = speed + STRAIGHT_K * (abs(e_l.steps) / self.l_spm
                                               - abs(e_r.steps) / self.r_spm)
                    pr = max(0.12, min(0.9, pr))
                    pl = speed
                else:                              # 弧线/原地转：按目标比例给速
                    ratio = (at_r / at_l) if at_l and at_r else 1.0
                    pr = min(0.7, speed * max(1.0, ratio))
                    pl = min(0.7, speed * max(1.0, 1.0 / ratio if ratio else 1.0))
                if prog_l >= 0.99:
                    pl = 0
                if prog_r >= 0.99:
                    pr = 0
                self._drive((1 if tl >= 0 else -1) * pl, (1 if tr >= 0 else -1) * pr)
                # ④ 堵转：还在驱动的轮 500ms 编码器不动
                now = time.monotonic()
                cur = (e_l.steps, e_r.steps)
                if cur == last and now - last_t > STALL_MS / 1000:
                    self._stop()
                    speak("卡住了")
                    print("   ⚠ 堵转，已断电")
                    return "stuck"
                if cur != last:
                    last, last_t = cur, now
                time.sleep(TICK)
        finally:
            self._stop()
            self.moving.clear()
        lm, rm = abs(e_l.steps) / self.l_spm, abs(e_r.steps) / self.r_spm
        print(f"   ✔ 实走：左 {lm:.2f} m / 右 {rm:.2f} m（目标 {abs(tl_m):.2f}）")
        return "done"

    # ---------------- 动作 API ----------------

    def forward(self, dist, q):
        return self._run(dist, dist, q)

    def reverse(self, dist, q):
        return self._run(-dist, -dist, q)

    def turn(self, deg, q):
        """原地转；deg>0=右转。无 IMU，靠里程计，误差会累积（v0 接受）。
        实车方向反了 → config.yaml motion.turn_sign 改 -1。"""
        deg *= TURN_SIGN
        p = self.wheel_base / 2 * math.radians(abs(deg))       # 每轮弧长
        if deg > 0:      # 右转：左轮前进、右轮后退
            return self._run(p, -p, q, speed=min(SPEED, 0.3))
        return self._run(-p, p, q, speed=min(SPEED, 0.3))

    def arc(self, deg, dist, q):
        """定曲率弧线：走 dist 米、航向变化 deg 度（左前方=−45°）。
        内轮走 (R−W/2)θ，外轮走 (R+W/2)θ。"""
        deg *= TURN_SIGN
        th = math.radians(abs(deg))
        if th <= 0:
            return "noop"
        radius = dist / th
        half = self.wheel_base / 2
        inner = (radius - half) * th
        outer = (radius + half) * th
        if deg < 0:                      # 左前方：左轮在内
            return self._run(inner, outer, q)
        return self._run(outer, inner, q)  # 右前方：右轮在内
