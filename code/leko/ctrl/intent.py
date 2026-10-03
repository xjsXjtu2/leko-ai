"""ctrl：意图解析（本地文法，F1.1 精确指令）—— 快慢通道里的"本地快通道"。

"停"无条件最高优先（安全红线）；其余指令必须"有距离/角度 + 剩字≤2"双保险，
防闲聊误触发。抽象目标（"找手表"）不在这里——那是慢通道（ECS dsh agent loop）。"""
from __future__ import annotations

import re

from leko.core.config import CONFIG
from leko.voice.wake import WAKE_WORDS

MAX_DIST = CONFIG["motion"]["max_dist_m"]
MAX_REVERSE = CONFIG["motion"]["max_reverse_m"]

CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
      "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 前进动词：先列 ASR 常见两字谐音（前近/全近/平进…），再兜底单字。
# 单字（进/近/走/前/径）很宽，安全性靠两条：无距离不动 + 剩字≤2（_leftover_ok）。
VERB_FWD = "前进|往前|向前|直行|直走|前近|全近|全进|平进|平近|走|前|进|近|径"
VERB_BACK = "后退|后腿|倒退|倒车|往后|向后|退|倒"

FILLER = "啊呃嗯哦呀哎的地得吧了呐哈那这呢个呗"


def cn2num(s: str):
    """'十五'→15；'一点五'/'1点5'→1.5；'半'→0.5；阿拉伯数字直通。"""
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    if s == "半":
        return 0.5
    if "点" in s:
        a, _, b = s.partition("点")
        ip = cn2num(a) if a else 0
        if ip is None or not b:
            return None
        bd = b if b.isdigit() else str(cn2num(b) or "")
        try:
            return float(ip) + float("0." + bd) if bd else None
        except ValueError:
            return None
    total, num = 0, 0
    for ch in s:
        if ch in CN:
            num = CN[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
        else:
            return None
    return total + num


def num2cn(n: int):
    """0~999 → 中文数字单字列表（45 → ['四','十','五']）。"""
    d = "零一二三四五六七八九"
    if n == 0:
        return ["零"]
    out = []
    if n >= 100:
        out += [d[n // 100], "百"]
        n %= 100
    if n >= 20:
        out += [d[n // 10], "十"]
        n %= 10
    elif n >= 10:
        out += ["十"]
        n %= 10
    if n:
        out.append(d[n])
    return out


def _dist_match(text: str):
    """距离 → (米, 命中原文)；无距离 → (None, None)。"""
    m = re.search(r"([\d.]+|[零一二三四五六七八九十百两半]+(?:点[零一二三四五\d]+)?)"
                  r"(米|公尺|m|厘米|公分|cm|分米|dm)", text)
    if not m:
        return None, None
    v = cn2num(m.group(1))
    if v is None:
        return None, None
    unit = m.group(2)
    if unit in ("厘米", "公分", "cm"):
        v /= 100.0
    elif unit in ("分米", "dm"):
        v /= 10.0
    return round(min(v, MAX_DIST), 2), m.group(0)


def _leftover_ok(t: str, *parts) -> bool:
    """把指令成分从原句剥掉，剩字（豁免常见语气词）≤2 才算一条干净指令。

    防"今天走了五米路"这类闲聊误触发；"呃前进一米吧"照常通过。
    唤醒词前缀豁免："乐可前进一米"一句连说
    （或预缓冲漏进来 1-2 个字）必须能过；先剥句首唤醒词再数剩字。
    """
    rest = t
    for w, _ in WAKE_WORDS:
        if rest.startswith(w):
            rest = rest[len(w):]
    for p in parts:
        if p:
            rest = rest.replace(p, "", 1)
    return len(re.sub(f"[{FILLER}]", "", rest)) <= 2


def parse(text: str):
    """识别文本 → 指令 dict；None = 无关话/闲聊句。

    "停"无条件最高优先（安全红线），不受任何句子检查限制。
    其余指令必须"有距离/角度 + 剩字≤2"双保险，防误触发。
    """
    if not text:
        return None
    t = re.sub(r"[\s，。,!？！？、'\"']", "", text.lower()).replace("１", "1")
    # SenseVoice 常把「米」听成「名」：前进一名 / 后退一名 / 半名
    t = re.sub(r"([\d.]+|[零一二三四五六七八九十百两半]+(?:点[零一二三四五\d]+)?)名",
               r"\1米", t)
    if re.search(r"停|刹车|别动|stop", t):
        return {"op": "stop"}
    md = re.search(r"听写|默写", t)             # F2.0 英语听写（08 方案；进入会话态）
    if md:
        return {"op": "dictation"} if _leftover_ok(t, md.group(0)) else None
    dist, dtxt = _dist_match(t)
    ang, atxt = None, None
    a = re.search(r"([零一二三四五六七八九十百两\d]+)度", t)
    if a:
        ang = cn2num(a.group(1))
        atxt = a.group(0)
    # 斜向（左前方等）→ 定曲率弧线
    for w, deg in (("左后方", -135), ("右后方", 135), ("左前方", -45), ("右前方", 45)):
        if w in t:
            vm = re.search(VERB_FWD, t)
            if not dist:
                return {"op": "miss"} if _leftover_ok(t, w, vm.group(0) if vm else "") else None
            return {"op": "arc", "deg": deg, "dist": dist, "dtxt": dtxt} \
                if _leftover_ok(t, w, dtxt, vm.group(0) if vm else "") else None
    m2 = re.search(r"掉头|转身|转个圈", t)
    if m2:
        deg = 360 if "圈" in m2.group(0) else 180
        return {"op": "turn", "deg": deg} if _leftover_ok(t, m2.group(0)) else None
    if "左转" in t or "右转" in t:
        deg = abs(ang) if ang else 90
        w = "右转" if "右转" in t else "左转"
        return {"op": "turn", "deg": deg if w == "右转" else -deg, "atxt": atxt} \
            if _leftover_ok(t, w, atxt) else None
    vb = re.search(VERB_BACK, t)                  # 倒车先判（前驱：限 1 米）
    if vb:
        if dist:
            return {"op": "dist", "dist": min(dist, MAX_REVERSE), "reverse": True,
                    "capped": dist > MAX_REVERSE, "dtxt": dtxt} \
                if _leftover_ok(t, vb.group(0), dtxt) else None
        return {"op": "miss"} if _leftover_ok(t, vb.group(0)) else None
    vf = re.search(VERB_FWD, t)                   # 前进
    if vf:
        if not dist:
            return {"op": "miss"} if _leftover_ok(t, vf.group(0)) else None
        return {"op": "dist", "dist": dist, "reverse": False, "dtxt": dtxt} \
            if _leftover_ok(t, vf.group(0), dtxt) else None
    return None


def phrase_words(s_: str):
    """'一米'→[一,米]；'2.5米'→[二,点,五,米]；'1m'→[一,米]；'3cm'→[三,厘米]。"""
    s_ = (s_.replace("cm", "厘米").replace("CM", "厘米")
             .replace("dm", "分米").replace("DM", "分米"))
    s_ = re.sub(r"[mM]", "米", s_)
    words, i = [], 0
    while i < len(s_):
        for u in ("厘米", "公尺", "分米"):
            if s_.startswith(u, i):
                words.append(u)
                i += len(u)
                break
        else:
            ch = s_[i]
            if ch.isdigit() or ch == ".":
                j = i
                while j < len(s_) and (s_[j].isdigit() or s_[j] == "."):
                    j += 1
                for piece in re.findall(r"\d+|\.", s_[i:j]):
                    words.append("点") if piece == "." else words.extend(num2cn(int(piece)))
                i = j
            else:
                words.append(ch)
                i += 1
    return words
