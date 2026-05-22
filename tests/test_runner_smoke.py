"""
单元测试：ReAct Runner

策略：完全 mock LLMClient，按预设脚本逐轮返回 JSON，
      验证 Runner 的循环控制、Trace 完整性、错误恢复、超轮策略、Tool 权限。
"""
import json
import shutil
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from backend.skill_loader import load_skill
from backend.tools.registry import ToolRegistry, ToolSpec
from backend.tools.query_product import query_product, QUERY_PRODUCT_SCHEMA
from backend.trace import TraceCollector
from backend.runner import ReActRunner
from backend.llm_client import ChatResult
from backend import persistence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = PROJECT_ROOT / "skills" / "lead-scoring-followup"


# ---------- helpers ----------

def _chat_result(content_dict: dict) -> ChatResult:
    return ChatResult(
        content=json.dumps(content_dict, ensure_ascii=False),
        latency_ms=10,
        finish_reason="stop",
        model="deepseek-chat-mock",
        json_mode=True,
    )


def _mock_llm(scripted_responses: list):
    """返回一个 mock LLMClient，每次 chat() 按顺序吐出 scripted_responses 里的内容。

    元素可以是：dict（包装成 ChatResult） 或 Exception 实例（chat() 会 raise）。
    """
    llm = MagicMock()
    llm.model = "deepseek-chat-mock"

    iterator = iter(scripted_responses)

    def _fake_chat(messages, json_mode=False, temperature=0.0, max_tokens=None):
        try:
            nxt = next(iterator)
        except StopIteration:
            raise AssertionError("脚本耗尽：Runner 调 LLM 次数超过预期")
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, ChatResult):
            return nxt
        return _chat_result(nxt)

    llm.chat = MagicMock(side_effect=_fake_chat)
    return llm


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="query_product",
        description="检索本地产品/案例资料，每条结果带 source_id",
        parameters_schema=QUERY_PRODUCT_SCHEMA,
        fn=query_product,
    ))
    return reg


# ---------- 测试用例 ----------

def test_happy_path_act_then_answer():
    """理想路径：turn1 调 query_product → turn2 给 answer"""
    skill = load_skill(SKILL_DIR)
    tools = _make_registry()
    trace = TraceCollector()
    llm = _mock_llm([
        # turn 1: 路由到 query_product
        {
            "reasoning_summary": "线索提到客服 AI，需查目录确认产品",
            "route_decision": {
                "need_tool": True,
                "tool_name": "query_product",
                "tool_args": {"query": "客服智能体", "top_k": 3},
            },
        },
        # turn 2: 给 final answer
        {
            "reasoning_summary": "已获取产品资料，可以输出 answer",
            "route_decision": {"need_tool": False},
            "answer": {
                "lead_tier": "A",
                "intent_level": "高",
                "pain_points": ["客服响应慢"],
                "missing_info": [],
                "risks": [],
                "recommended_product": "PROD-CS-AGENT",
                "next_actions": ["1 工作日内安排 Demo"],
                "draft_reply": "您好，针对您的客服场景我们的客服智能体...",
                "needs_human_review": False,
                "triggered_rules": [],
                "evidence": [
                    {"claim": "客服智能体支持 7x24", "source_id": "PROD-CS-AGENT"},
                ],
            },
        },
    ])
    runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace, max_turns=3)

    result = runner.run("我们做电商，客服 24h 顶不住，能不能上 AI 客服？")
    assert result.finished is True
    assert result.answer["lead_tier"] == "A"
    assert result.answer["recommended_product"] == "PROD-CS-AGENT"

    types = [s.step_type for s in trace.steps()]
    # 期望顺序：init → reasoning → act → observe → reasoning → answer
    assert types == ["init", "reasoning", "act", "observe", "reasoning", "answer"]

    # Tool 调用次数 = 1（仅 query_product 一次）
    assert llm.chat.call_count == 2


