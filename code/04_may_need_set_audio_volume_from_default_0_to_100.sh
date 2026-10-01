#!/bin/bash
# 把 UGREEN 一体机（U2K / UGREEN Camera 2K，UAC 音频）的 ALSA 播放音量设为 100%。
#
# 为什么需要它：
#   这块 USB 音频设备的 PCM 播放音量上电默认是 0%，而 /var/lib/alsa/asound.state
#   里存的恰好也是 0%，于是每次开机 alsa-restore 都会"忠实地"把 0% 恢复回来。
#   表现极具误导性：aplay 返回 0、耗时也和音频时长一致，但喇叭一声不响。
#
# 用法：
#   bash ~/fix_audio_volume.sh           # 立即生效 + 写入存档（推荐先跑这个）
#   bash ~/fix_audio_volume.sh --udev    # 额外装 udev 规则，热插拔也自动生效
set -u

CARD=$(awk '/UGREEN/{print $1}' /proc/asound/cards | head -1)
if [ -z "$CARD" ]; then
    echo "✗ /proc/asound/cards 里没有 UGREEN 设备 —— 摄像头插好了吗？"
    exit 1
fi

BEFORE=$(amixer -c "$CARD" sget PCM 2>/dev/null | grep -o '\[[0-9]*%\]' | head -1)
amixer -c "$CARD" sset PCM 100% unmute >/dev/null 2>&1
AFTER=$(amixer -c "$CARD" sget PCM 2>/dev/null | grep -o '\[[0-9]*%\]' | head -1)
echo "· card $CARD : PCM 音量 ${BEFORE:-?} → ${AFTER:-?}"

echo "· 写入 ALSA 存档（开机由 alsa-restore 自动恢复）…"
if sudo alsactl store; then
    echo "  ✓ 已存入 /var/lib/alsa/asound.state"
else
    echo "  ✗ 写入失败（需要 sudo 密码）"
fi

if [ "${1:-}" = "--udev" ]; then
    echo "· 安装 udev 规则（USB 热插拔时自动设音量）…"
    sudo tee /usr/local/bin/ugreen-volume.sh >/dev/null <<'EOS'
#!/bin/bash
# 由 udev 触发：UGREEN 一体机的 USB 声卡枚举后，把播放音量顶到 100%
sleep 1
CARD=$(awk '/UGREEN/{print $1}' /proc/asound/cards | head -1)
[ -n "$CARD" ] && amixer -c "$CARD" sset PCM 100% unmute >/dev/null 2>&1
EOS
    sudo chmod +x /usr/local/bin/ugreen-volume.sh
    sudo tee /etc/udev/rules.d/90-ugreen-audio.rules >/dev/null <<'EOS'
ACTION=="add", SUBSYSTEM=="sound", KERNEL=="card*", RUN+="/usr/local/bin/ugreen-volume.sh"
EOS
    sudo udevadm control --reload
    echo "  ✓ 已安装 /etc/udev/rules.d/90-ugreen-audio.rules"
fi

echo "✓ 完成。现在可以重跑：python3 ~/04_av_test.py speaker"
