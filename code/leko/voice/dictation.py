"""voice：英语听写 F2.0 状态机（08-英语听写技术方案）。

流程全在本地（念词/等待/播报），只有两步上云：拍词表提取、拍手写判分
（ECS 纯函数视觉服务，key 只在 ECS）。

已拍板规则（2026-10-03）：
- 每词念 3 遍，每遍间隔 = 字母数 ÷ 2 秒
- unsure 算对 + 播报点名请家长复查（绝不冤枉孩子）
- 订正循环只重听错词，最多 3 轮

会话内短指令（"好了/写完了/再来一遍/下一个/结束听写"）由 Listener.text_hook
交给 session_parse，优先于全局解析——离开会话自动恢复全局解析。
"""
from __future__ import annotations

import base64
import queue
import re
import time
from pathlib import Path

from leko.core.config import CONFIG, path
from leko.ctrl.intent import num2cn
from leko.hal.camera import Camera
from leko.net import http_client
from leko.voice.tts import prewarm_words, speak, speak_en, speak_wait

REPEATS = int(CONFIG["dictation"].get("repeats", 3))
GAP_PER_LETTER = float(CONFIG["dictation"].get("gap_per_letter", 0.5))
WORD_GAP_S = float(CONFIG["dictation"].get("word_gap_s", 10.0))
ROUNDS_MAX = int(CONFIG["dictation"].get("rounds_max", 3))
PHOTO_RETRY = 3
NUM_CN = "零一二三四五六七八九十"


def _cn(n: int) -> str:
    return "".join(num2cn(n)) if n <= 10 else str(n)


def session_parse(text: str):
    """听写会话内的短指令（Listener.text_hook 调用，优先于全局解析）。"""
    if not text:
        return None
    t = re.sub(r"[\s，。,.!?！？、'\"']", "", text.lower())
    if re.search(r"停|结束|退出|不听了|不写了", t):
        return {"op": "quit"}
    if re.search(r"写完了|写好了|拍好了|念完了", t):
        return {"op": "written"}
    if re.search(r"再来一遍|再念一遍|再读一遍|重复|再来一次", t):
        return {"op": "again"}
    if re.search(r"下一个|跳过", t):
        return {"op": "next"}
    if re.search(r"好了|放好了|可以了|行了", t):
        return {"op": "ok"}
    if re.search(r"拍一张|重新拍|拍照", t):
        return {"op": "retake"}
    return None