def test_max_turns_forces_summary():
    """LLM 一直说 need_tool=true，Runner 在第 max_turns 轮后必须强制总结"""
    skill = load_skill(SKILL_DIR)
    tools = _make_registry()
    trace = TraceCollector()

    insistent_tool_call = {
        "reasoning_summary": "继续查",
        "route_decision": {
            "need_tool": True,
            "tool_name": "query_product",
            "tool_args": {"query": "AI"},
        },
    }
    final_answer_after_force = {
        "reasoning_summary": "强制总结",
        "route_decision": {"need_tool": False},
        "answer": {
            "lead_tier": "C",
            "intent_level": "中",
            "pain_points": [], "missing_info": [], "risks": [],
            "recommended_product": None,
            "next_actions": [], "draft_reply": "...",
            "needs_human_review": True,
            "triggered_rules": [],
            "evidence": [],
        },
    }
    # max_turns=2 → 第 1,2 轮 LLM 都坚持调 Tool，第 3 轮（强制总结轮）给 answer
    llm = _mock_llm([insistent_tool_call, insistent_tool_call, final_answer_after_force])
    runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace, max_turns=2)

    result = runner.run("随便问问")
    assert result.finished is True
    # Trace 里必须出现 max_turns_guard
    assert any(s.step_type == "max_turns_guard" for s in trace.steps())


def test_llm_failure_falls_back():
    """LLM 连续失败两次 → 兜底 answer + Trace 有 error"""
    skill = load_skill(SKILL_DIR)
    tools = _make_registry()
    trace = TraceCollector()
    llm = _mock_llm([
        RuntimeError("connection reset"),
        RuntimeError("connection reset"),
    ])
    runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace, max_turns=3)

    result = runner.run("test")
    assert result.finished is False  # 兜底
    assert result.answer["needs_human_review"] is True
    assert any(s.step_type == "error" for s in trace.steps())


def test_llm_retry_succeeds_on_second_try():
    """LLM 第一次失败、第二次成功 → 流程仍能继续"""
    skill = load_skill(SKILL_DIR)
    tools = _make_registry()
    trace = TraceCollector()

    answer_payload = {
        "reasoning_summary": "ok",
        "route_decision": {"need_tool": False},
        "answer": {
            "lead_tier": "B", "intent_level": "中",
            "pain_points": [], "missing_info": [], "risks": [],
            "recommended_product": None, "next_actions": [],
            "draft_reply": "...", "needs_human_review": False,
            "triggered_rules": [], "evidence": [],
        },
    }
    llm = _mock_llm([RuntimeError("timeout"), answer_payload])
    runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace, max_turns=3)

    result = runner.run("hi")
    assert result.finished is True
    # 应该有一条 warn trace 记录首次失败
    assert any(s.step_type == "warn" for s in trace.steps())


def test_unauthorized_tool_rejected():
    """Skill 没开放的 Tool 被请求时 → Trace 记录拒绝，循环继续

    新增（D7）验证：
      - answer.tool_iteration_requests 至少 1 条，reason='unauthorized'
      - answer.needs_human_review 被强制为 True（即便 LLM 自己写的是 False 也覆盖）
      - answer.evidence 多挂一条 RUNNER-TOOL-ITERATION
    """
    skill = load_skill(SKILL_DIR)
    tools = _make_registry()
    # 额外注册一个 Skill 未授权的 Tool
    tools.register(ToolSpec(
        name="send_email",
        description="发邮件",
        parameters_schema={"type": "object"},
        fn=lambda args: {"sent": True},
    ))
    trace = TraceCollector()
    llm = _mock_llm([
        # turn 1: 请求未授权 Tool
        {
            "reasoning_summary": "想发邮件",
            "route_decision": {
                "need_tool": True,
                "tool_name": "send_email",
                "tool_args": {"to": "x@y.com"},
            },
        },
        # turn 2: Runner 拒绝后让 LLM 终止
        # 故意把 LLM 写的 needs_human_review=False，验证 Runner 会强制覆盖为 True
        {
            "reasoning_summary": "改走直接 answer",
            "route_decision": {"need_tool": False},
            "answer": {
                "lead_tier": "D", "intent_level": "不相关",
                "pain_points": [], "missing_info": [], "risks": [],
                "recommended_product": None, "next_actions": [],
                "draft_reply": "...", "needs_human_review": False,
                "triggered_rules": [], "evidence": [],
            },
        },
    ])
    runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace, max_turns=3)

    result = runner.run("xx")
    assert result.finished is True

    # Trace 里应有 act 步骤但带 error
    act_steps = [s for s in trace.steps() if s.step_type == "act"]
    assert len(act_steps) == 1
    assert act_steps[0].error is not None
    assert "未授权" in act_steps[0].error

    # D7 验证：tool_iteration_requests 被注入到 answer
    requests = result.answer.get("tool_iteration_requests")
    assert isinstance(requests, list)
    assert len(requests) == 1
    assert requests[0]["tool_name"] == "send_email"
    assert requests[0]["tool_args"] == {"to": "x@y.com"}
    assert requests[0]["reason"] == "unauthorized"
    assert requests[0]["turn"] == 1
    assert "未授权" in requests[0]["detail"]

    # D7 验证：needs_human_review 被强制覆盖为 True
    assert result.answer["needs_human_review"] is True

    # D7 验证：evidence 多了一条 RUNNER-TOOL-ITERATION
    evidence_sources = [e.get("source_id") for e in result.answer.get("evidence", [])]
    assert "RUNNER-TOOL-ITERATION" in evidence_sources


