#!/usr/bin/env python3
"""测试2：编码器读数。
用法: python3 02_encoder_test.py
第一步用手转轮子看脉冲计数；第二步电机带动跑 3 秒对比两轮。
"""
from gpiozero import RotaryEncoder, Motor
from time import sleep

enc_l = RotaryEncoder(a=5, b=6, max_steps=0)      # 左轮
enc_r = RotaryEncoder(a=25, b=16, max_steps=0)    # 右轮
left = Motor(forward=23, backward=24, enable=12, pwm=True)
right = Motor(forward=27, backward=17, enable=13, pwm=True)  # 右轮与定义相反（= config motor_right.invert）：forward() = 前驱车头方向

print("== 第一步：用手分别正/反转左右轮各几圈，观察计数方向 ==")
for _ in range(6):
    print(f"左 {enc_l.steps:>8}   右 {enc_r.steps:>8}")
    sleep(1)

print("== 第二步：两轮同速跑 3 秒 ==")
enc_l.steps = 0
enc_r.steps = 0
left.forward(speed=0.4)
right.forward(speed=0.4)
sleep(3)
left.stop(); right.stop()
l, r = abs(enc_l.steps), abs(enc_r.steps)
print(f"左 {l} 步, 右 {r} 步, 差值 {abs(l-r)}")
if l == 0 or r == 0:
    print("⚠️ 某路为 0：查编码器 3.3V 供电与 A 相接线")
elif abs(l - r) > (l + r) * 0.1:
    print("⚠️ 两轮差 >10%：先记录，PID 阶段用它修正")
else:
    print("✅ 编码器正常")
