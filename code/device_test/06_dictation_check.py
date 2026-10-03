#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""06 听写链路验收（08-英语听写技术方案 §十 测试计划 1/2 步）。

不依赖麦克风/电机，用真实照片直接打 ECS 视觉服务，验证：
  词表提取准度 / 手写判分准度 / 拍照清晰度自检。

用法（在树莓派上，venv 会自动切换）：
  python3 device_test/06_dictation_check.py health              # 云端状态（key 配没配一看便知）
  python3 device_test/06_dictation_check.py photo               # 拍一张存 av_out/，看清晰度自检
  python3 device_test/06_dictation_check.py wordlist <photo.jpg>          # 词表提取准度
  python3 device_test/06_dictation_check.py handwriting <photo.jpg> apple dog cat   # 手写判分准度
  python3 device_test/06_dictation_check.py check <photo.jpg>   # = photo + wordlist 一条龙

准度过关标准（08 §十）：≥5 张不同版式词表照片全对；手写样本
错拼能抓出、歪斜不冤枉、看不清走 unsure。
"""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # code/ → import leko

from leko.core.config import CONFIG, path                     # noqa: E402
from leko.hal.camera import Camera                            # noqa: E402
from leko.net import http_client                              # noqa: E402


def b64_of(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit("用法：06_dictation_check.py <health|photo|wordlist|handwriting|check> [参数]")

    if args[0] == "health":
        print(http_client.get_health())
        return

    if args[0] == "photo":
        cam = Camera(blur_min=float(CONFIG["dictation"].get("photo_blur_min", 60.0)))
        out = path("tts_cache").parent / f"dictation_check_{__import__('time').strftime('%H%M%S')}.jpg"
        ok, reason = cam.capture(out)
        cam.close()
        print(("✓ " if ok else "✗ ") + reason, "→", out)
        sys.exit(0 if ok else 1)

    if args[0] == "wordlist":
        photo = Path(args[1]).expanduser()
        if not photo.exists():
            sys.exit(f"✗ 文件不存在：{photo}")
        data = http_client.post_json("/vision/wordlist", {"image_b64": b64_of(photo)})
        words = data["words"]
        print(f"✓ 提取到 {len(words)} 个词：{' '.join(words)}")

    elif args[0] == "handwriting":
        photo = Path(args[1]).expanduser()
        if not photo.exists():
            sys.exit(f"✗ 文件不存在：{photo}")
        words = args[2:]
        if not words:
            sys.exit("✗ 用法：handwriting <photo.jpg> 单词1 单词2 …（期望词表）")
        data = http_client.post_json("/vision/handwriting",
                                      {"image_b64": b64_of(photo), "words": words})
        print(f"{'expect':<14}{'verdict':<10}wrote")
        for w in data["words"]:
            mark = {"correct": "✓", "wrong": "✗", "unsure": "?"}[w["verdict"]]
            print(f"{w['expect']:<14}{mark} {w['verdict']:<8}{w.get('wrote', '')}")
        print(f"unsure: {data.get('unsure', 0)}（准度验收：错拼要抓出、歪斜不冤枉、看不清走 unsure）")

    elif args[0] == "check":
        # 一条龙：拍照 → 词表提取（不指定照片就现场拍）
        cam = Camera(blur_min=float(CONFIG["dictation"].get("photo_blur_min", 60.0)))
        out = path("tts_cache").parent / f"dictation_check_{__import__('time').strftime('%H%M%S')}.jpg"
        ok, reason = cam.capture(out)
        cam.close()
        if not ok:
            sys.exit(f"✗ {reason}（重拍或挪灯）")
        print("✓", reason)
        data = http_client.post_json("/vision/wordlist", {"image_b64": b64_of(out)})
        words = data["words"]
        print(f"✓ 提取到 {len(words)} 个词：{' '.join(words)}")
    else:
        sys.exit(f"✗ 未知子命令：{args[0]}")


if __name__ == "__main__":
    main()
