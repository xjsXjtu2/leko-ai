"""ctrl：安全红线（07 §3.3）—— 高于一切任务/agent 请求。

已实装（在 Pilot 50Hz 闭环里）：
  ① 语音"停"无条件打断（"停"在 intent.parse 里最高优先，不经句子检查）
  ② 堵转：PWM 有输出且编码器 stall_ms 无脉冲 → 断电（520 堵转 3.5A > TB6612 峰值 3.2A）
  ③ 限幅：单条指令 ≤ max_dist_m；倒车 ≤ max_reverse_m（前驱禁长倒）
  ④ 硬件兜底：3A 保险丝（已装）+ 急停按钮串电池正极（待购，TB1 无 STBY 脚）

待实装（卡 LD06 / VL53L0X 采购，到货后补 50Hz 分级任务）：
  ⑤ LiDAR 分级：>50cm 正常 / 30~50 减速 / 10~30 强停（需求红线）/ <10 紧急+语音报警
  ⑥ 悬崖：VL53L0X 悬空 → 禁前进；压边 → 停 + 短倒 ≤20cm（倒车禁令唯一例外）
"""
from __future__ import annotations

from leko.core.config import CONFIG

STALL_MS = CONFIG["safety"]["stall_ms"]
JUNK_DBFS = CONFIG["safety"]["junk_dbfs"]