def test_tool_execution_error_records_iteration_request():
    """已授权 Tool 调用时抛异常 → 同样记账为 tool_iteration_requests（reason=execution_error）"""
    skill = load_skill(SKILL_DIR)
    # 用一个会抛异常的"假 query_product"覆盖掉真实的
    tools = ToolRegistry()

    def _broken_tool(args: dict):
        raise ValueError("simulated DB outage")

    tools.register(ToolSpec(
        name="query_product",
        description="坏掉的 query_product",
        parameters_schema=QUERY_PRODUCT_SCHEMA,
        fn=_broken_tool,
    ))
    trace = TraceCollector()
    llm = _mock_llm([
        # turn 1: 让它调一次
        {
            "reasoning_summary": "查目录",
            "route_decision": {
                "need_tool": True,
                "tool_name": "query_product",
                "tool_args": {"query": "客服"},
            },
        },
        # turn 2: 看到 observe 是 error 后终止
        {
            "reasoning_summary": "工具坏了，先给保守 answer",
            "route_decision": {"need_tool": False},
            "answer": {
                "lead_tier": "C", "intent_level": "中",
                "pain_points": [], "missing_info": [], "risks": [],
                "recommended_product": None, "next_actions": [],
                "draft_reply": "...", "needs_human_review": False,
                "triggered_rules": [], "evidence": [],
            },
        },
    ])
    runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace, max_turns=3)
    result = runner.run("hi")
    assert result.finished is True

    requests = result.answer.get("tool_iteration_requests", [])
    assert len(requests) == 1
    assert requests[0]["reason"] == "execution_error"
    assert "DB outage" in requests[0]["detail"]
    # needs_human_review 被强制覆盖
    assert result.answer["needs_human_review"] is True


def test_success_path_has_empty_iteration_requests():
    """Happy path 应当注入空列表（不是缺字段），方便下游消费者无脑迭代"""
    skill = load_skill(SKILL_DIR)
    tools = _make_registry()
    trace = TraceCollector()
    llm = _mock_llm([
        {
            "reasoning_summary": "走 Tool",
            "route_decision": {
                "need_tool": True,
                "tool_name": "query_product",
                "tool_args": {"query": "客服"},
            },
        },
        {
            "reasoning_summary": "ok",
            "route_decision": {"need_tool": False},
            "answer": {
                "lead_tier": "A", "intent_level": "高",
                "pain_points": [], "missing_info": [], "risks": [],
                "recommended_product": "PROD-CS-AGENT", "next_actions": [],
                "draft_reply": "...", "needs_human_review": False,
                "triggered_rules": [], "evidence": [],
            },
        },
    ])
    runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace, max_turns=3)
    result = runner.run("hi")
    assert result.finished is True
    assert result.answer["tool_iteration_requests"] == []
    # 没有任何拒绝/异常 → 不强制覆盖，保持 LLM 输出
    assert result.answer["needs_human_review"] is False


def test_invalid_json_recovers_once():
    """LLM 第一次返回非法 JSON → Runner 让它修一次 → 修好后正常继续"""
    skill = load_skill(SKILL_DIR)
    tools = _make_registry()
    trace = TraceCollector()

    # 第一次返回的是非法 JSON（用 ChatResult 直接构造）
    bad_chat = ChatResult(
        content="not a json at all",
        latency_ms=5,
        finish_reason="stop",
        model="mock",
        json_mode=True,
    )
    fixed_answer = {
        "reasoning_summary": "修好",
        "route_decision": {"need_tool": False},
        "answer": {
            "lead_tier": "B", "intent_level": "中",
            "pain_points": [], "missing_info": [], "risks": [],
            "recommended_product": None, "next_actions": [],
            "draft_reply": "...", "needs_human_review": False,
            "triggered_rules": [], "evidence": [],
        },
    }
    llm = _mock_llm([bad_chat, fixed_answer])
    runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace, max_turns=3)

    result = runner.run("hi")
    assert result.finished is True
    # 应有一条 reasoning trace 记录了 JSON 修复事件
    assert any(
        s.step_type == "reasoning" and s.error is not None
        for s in trace.steps()
    )


