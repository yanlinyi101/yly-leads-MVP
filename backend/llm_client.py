"""
LLM Client：DeepSeek（OpenAI 兼容）

设计要点：
  - 使用 openai 官方 SDK，只换 base_url 即可调用 DeepSeek，便于以后无痛切换到 OpenAI/通义/Kimi
  - retry / timeout 由 SDK 内建机制处理（max_retries / timeout）
  - 提供 JSON 模式：response_format={"type": "json_object"}，给 ReAct Runner 的 route_decision 用
  - 返回结构化的 ChatResult，把 content + latency_ms + finish_reason 一并带出，便于 Trace 收集

不做的事（候选人有意识地保留为后续迭代）：
  - 不在 Client 层做语义层面的校验（例如检查 JSON schema），那是 Runner 的职责
  - 不做缓存（Demo 阶段每次都重新调用，避免掩盖真实延时）
  - 不做计费统计（生产环境再做）
"""
from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI, OpenAIError, APITimeoutError

from .config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    content: str
    latency_ms: int
    finish_reason: Optional[str]
    model: str
    json_mode: bool

    def as_json(self) -> dict:
        """解析 content 为 JSON。仅在 json_mode=True 时使用。

        DeepSeek 在 json_object 模式下保证返回合法 JSON。
        即便如此，调用方仍需要捕获 ValueError，因为：
          - 模型可能因 max_tokens 截断返回不完整 JSON
          - finish_reason 为 'length' 时尤其要小心
        """
        return json.loads(self.content)


class LLMClient:
    """统一的 LLM 调用入口。线程安全：每次调用都用同一个 OpenAI client（SDK 内部自带 httpx 连接池）。"""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.deepseek_api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY 未配置。请复制 .env.example 为 .env 并填入 API Key。"
            )
        self._client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._model = settings.deepseek_model

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        messages: list[dict],
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> ChatResult:
        """调用 DeepSeek Chat Completions。

        Args:
            messages: OpenAI 风格 messages 列表，[{"role": "system"|"user"|"assistant", "content": "..."}]
            json_mode: 若为 True，要求模型返回 JSON 对象（response_format=json_object）。
                       注意：DeepSeek 要求 messages 中必须出现 "JSON" 字样（已在 Runner 的 prompt 里满足）。
            temperature: 默认 0 以保证可复现（评审重点是 Trace 可审计）
            max_tokens: 可选输出长度上限

        Returns:
            ChatResult，包含 content / latency_ms / finish_reason
        Raises:
            RuntimeError: 当 API 调用失败、超时或被截断时（Runner 应捕获并写入 Trace 的 error 字段）
        """
        kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except APITimeoutError as e:
            elapsed = int((time.perf_counter() - t0) * 1000)
            logger.warning("LLM 调用超时 (%dms): %s", elapsed, e)
            raise RuntimeError(f"LLM 调用超时（{elapsed}ms）：{e}") from e
        except OpenAIError as e:
            elapsed = int((time.perf_counter() - t0) * 1000)
            logger.warning("LLM 调用失败 (%dms): %s", elapsed, e)
            raise RuntimeError(f"LLM 调用失败：{e}") from e

        latency_ms = int((time.perf_counter() - t0) * 1000)

        if not resp.choices:
            raise RuntimeError("LLM 返回空 choices 列表")

        choice = resp.choices[0]
        content = (choice.message.content or "").strip()
        finish_reason = choice.finish_reason

        if json_mode and finish_reason == "length":
            # 截断了，JSON 大概率不完整；Runner 应该捕获 ValueError 并降级
            logger.warning("LLM 在 json_mode 下被 max_tokens 截断，可能返回不完整 JSON")

        return ChatResult(
            content=content,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            model=self._model,
            json_mode=json_mode,
        )
