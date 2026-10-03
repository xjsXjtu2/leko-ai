#!/usr/bin/env python3
"""测试1：让两个轮子转起来。
用法: python3 01_motor_test.py
两轮依次前进/后退，速度渐变。轮子必须悬空！
"""
from gpiozero import Motor
from time import sleep

# BCM 编号，见 05-接线与调试手册
left = Motor(forward=23, backward=24, enable=12, pwm=True)
right = Motor(forward=27, backward=17, enable=13, pwm=True)  # 右轮与定义相反（= config motor_right.invert）：forward() = 前驱车头方向


def ramp(motor, name):
    print(f"--- {name}: 渐进前进 ---")
    for s in [0.3, 0.5, 0.7, 1.0]:
        motor.forward(speed=s)
        print(f"  speed={s}")
        sleep(1.5)
    motor.stop()
    sleep(0.5)
    print(f"--- {name}: 后退 ---")
    motor.backward(speed=0.5)
    sleep(2)
    motor.stop()
    sleep(0.5)


try:
    ramp(left, "左轮")
    ramp(right, "右轮")
    print("=== 两轮同进 2 秒 ===")
    left.forward(speed=0.5)
    right.forward(speed=0.5)
    sleep(2)
    left.stop(); right.stop()
    print("测试完成 ✅  若某轮不转/转向相反，见手册第四节排查。")
except KeyboardInterrupt:
    left.stop(); right.stop()
    print("已停止")