# =============================================================================
# 新增：自定义 Playbook 加载到 system prompt
# =============================================================================

REAL_DATA_DIR = PROJECT_ROOT / "data"


def _isolate_runner_data(tmp_path: Path) -> Path:
    """复制业务资产到 tmp_path/data，并 set_data_dir 指过去。

    这样 Runner 既能读到 product_catalog/sales_sop/forbidden_claims，
    又能让 custom_playbooks 走 tmp 路径不影响真实目录。
    """
    dst = tmp_path / "data"
    dst.mkdir(parents=True, exist_ok=True)
    for fname in ("product_catalog.json", "sales_sop.md", "forbidden_claims.md"):
        shutil.copy2(REAL_DATA_DIR / fname, dst / fname)
    persistence.set_data_dir(dst)
    return dst


def test_runner_loads_custom_playbooks(tmp_path, monkeypatch):
    """写一份临时 playbook，验证 system prompt 包含其 title，
    且 init trace 的 context_summary 出现 playbooks_loaded=1。
    """
    data_dir = _isolate_runner_data(tmp_path)
    # 写 playbook
    pb_dir = data_dir / "custom_playbooks"
    pb_dir.mkdir()
    (pb_dir / "_example.md").write_text(
        "---\ntitle: 应该被忽略的示例\n---\n\n示例内容\n", encoding="utf-8"
    )
    (pb_dir / "my_pb.md").write_text(
        "---\ntitle: 我的专属判断套路\n---\n\n客户主动提 ROI 必有预算\n",
        encoding="utf-8",
    )

    try:
        skill = load_skill(SKILL_DIR)
        tools = _make_registry()
        trace = TraceCollector()
        llm = _mock_llm([{
            "reasoning_summary": "ok",
            "route_decision": {"need_tool": False},
            "answer": {
                "lead_tier": "B", "intent_level": "中",
                "pain_points": [], "missing_info": [], "risks": [],
                "recommended_product": None, "next_actions": [],
                "draft_reply": "...", "needs_human_review": False,
                "triggered_rules": [], "evidence": [],
            },
        }])
        runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace, max_turns=3)
        result = runner.run("hi")
        assert result.finished is True

        # 1) system prompt 里应该包含自定义 playbook 的 title
        # 取 mock 第一次调用的 messages 第 0 条
        first_call = llm.chat.call_args_list[0]
        messages = first_call.args[0] if first_call.args else first_call.kwargs["messages"]
        system_content = messages[0]["content"]
        assert "我的专属判断套路" in system_content
        # _example.md 不应该被注入
        assert "应该被忽略的示例" not in system_content
        # 也应该有标题段
        assert "销售自定义方法论 Playbook" in system_content

        # 2) init trace 的 context_summary 应含 playbooks_loaded=1
        init_step = [s for s in trace.steps() if s.step_type == "init"][0]
        assert "playbooks_loaded=1" in (init_step.context_summary or "")
    finally:
        persistence.set_data_dir(None)


def test_runner_empty_playbooks_shows_placeholder(tmp_path):
    """没有 playbook（或只有 _example.md）时，prompt 出现"（暂无自定义方法论）"占位。"""
    data_dir = _isolate_runner_data(tmp_path)
    pb_dir = data_dir / "custom_playbooks"
    pb_dir.mkdir()
    (pb_dir / "_example.md").write_text(
        "---\ntitle: x\n---\n\nx\n", encoding="utf-8"
    )

    try:
        skill = load_skill(SKILL_DIR)
        tools = _make_registry()
        trace = TraceCollector()
        llm = _mock_llm([{
            "reasoning_summary": "ok",
            "route_decision": {"need_tool": False},
            "answer": {
                "lead_tier": "C", "intent_level": "低",
                "pain_points": [], "missing_info": [], "risks": [],
                "recommended_product": None, "next_actions": [],
                "draft_reply": "...", "needs_human_review": False,
                "triggered_rules": [], "evidence": [],
            },
        }])
        runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace, max_turns=3)
        runner.run("hi")
        first_call = llm.chat.call_args_list[0]
        messages = first_call.args[0] if first_call.args else first_call.kwargs["messages"]
        system_content = messages[0]["content"]
        assert "（暂无自定义方法论）" in system_content
        init_step = [s for s in trace.steps() if s.step_type == "init"][0]
        assert "playbooks_loaded=0" in (init_step.context_summary or "")
    finally:
        persistence.set_data_dir(None)
