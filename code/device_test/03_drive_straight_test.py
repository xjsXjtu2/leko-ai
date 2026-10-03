#!/usr/bin/env python3
"""测试3：开环走直线（PID 的起点）。
用法: python3 03_drive_straight.py   （地面需要 >2m 直线）
同 PWM 跑 2 秒，比较两轮编码器步数差 → 得出左右轮偏差系数。
之后做 PID：右轮跟随左轮里程，Kp 从 0.5 开始。
"""
from gpiozero import RotaryEncoder, Motor
from time import sleep

enc_l = RotaryEncoder(a=5, b=6, max_steps=0)
enc_r = RotaryEncoder(a=25, b=16, max_steps=0)
# 2026-10-02 实车：右电机对调 17/27（= config motor_right.invert），
# 左电机保持 23/24 → forward() = 车头（前驱驱动轮端）前进
left = Motor(forward=23, backward=24, enable=12, pwm=True)
right = Motor(forward=27, backward=17, enable=13, pwm=True)

SPEED = 0.4
RUN = 2.0

input("把车放在直线起点，轮子着地。回车开始...")
enc_l.steps = 0
enc_r.steps = 0
left.forward(speed=SPEED)
right.forward(speed=SPEED)
sleep(RUN)
left.stop(); right.stop()
sleep(0.3)

l, r = abs(enc_l.steps), abs(enc_r.steps)
print(f"左 {l} 步, 右 {r} 步")
if l and r:
    ratio = r / l
    print(f"右/左 步数比 = {ratio:.3f}")
    print(f"→ PID 初值建议：右轮目标 PWM = {SPEED} × {1/ratio:.3f}")
    print("（本轮先不做闭环；比值稳定后再进 PID，参数 Kp=0.5 起）")
else:
    print("⚠️ 有编码器 0 步，先回到测试 2 排查")
