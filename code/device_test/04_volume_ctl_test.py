#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扬声器音量查询 / 设置（树莓派 ALSA 与 macOS 通用，纯标准库）。

为什么单独做一个脚本
--------------------
绿联一体机这块 USB 音频口的 `PCM` 播放音量**默认就是 0%**，而
`/var/lib/alsa/asound.state` 里存的也常常是 0%，于是每次开机
`alsa-restore` 都会把 0% 恢复回来。表现极具误导性 —— `aplay` 返回 0、
耗时也和音频时长一致，但喇叭一声不响。用这个脚本一眼就能看出真相。

用法
----
::

    python3 volume_ctl.py                  # 查询（自动定位绿联声卡）
    python3 volume_ctl.py 80               # 设为 80%
    python3 volume_ctl.py 100 -u           # 设为 100% 并取消静音
    python3 volume_ctl.py mute             # 静音
    python3 volume_ctl.py unmute           # 取消静音（音量不变）
    python3 volume_ctl.py --list           # 列出所有可播放声卡及其音量
    python3 volume_ctl.py 100 -u --store   # 设完顺手持久化（需要 sudo）
    python3 volume_ctl.py -j               # JSON 输出，便于其它程序调用

退出码：0 成功；1 失败（未找到声卡 / amixer 报错 / 回读校验不一致）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# 不同声卡的播放音量控件命名不一，按这个顺序挑第一个存在的
CONTROL_PREFERENCE = ("PCM", "Master", "Speaker", "Headphone",
                      "Playback", "Digital", "LineOut")
# 用来认出绿联一体机的关键词（card id / 描述）
UGREEN_HINTS = ("ugreen", "绿联", "u2k", "camera")


# ---------------------------------------------------------------- 基础工具

