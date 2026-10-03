"""视觉纯函数：词表提取 / 手写判分（阿里云百炼 qwen-vl，OpenAI 兼容接口）。

换厂商只改 base_url + model（.env），豆包/GLM-4V 同为 OpenAI 兼容，零代码改动。"""
from __future__ import annotations

import json
import os
import re


class UpstreamError(RuntimeError):
    """多模态 API 调用失败（网络/鉴权/限流）。"""


class BadOutput(RuntimeError):
    """模型返回了不合规的 JSON。"""


def _call(prompt: str, image_b64: str) -> str:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["DASHSCOPE_API_KEY"],
                        base_url=os.environ.get(
                            "VISION_BASE_URL",
                            "https://dashscope.aliyuncs.com/compatible-mode/v1"))
        resp = client.chat.completions.create(
            model=os.environ.get("VISION_MODEL", "qwen-vl-plus"),
            temperature=0,
            messages=[{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": prompt},
            ]}])
    except Exception as e:                                  # noqa: BLE001
        raise UpstreamError(f"API 调用失败：{e}") from e
    if not resp.choices:
        raise UpstreamError("API 返回空 choices")
    return resp.choices[0].message.content or ""


def _json_of(text: str) -> dict:
    """从模型输出抠 JSON：容忍 ```json 围栏、前后废话。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise BadOutput(f"没找到 JSON：{text[:200]}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise BadOutput(f"JSON 解析失败：{e}；原文：{text[:200]}") from e


def extract_wordlist(image_b64: str) -> list[str]:
    """照片 → 英文单词表（保序去重）。"""
    data = _json_of(_call(prompts.WORDLIST, image_b64))
    words = data.get("words")
    if not isinstance(words, list) or not words:
        raise BadOutput("words 为空或类型不对")
    seen, out = set(), []
    for w in words:
        w = str(w).strip().lower()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def judge_handwriting(image_b64: str, expect: list[str]) -> list[dict]:
    """孩子手写照片 + 词表 → 逐词判定 [{expect, verdict, wrote}]。

    verdict: correct / wrong / unsure（拿不准绝不冤枉——Pi 端 unsure 算对）。"""
    prompt = prompts.handwriting(expect)
    data = _json_of(_call(prompt, image_b64))
    words = data.get("words")
    if not isinstance(words, list) or not words:
        raise BadOutput("words 为空或类型不对")
    out = []
    for item in words:
        if not isinstance(item, dict):
            raise BadOutput(f"判分项不是对象：{item}")
        out.append({"expect": str(item.get("expect", "")),
                    "verdict": str(item.get("verdict", "unsure")),
                    "wrote": str(item.get("wrote", ""))})
    return out