class DictationSession:
    """一次完整听写：拍词表 → 念词 → 拍手写 → 判分播报 → 订正循环。"""

    def __init__(self, q: queue.Queue, listener):
        self.q = q
        self.listener = listener
        self.camera = Camera(blur_min=float(CONFIG["dictation"].get("photo_blur_min", 60.0)))
        self.words: list[str] = []
        self.all_results: list[dict] = []

    # ---------------- 基础：队列 / 等待 ----------------

    def _drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    def _wait_cmd(self, seconds: float):
        """等一条会话指令，最多 seconds 秒。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                return self.q.get(timeout=0.2)
            except queue.Empty:
                continue
        return None

    def _wait_confirm(self, kinds, timeout: float):
        """等确认类指令（ok/written），quit/stop 也接受（退出）。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                c = self.q.get(timeout=0.3)
                op = c.get("op")
                if op in kinds or op in ("quit", "stop"):
                    return c
            except queue.Empty:
                continue
        return None

    def _sleep_watch(self, seconds: float):
        """可打断的等待：again/next/quit 立刻返回，其余忽略。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                c = self.q.get(timeout=0.2)
                if c.get("op") in ("again", "next", "quit"):
                    return c
            except queue.Empty:
                pass
        return None

    # ---------------- 拍照 ----------------

    def _photo(self, retry: int = PHOTO_RETRY) -> Path | None:
        """拍一张（清晰度自检 + 重试）。None = 放弃。"""
        out_dir = path("tts_cache").parent            # av_out/
        out_dir.mkdir(parents=True, exist_ok=True)
        for i in range(retry):
            out = out_dir / f"dictation_{int(time.time() * 1000)}.jpg"
            ok, reason = self.camera.capture(out)
            if ok:
                print(f"   📸 {out.name}：{reason}")
                return out
            print(f"   📸 拍照不合格：{reason}")
            speak_wait(f"没拍清楚，挪近一点或者开灯，放好了说好了")
            c = self._wait_confirm(("ok",), timeout=45)
            if c is None or c.get("op") in ("quit", "stop"):
                return None
        speak_wait("拍了三次都不行，下次再试吧")
        return None

    def _post_photo(self, endpoint: str, payload: dict) -> dict:
        """照片上云（判分/提词），网络失败给一次'等网恢复重试'机会。"""
        payload["image_b64"] = base64.b64encode(payload["image_b64"].read_bytes()).decode()
        for attempt in (1, 2):
            try:
                return http_client.post_json(endpoint, payload)
            except RuntimeError as e:
                if attempt == 2:
                    raise
                speak_wait(f"{e}。网络好了说拍照，我再试一次")
                c = self._wait_confirm(("retake", "ok"), timeout=600)
                if c is None or c.get("op") in ("quit", "stop"):
                    raise

    # ---------------- 念词 ----------------

    def _speak_word(self, word: str) -> str | None:
        """念词 3 遍，每遍间隔 = 字母数÷2 秒。返回 'next'/'quit'（打断）或 None。"""
        gap = len(word) * GAP_PER_LETTER
        said = 0
        while said < REPEATS:
            speak_en(word, wait=True)                 # 英文儿童声
            said += 1
            if said >= REPEATS:
                return None
            c = self._sleep_watch(gap)
            if c:
                if c["op"] == "again":
                    said = max(0, said - 1)           # 再来一遍 = 这一遍重念
                    continue
                return c["op"]
        return None

    def _dictate_words(self, words: list[str]) -> str:
        """逐词念 + 词间等待。返回 'written'（写完了）/'quit'。"""
        for idx, w in enumerate(words):
            speak_wait(f"第{_cn(idx + 1)}个词")
            r = self._speak_word(w)
            if r == "quit":
                return "quit"
            if r == "next":
                continue
            # 词间等待（写字时间）：写完了/下一个/再来一遍 均可打断
            while True:
                c = self._wait_cmd(WORD_GAP_S)
                if c is None:
                    break                             # 超时 → 下一词
                op = c.get("op")
                if op in ("written", "ok"):           # "好了/写完了" → 直接进判分
                    return "written"
                if op == "next":
                    break
                if op == "quit":
                    return "quit"
                if op == "again":                     # 整词重念
                    if self._speak_word(w) == "quit":
                        return "quit"
        speak_wait("都念完了，写完了就喊我拍照")
        c = self._wait_confirm(("written", "ok"), timeout=300)
        if c is None or c.get("op") in ("quit", "stop"):
            return "quit"
        return "written"

    # ---------------- 判分与播报 ----------------

    def _judge(self, words: list[str]) -> list[dict] | None:
        """拍手写 → ECS 判分。None = 放弃。"""
        speak_wait("把写的放到我面前，放好了说好了")
        c = self._wait_confirm(("ok",), timeout=180)
        if c is None or c.get("op") in ("quit", "stop"):
            return None
        photo = self._photo()
        if photo is None:
            return None
        try:
            data = self._post_photo("/vision/handwriting", {"image_b64": photo, "words": words})
        except RuntimeError as e:
            speak_wait(f"{e}。这次先不算了，下次再来")
            return None
        return data.get("words", [])

    def _announce(self, results: list[dict], rnd: int) -> list[str]:
        """播报判分（第 rnd+1 轮）。返回错词列表（空 = 结束循环）。"""
        wrongs = [r for r in results if r.get("verdict") == "wrong"]
        unsures = [r for r in results if r.get("verdict") == "unsure"]
        if not wrongs and not unsures:
            speak_wait("太棒了，全对！")
            return []
        if unsures:
            speak_wait(f"有{_cn(len(unsures))}个词拿不准，先算你对，等爸爸妈妈看一眼")
            for r in unsures:
                speak_en(r["expect"], wait=True)
        if not wrongs:
            speak_wait("没有拼错的，听写结束")
            return []
        speak_wait(f"拼错了{_cn(len(wrongs))}个词，听好正确写法")
        for r in wrongs:
            w = r["expect"]
            speak_en(w, wait=True)                          # 正确读音
            speak_en(" ".join(w), wait=True)                # 字母拼读 a p p l e
            wrote = (r.get("wrote") or "").strip()
            if wrote:
                speak_wait(f"你写成了 {wrote}，跟我读")
                speak_en(w, wait=True)
                time.sleep(1.0)
        return [r["expect"] for r in wrongs]

    # ---------------- 主流程 ----------------

    def run(self) -> dict | None:
        """完整听写会话。返回摘要 dict（None = 未完成/放弃）。"""
        self.listener.text_hook = session_parse           # 会话短指令优先
        self._drain()
        try:
            speak_wait("听写开始。把单词表放到我面前，放好了说好了")
            c = self._wait_confirm(("ok",), timeout=120)
            if c is None or c.get("op") in ("quit", "stop"):
                return None
            photo = self._photo()
            if photo is None:
                return None
            try:
                data = self._post_photo("/vision/wordlist", {"image_b64": photo})
            except RuntimeError as e:
                speak_wait(f"{e}。这次先不算了，下次再来")
                return None
            words = [str(w) for w in data.get("words", [])]
            if not words:
                speak_wait("没认出单词，检查一下照片，下次再来")
                return None
            self.words = words
            prewarm_words(words)                           # 全部单词音频预缓存（断网照念）
            print(f"   📝 词表（{len(words)}）：{' '.join(words)}")
            speak_wait(f"一共{_cn(len(words))}个单词，听写开始，写完喊我拍照")

            wrongs = list(words)                           # 第 0 轮念全部
            for rnd in range(ROUNDS_MAX):
                if self._dictate_words(wrongs) == "quit":
                    return None
                results = self._judge(wrongs)
                if results is None:
                    return None
                self.all_results.extend(results)
                wrongs = self._announce(results, rnd)
                if not wrongs:
                    break                                  # 全对 → 结束
                if rnd < ROUNDS_MAX - 1:
                    speak_wait(f"现在重听写错的{_cn(len(wrongs))}个词，写完喊我")
            speak_wait("听写结束")
            return {"words": self.words, "rounds": rnd + 1}
        finally:
            self.listener.text_hook = None                 # 恢复全局解析
            self._drain()
            self.camera.close()