def _paint(s: str, code: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def ok(s):   return _paint(s, "32")
def bad(s):  return _paint(s, "31")
def warn(s): return _paint(s, "33")
def info(s): return _paint(s, "36")


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(cmd, timeout=15):
    """执行命令 → (returncode, stdout, stderr)，永不抛异常。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
    except Exception as e:                                     # noqa: BLE001
        return 1, "", str(e)


# ---------------------------------------------------------------- 设备发现

def list_playback_devices():
    """`aplay -l` → 可播放设备列表（按 card 去重，保留第一个 device）。

    注意 device 名几乎都含空格（`device 0: USB Audio [USB Audio]`），
    所以不能像早期版本那样用 \\S+，否则整行都匹配不上。
    """
    devs, seen = [], set()
    if not (IS_LINUX and have("aplay")):
        return devs
    rc, o, _ = run(["aplay", "-l"])
    for line in o.splitlines():
        m = re.search(r"card (\d+): (.+?) \[(.*?)\], "
                      r"device (\d+): (.+?) \[(.*?)\]", line)
        if not m:
            continue
        card, card_id, card_name, dev, dev_name = (
            m.group(1), m.group(2).strip(), m.group(3).strip(),
            m.group(4), m.group(6).strip())
        if card in seen:
            continue
        seen.add(card)
        devs.append({
            "card": int(card), "card_id": card_id, "card_name": card_name,
            "dev": int(dev), "dev_name": dev_name,
            "plughw": f"plughw:{card},{dev}",
            "name": f"{card_name} / {dev_name}",
        })
    return devs


def is_ugreen(d) -> bool:
    blob = " ".join(str(d.get(k, "")) for k in
                    ("card_id", "card_name", "dev_name")).lower()
    return any(h in blob for h in UGREEN_HINTS)


def find_ugreen_card():
    """优先读 /proc/asound/cards（最权威），找不到再退回 aplay -l。"""
    if IS_LINUX:
        try:
            txt = Path("/proc/asound/cards").read_text(
                encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            txt = ""
        for m in re.finditer(r"^\s*(\d+)\s*\[(.+?)\s*\]\s*:\s*(.*)$", txt, re.M):
            blob = (m.group(2) + " " + m.group(3)).lower()
            if any(h in blob for h in UGREEN_HINTS):
                return int(m.group(1))
    for d in list_playback_devices():
        if is_ugreen(d):
            return d["card"]
    return None


def match_device(pat: str):
    """--device 匹配：支持 `camera`、`U2K`、`plughw:2,0`、`2`。"""
    low = pat.strip().lower()
    for d in list_playback_devices():
        blob = " ".join(str(v) for v in d.values()).lower()
        if low in blob:
            return d
    if low.isdigit():
        for d in list_playback_devices():
            if d["card"] == int(low):
                return d
    return None


# ---------------------------------------------------------------- 音量读写

def pick_control(card: int, want=None):
    """选混音器控件名：优先常见的 PCM，否则列出所有简单控件再挑。"""
    if want:
        return want
    rc, o, _ = run(["amixer", "-c", str(card), "scontrols"])
    names = re.findall(r"Simple mixer control '([^']+)'", o)
    for p in CONTROL_PREFERENCE:
        if p in names:
            return p
    return names[0] if names else None


def get_volume(card: int, control=None):
    """读播放音量 → dict；读不到返回 None。

    兼容两种 amixer 输出：
        单声道合并：`Mono: Playback 100 [100%] [0.39dB] [on]`
        多声道分离：`Front Left: Playback 100 [100%] [0.00dB] [on]`
    """
    if not (IS_LINUX and have("amixer")):
        return None
    control = pick_control(card, control)
    if not control:
        return None
    rc, o, e = run(["amixer", "-c", str(card), "sget", control])
    txt = o + "\n" + e
    if rc != 0:
        return None
    rows = re.findall(
        r"Playback\s+(\d+)\s+\[(\d+)%\]\s+\[([-\d.]+)dB\]\s+\[(on|off)\]", txt)
    if not rows:
        return None
    pcts = [int(r[1]) for r in rows]
    dbs = [float(r[2]) for r in rows]
    return {
        "card": card,
        "control": control,
        "percent": max(pcts),
        "db": max(dbs),
        "muted": any(r[3] == "off" for r in rows),
        "channels": len(rows),
        "per_channel": pcts,
    }


def set_volume(card: int, percent: int, control=None, unmute=False):
    """设音量 → (成功, 说明, 回读结果)。设置后一定回读校验。

    有些声卡会把百分比量化到硬件步进（如 0-31），所以只要"非零"就算成功，
    不苛求逐位相等。
    """
    control = pick_control(card, control)
    if not control:
        return False, "未找到可用的播放控件", None
    cmd = ["amixer", "-c", str(card), "sset", control, f"{percent}%"]
    if unmute:
        cmd.append("unmute")
    rc, _, e = run(cmd)
    if rc != 0:
        return False, (e.strip().splitlines() or ["amixer 失败"])[-1][:80], None
    after = get_volume(card, control)
    if after is None:
        return True, "已设置（但回读失败，请用查询复核）", None
    if after["percent"] == percent:
        return True, "", after
    if after["percent"] > 0 and percent > 0:
        return True, f"设备量化为 {after['percent']}%", after
    return False, f"设置后仍为 {after['percent']}%", after


def set_mute(card: int, muted: bool, control=None):
    control = pick_control(card, control)
    if not control:
        return False, "未找到可用的播放控件", None
    rc, _, e = run(["amixer", "-c", str(card), "sset", control,
                    "mute" if muted else "unmute"])
    if rc != 0:
        return False, (e.strip().splitlines() or ["amixer 失败"])[-1][:80], None
    return True, "", get_volume(card, control)


# ---------------------------------------------------------------- macOS

def mac_get_volume():
    rc, o, _ = run(["osascript", "-e", "output volume of (get volume settings)"])
    if rc != 0 or not o.strip().isdigit():
        return None
    rc2, o2, _ = run(["osascript", "-e",
                      "get volume settings"])   # 含 output muted
    muted = "output muted:true" in o2.lower()
    return {
        "card": None, "control": "system-output",
        "percent": int(o.strip()), "db": None, "muted": muted,
        "channels": 1, "per_channel": [int(o.strip())],
    }


def mac_set_volume(percent):
    run(["osascript", "-e", f"set volume output volume {percent}"])
    return mac_get_volume()


def mac_set_mute(muted: bool):
    run(["osascript", "-e", f"set volume {'with' if muted else 'without'} output muted"])
    return mac_get_volume()


# ---------------------------------------------------------------- 输出

def render(v: dict, card_info=None, plain=False):
    """人类可读的一屏摘要。"""
    lines = []
    lines.append(info("扬声器音量"))
    if card_info:
        lines.append(f"  声卡    card {card_info['card']}  "
                     f"{card_info['card_id']} [{card_info['card_name']}]"
                     + ("  ← 绿联一体机" if is_ugreen(card_info) else ""))
    if v:
        db = f"  ({v['db']:+.2f} dB)" if v.get("db") is not None else ""
        lines.append(f"  控件    {v['control']}")
        lines.append(f"  音量    {v['percent']}%{db}")
        lines.append(f"  状态    {'已静音' if v['muted'] else '未静音'}")
        if v["percent"] == 0 or v["muted"]:
            lines.append(warn("  ⚠ 音量为 0 / 已静音：aplay 会返回成功但喇叭完全不响"))
            lines.append(info("    修复： python3 volume_ctl.py 100 -u --store"))
        else:
            lines.append(ok("  ✓ 正常"))
    else:
        lines.append(bad("  读取失败：未找到控件或 amixer 不可用"))
    return "\n".join(lines)


def emit(v, args, card_info=None):
    if args.json:
        print(json.dumps({"device": card_info, "volume": v},
                         ensure_ascii=False, indent=2))
    else:
        print(render(v, card_info))


# ---------------------------------------------------------------- 主流程

def find_alsactl():
    """alsactl 常在 /usr/sbin，而非登录 shell 的 PATH 里没有它。"""
    return shutil.which("alsactl") or next(
        (p for p in ("/usr/sbin/alsactl", "/sbin/alsactl", "/usr/bin/alsactl")
         if Path(p).exists()), None)


def do_store():
    """调 sudo alsactl store 写存档（让它继承终端以便输密码）。"""
    exe = find_alsactl()
    if not exe:
        return False, "本机没有 alsactl（alsa-utils 未安装？）"
    print(info("写入 ALSA 存档（开机由 alsa-restore 自动恢复）…"))
    try:
        rc = subprocess.call(["sudo", exe, "store"])
    except Exception as e:                                     # noqa: BLE001
        return False, str(e)
    return (rc == 0), ("已写入 /var/lib/alsa/asound.state" if rc == 0
                       else "写入失败（无终端时 sudo 无法输入密码，请在本地终端执行）")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="volume_ctl.py",
        description="查询 / 设置扬声器音量（Pi 走 ALSA，macOS 走系统音量）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 volume_ctl.py\n"
               "  python3 volume_ctl.py 80\n"
               "  python3 volume_ctl.py 100 -u --store\n"
               "  python3 volume_ctl.py --list\n")
    ap.add_argument("volume", nargs="?",
                    help="目标音量 0-100；或 mute / unmute；省略则只查询")
    ap.add_argument("-c", "--card", type=int, help="指定声卡号")
    ap.add_argument("--device", help="按名字匹配声卡，如 camera / U2K / plughw:2,0")
    ap.add_argument("--control", help="指定混音器控件名（默认自动：PCM→Master→…）")
    ap.add_argument("-u", "--unmute", action="store_true", help="设音量时一并取消静音")
    ap.add_argument("-l", "--list", action="store_true", help="列出所有可播放声卡及音量")
    ap.add_argument("-j", "--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--store", action="store_true",
                    help="设置后执行 sudo alsactl store 持久化")
    ap.add_argument("--force", action="store_true", help="即使音量已达标也重新设置")
    args = ap.parse_args(argv)

    if not IS_LINUX and not IS_MAC:
        print(bad(f"当前平台（{sys.platform}）暂不支持"))
        return 1

    # ---------- --list ----------
    if args.list:
        if IS_MAC:
            v = mac_get_volume()
            print(info("macOS 走系统输出音量（不按声卡区分）"))
            print(render(v))
            return 0 if v else 1
        devs = list_playback_devices()
        if not devs:
            print(bad("没有找到可播放声卡（aplay -l 为空）"))
            return 1
        print(info("可播放声卡"))
        for d in devs:
            ctl = pick_control(d["card"], args.control)
            v = get_volume(d["card"], ctl) if ctl else None
            if v:
                shown = f"{v['percent']:>3}%" + ("  已静音" if v["muted"] else "")
                flag = bad("  ✗ 无声") if (v["percent"] == 0 or v["muted"]) else ""
            else:
                # HDMI 这类声卡的音量由接收端控制，本身没有混音器控件
                shown, flag = "  —", info("  无音量控件")
            mark = "  ← 绿联一体机" if is_ugreen(d) else ""
            print(f"  card {d['card']}  {d['card_id']:<10} {d['name']}"
                  f"   {shown}{flag}{mark}")
        return 0

    # ---------- macOS 分支 ----------
    if IS_MAC:
        action = (args.volume or "").strip().lower()
        if action in ("mute", "unmute"):
            v = mac_set_mute(action == "mute")
            print(ok(f"已{'静音' if action == 'mute' else '取消静音'}"))
            emit(v, args)
            return 0 if v else 1
        if action:
            if not action.isdigit() or not 0 <= int(action) <= 100:
                print(bad("音量必须是 0-100 的整数，或 mute / unmute"))
                return 1
            target = int(action)
            cur = mac_get_volume()
            if (cur and cur["percent"] == target and not cur["muted"]
                    and not args.force):
                print(info(f"已经是 {target}%，无需修改（--force 可强制重设）"))
                emit(cur, args)
                return 0
            v = mac_set_volume(target)
            if args.store:
                print(warn("macOS 不需要 alsactl store（系统自己会记住音量）"))
            print(ok(f"已设为 {target}%"
                     + ("，并取消静音" if args.unmute and v and not v["muted"] else "")))
            emit(v, args)
            return 0 if v else 1
        v = mac_get_volume()
        emit(v, args)
        return 0 if v else 1

    # ---------- Linux 分支：定位声卡 ----------
    card_info = None
    if args.card is not None:
        card = args.card
        card_info = next((d for d in list_playback_devices()
                          if d["card"] == card), None)
    elif args.device:
        card_info = match_device(args.device)
        if not card_info:
            print(bad(f"没有匹配到设备：{args.device}"))
            print(info("用 --list 看候选"))
            return 1
        card = card_info["card"]
    else:
        card = find_ugreen_card()
        if card is None:
            print(bad("未自动找到绿联音频设备"))
            print(info("可能是没插好，或用 --list 看候选后加 --card N 指定"))
            return 1
        card_info = next((d for d in list_playback_devices()
                          if d["card"] == card), None)

    action = (args.volume or "").strip().lower()

    # ---------- 静音 / 取消静音 ----------
    if action in ("mute", "unmute"):
        good, msg, after = set_mute(card, action == "mute", args.control)
        if not good:
            print(bad(f"操作失败：{msg}"))
            return 1
        print(ok(f"已{'静音' if action == 'mute' else '取消静音'}"))
        emit(after, args, card_info)
        return 0

    # ---------- 设置音量 ----------
    if action:
        if not action.isdigit() or not 0 <= int(action) <= 100:
            print(bad("音量必须是 0-100 的整数，或 mute / unmute"))
            return 1
        target = int(action)
        cur = get_volume(card, args.control)
        if (cur and cur["percent"] == target and not cur["muted"]
                and not args.force):
            print(info(f"已经是 {target}%，无需修改（--force 可强制重设）"))
            emit(cur, args, card_info)
            return 0
        good, msg, after = set_volume(card, target, args.control, args.unmute)
        if not good:
            print(bad(f"设置失败：{msg}"))
            return 1
        note = f"（{msg}）" if msg else ""
        print(ok(f"已设为 {target}%{note}"
                 + ("，并取消静音" if args.unmute else "")))
        emit(after, args, card_info)
        if args.store:
            stored, smsg = do_store()
            print((ok if stored else bad)(f"  {'✓' if stored else '✗'} {smsg}"))
            if stored:
                print(info("  想让 USB 热插拔时也自动生效："
                           "bash ~/fix_audio_volume.sh --udev"))
            return 0 if stored else 1
        if after and after["percent"] == 0:
            print(warn("  提示：重启后可能被 alsa-restore 恢复成 0%，"
                       "建议加 --store 持久化"))
        return 0

    # ---------- 只查询 ----------
    v = get_volume(card, args.control)
    emit(v, args, card_info)
    if v is None:
        print(bad("  读取失败：该声卡可能没有 PCM/Master 类播放控件"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
