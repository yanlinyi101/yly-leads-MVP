"""
单元测试：LLM Client

策略：mock 掉 openai SDK 的 chat.completions.create，
      验证 LLMClient 正确地传参、返回 ChatResult、处理 JSON 模式与错误。
不依赖真实 DEEPSEEK_API_KEY。
"""
import os
import json
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch):
    """每个测试都注入假 API key，避免 LLMClient 初始化失败"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    # config 用了 lru_cache 风格的全局读取？我们这里直接重置 dotenv 缓存即可
    yield


def _make_fake_resp(content: str, finish_reason: str = "stop"):
    """构造一个跟 openai SDK 返回值结构一致的 mock"""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_chat_plain_text():
    """普通 chat：返回纯文本"""
    from backend.llm_client import LLMClient

    with patch("backend.llm_client.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_fake_resp("hello world")

        llm = LLMClient()
        result = llm.chat([{"role": "user", "content": "hi"}])

        assert result.content == "hello world"
        assert result.finish_reason == "stop"
        assert result.json_mode is False
        assert result.latency_ms >= 0
        assert result.model == "deepseek-chat"

        # 验证关键参数透传
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "deepseek-chat"
        assert call_kwargs["temperature"] == 0.0
        assert "response_format" not in call_kwargs


def test_chat_json_mode():
    """JSON 模式：传 response_format，content 可解析为 dict"""
    from backend.llm_client import LLMClient

    payload = {"need_tool": True, "tool_name": "query_product"}
    with patch("backend.llm_client.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_fake_resp(json.dumps(payload))

        llm = LLMClient()
        result = llm.chat(
            [{"role": "user", "content": "return JSON"}],
            json_mode=True,
        )

        assert result.json_mode is True
        assert result.as_json() == payload

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["response_format"] == {"type": "json_object"}


def test_chat_api_error_wrapped():
    """SDK 抛 OpenAIError 应被包成 RuntimeError，让 Runner 能统一捕获"""
    from openai import OpenAIError
    from backend.llm_client import LLMClient

    with patch("backend.llm_client.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.side_effect = OpenAIError("rate limited")

        llm = LLMClient()
        with pytest.raises(RuntimeError, match="LLM 调用失败"):
            llm.chat([{"role": "user", "content": "x"}])


def test_chat_empty_choices_raises():
    """API 返回空 choices 时应明确报错"""
    from backend.llm_client import LLMClient

    with patch("backend.llm_client.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        empty_resp = MagicMock()
        empty_resp.choices = []
        mock_client.chat.completions.create.return_value = empty_resp

        llm = LLMClient()
        with pytest.raises(RuntimeError, match="空 choices"):
            llm.chat([{"role": "user", "content": "x"}])


def test_missing_api_key_raises(monkeypatch):
    """未配置 API key 时初始化应失败"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    from backend.llm_client import LLMClient

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        LLMClient()
