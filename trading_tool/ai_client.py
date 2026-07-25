"""
AI 客户端（OpenAI 兼容 Chat Completions）
========================================
用于「周报 / 月报」等文本总结。采用 OpenAI 兼容接口，因此可对接：
  - OpenAI（默认）
  - DeepSeek / 通义千问 / 智谱 GLM / Groq / 本地 vLLM / Ollama 等（设 AI_BASE_URL 即可）

环境变量（均在 Render 配置，sync:false）：
  AI_API_KEY   必填，未配则 ai_client.available() 为 False（报告流水线自动跳过 AI 段落）
  AI_BASE_URL  默认 https://api.openai.com/v1
  AI_MODEL     默认 gpt-4o-mini

设计原则：缺密钥或调用失败都不阻断主流程（报告降级为结构化纯文本）。
"""

import os
import logging

import requests

logger = logging.getLogger("ai_client")

AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini").strip()


def available() -> bool:
    """是否已配置可用的 AI（有 API Key）。"""
    return bool(AI_API_KEY)


def chat(system_prompt: str, user_prompt: str, max_tokens: int = 2000,
         temperature: float = 0.3) -> str:
    """调用 Chat Completions，返回模型文本。未配置或失败抛异常（由调用方决定降级）。"""
    if not AI_API_KEY:
        raise RuntimeError("未配置 AI_API_KEY")
    try:
        resp = requests.post(
            f"{AI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {AI_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("AI 调用失败: %s", e)
        raise
