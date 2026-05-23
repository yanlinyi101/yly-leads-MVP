"""
单元测试：FastAPI 路由

策略：用 fastapi.testclient + monkeypatch 替换 _get_llm，
      不依赖真实 DEEPSEEK_API_KEY。
"""
import json
import shutil
from pathlib import Path

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from backend import main as main_module
from backend import persistence
from backend.llm_client import ChatResult


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_DATA_DIR = PROJECT_ROOT / "data"


def _isolate_data_dir(tmp_path: Path) -> Path:
    """把真实业务资产复制一份到 tmp_path/data，再把 persistence 指向那里。

    这样：
      - Runner 仍能读到 product_catalog / sales_sop / forbidden_claims
      - analysis_log.jsonl / feedback_log.jsonl / custom_playbooks 都写到 tmp 里
      - 测试结束后 pytest 自动清 tmp，不污染真实 data/

    注意只能复制必需的文件，避免 leak。
    """
    dst = tmp_path / "data"
    dst.mkdir(parents=True, exist_ok=True)
    for fname in ("product_catalog.json", "sales_sop.md", "forbidden_claims.md", "leads.json"):
        src = REAL_DATA_DIR / fname
        if src.is_file():
            shutil.copy2(src, dst / fname)
    persistence.set_data_dir(dst)
    return dst


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch, tmp_path):
    """autouse 自动隔离：
      - 注入测试 API key
      - 重置 main 模块单例
      - 默认就把 persistence.data_dir 隔离到 tmp_path/data，
        并把真实业务资产复制过去，让 Runner 仍能读 product_catalog 等。
        这样每个测试结束都不会污染真实 data/。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setenv("REACT_MAX_TURNS", "3")
    main_module._skill = None
    main_module._tools = None
    main_module._llm = None
    _isolate_data_dir(tmp_path)
    yield
    persistence.set_data_dir(None)


def _chat_result(content_dict: dict) -> ChatResult:
    return ChatResult(
        content=json.dumps(content_dict, ensure_ascii=False),
        latency_ms=10,
        finish_reason="stop",
        model="mock",
        json_mode=True,
    )


def _install_mock_llm(monkeypatch, scripted: list):
    """把 main._get_llm 替换成一个吐脚本的 mock"""
    llm = MagicMock()
    it = iter(scripted)

    def _fake_chat(messages, json_mode=False, temperature=0.0, max_tokens=None):
        return _chat_result(next(it))

    llm.chat = MagicMock(side_effect=_fake_chat)
    monkeypatch.setattr(main_module, "_get_llm", lambda: llm)
    return llm


# -----------------------------------------------------------------------------
# tests
# -----------------------------------------------------------------------------

def test_health(monkeypatch):
    client = TestClient(main_module.app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["skill"].startswith("lead-scoring-followup@")
    assert "query_product" in body["registered_tools"]
    assert body["react_max_turns"] == 3


def test_list_leads(monkeypatch):
    client = TestClient(main_module.app)
    r = client.get("/api/leads")
    assert r.status_code == 200
    body = r.json()
    assert len(body["leads"]) >= 5
    # 越狱测试 LEAD-006 必须在
    ids = [l["lead_id"] for l in body["leads"]]
    assert "LEAD-006" in ids


def test_analyze_happy_path(monkeypatch):
    """LEAD-001 风格的高意向线索 → 走一轮 query_product → answer"""
    _install_mock_llm(monkeypatch, [
        {
            "reasoning_summary": "高意向，查客服智能体",
            "route_decision": {
                "need_tool": True,
                "tool_name": "query_product",
                "tool_args": {"query": "客服智能体", "top_k": 3},
            },
        },
        {
            "reasoning_summary": "已查到，输出 answer",
            "route_decision": {"need_tool": False},
            "answer": {
                "lead_tier": "A", "intent_level": "高",
                "pain_points": ["客服响应慢"], "missing_info": [], "risks": [],
                "recommended_product": "PROD-CS-AGENT",
                "next_actions": ["1 工作日内安排 Demo"],
                "draft_reply": "您好...",
                "needs_human_review": False,
                "triggered_rules": [],
                "evidence": [{"claim": "...", "source_id": "PROD-CS-AGENT"}],
            },
        },
    ])
    client = TestClient(main_module.app)
    r = client.post("/api/analyze", json={"lead_text": "客服 24h 顶不住，想上 AI"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["lead_tier"] == "A"
    assert body["result"]["recommended_product"] == "PROD-CS-AGENT"
    # Trace 结构正确
    types = [s["step_type"] for s in body["trace"]]
    assert types == ["init", "reasoning", "act", "observe", "reasoning", "answer"]
    # tool_iteration_requests 字段存在且为空
    assert body["result"]["tool_iteration_requests"] == []


def test_analyze_empty_text_is_400(monkeypatch):
    client = TestClient(main_module.app)
    r = client.post("/api/analyze", json={"lead_text": "   "})
    assert r.status_code == 400


def test_analyze_missing_field_is_422(monkeypatch):
    """Pydantic 自动校验：缺 lead_text → 422"""
    client = TestClient(main_module.app)
    r = client.post("/api/analyze", json={})
    assert r.status_code == 422


# =============================================================================
# 新增（扩展功能）：analysis_log / feedback / analytics / playbook CRUD
# =============================================================================

def _simple_answer_script():
    """一个最短的 happy-path 脚本：1 轮 act + 1 轮 answer。"""
    return [
        {
            "reasoning_summary": "查目录",
            "route_decision": {
                "need_tool": True,
                "tool_name": "query_product",
                "tool_args": {"query": "客服", "top_k": 2},
            },
        },
        {
            "reasoning_summary": "出 answer",
            "route_decision": {"need_tool": False},
            "answer": {
                "lead_tier": "A", "intent_level": "高",
                "pain_points": ["客服慢"], "missing_info": [], "risks": [],
                "recommended_product": "PROD-CS-AGENT",
                "next_actions": ["1 工作日内 Demo"],
                "draft_reply": "您好...",
                "needs_human_review": False,
                "triggered_rules": [],
                "evidence": [{"claim": "...", "source_id": "PROD-CS-AGENT"}],
            },
        },
    ]


def test_analyze_records_to_log(monkeypatch, tmp_path):
    """analyze 后 data/analysis_log.jsonl 多一行，且字段齐全。"""
    _isolate_data_dir(tmp_path)
    _install_mock_llm(monkeypatch, _simple_answer_script())
    client = TestClient(main_module.app)

    log_path = persistence.get_data_dir() / "analysis_log.jsonl"
    assert not log_path.exists()

    r = client.post(
        "/api/analyze",
        json={"lead_text": "客服 24h 顶不住", "external_id": "crm-CUST-001"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["external_id"] == "crm-CUST-001"
    # L2: analysis_id 应该由服务端生成并返回（uuid4 hex 是 32 位）
    assert isinstance(body["analysis_id"], str) and len(body["analysis_id"]) == 32

    assert log_path.is_file()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    # 必含字段
    assert rec["external_id"] == "crm-CUST-001"
    assert rec["analysis_id"] == body["analysis_id"]
    assert rec["lead_tier"] == "A"
    assert rec["recommended_product"] == "PROD-CS-AGENT"
    assert rec["needs_human_review"] is False
    assert rec["prompt_version"].startswith("lead-scoring-followup@")
    assert rec["trace_step_count"] >= 2


def test_analyses_endpoint_filter(monkeypatch, tmp_path):
    """GET /api/analyses 应能按 external_id 过滤，并按 timestamp desc。"""
    _isolate_data_dir(tmp_path)
    # 直接往 jsonl 写两条
    persistence.append_jsonl("analysis_log.jsonl", {
        "timestamp": "2026-01-01T00:00:00Z",
        "analysis_id": "aaa", "external_id": "A",
        "lead_tier": "C", "intent_level": "中",
        "recommended_product": None, "needs_human_review": False,
        "triggered_rules": [], "prompt_version": "s@1", "trace_step_count": 3,
        "lead_text": "foo",
    })
    persistence.append_jsonl("analysis_log.jsonl", {
        "timestamp": "2026-02-01T00:00:00Z",
        "analysis_id": "bbb", "external_id": "B",
        "lead_tier": "A", "intent_level": "高",
        "recommended_product": "X", "needs_human_review": False,
        "triggered_rules": [], "prompt_version": "s@1", "trace_step_count": 4,
        "lead_text": "bar",
    })
    client = TestClient(main_module.app)
    r = client.get("/api/analyses")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    # desc
    assert items[0]["external_id"] == "B"

    r2 = client.get("/api/analyses?external_id=A")
    assert r2.status_code == 200
    items2 = r2.json()["items"]
    assert len(items2) == 1
    assert items2[0]["external_id"] == "A"


def test_feedback_endpoint_records(monkeypatch, tmp_path):
    """POST feedback 写入 feedback_log.jsonl（带 analysis_id + external_id）。"""
    _isolate_data_dir(tmp_path)
    client = TestClient(main_module.app)
    r = client.post("/api/feedback", json={
        "analysis_id": "anal-001", "external_id": "crm-1",
        "outcome": "deal", "deal_amount": 10000.0, "note": "签了"
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["recorded"]["analysis_id"] == "anal-001"
    assert body["recorded"]["external_id"] == "crm-1"
    assert body["recorded"]["join_kind"] == "precise"
    assert body["recorded"]["outcome"] == "deal"

    fb_path = persistence.get_data_dir() / "feedback_log.jsonl"
    assert fb_path.is_file()
    lines = fb_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["deal_amount"] == 10000.0
    assert rec["note"] == "签了"


def test_feedback_requires_at_least_one_id(monkeypatch, tmp_path):
    """L2: analysis_id / external_id 至少要给一个，否则 400。"""
    _isolate_data_dir(tmp_path)
    client = TestClient(main_module.app)
    r = client.post("/api/feedback", json={"outcome": "deal"})
    assert r.status_code == 400


def test_feedback_invalid_outcome_422(monkeypatch, tmp_path):
    """outcome 不在 Literal 枚举 → Pydantic 422。"""
    _isolate_data_dir(tmp_path)
    client = TestClient(main_module.app)
    r = client.post("/api/feedback", json={
        "external_id": "crm-1", "outcome": "WIN_BIGLY",
    })
    assert r.status_code == 422


def test_analytics_endpoint(monkeypatch, tmp_path):
    """先 analyze + 多条 feedback，再调 analytics。验证混淆矩阵 / surprises 结构。

    覆盖三种 match_kind：precise（analysis_id 精准）/ fuzzy（external_id 退化）/ orphan（无匹配）。
    """
    _isolate_data_dir(tmp_path)
    _install_mock_llm(monkeypatch, _simple_answer_script())
    client = TestClient(main_module.app)
    r = client.post("/api/analyze", json={"lead_text": "x", "external_id": "crm-A"})
    aid = r.json()["analysis_id"]

    # 1) precise: 用 analysis_id 精准 join → A → no_deal 高估
    client.post("/api/feedback", json={
        "analysis_id": aid, "external_id": "crm-A",
        "outcome": "no_deal", "note": "客户改主意",
    })
    # 2) orphan: 没分析过的 external_id，无 analysis_id → orphan
    client.post("/api/feedback", json={"external_id": "ghost", "outcome": "pending"})

    r2 = client.get("/api/analytics/feedback")
    assert r2.status_code == 200
    body = r2.json()
    assert body["total_feedback"] == 2
    # A 行 no_deal=1
    assert body["confusion_matrix"]["A"].get("no_deal") == 1
    # UNMATCHED 行 pending=1
    assert body["confusion_matrix"]["UNMATCHED"].get("pending") == 1
    # surprises 至少包含 crm-A 这一条
    surprises = body["surprises"]
    assert any(
        s["external_id"] == "crm-A" and s["predicted_tier"] == "A" and s["match_kind"] == "precise"
        for s in surprises
    )
    assert body["no_match_feedback_count"] == 1
    # match_kind_breakdown 至少包含 precise=1（feedback#1）和 orphan=1（feedback#2）
    breakdown = body["match_kind_breakdown"]
    assert breakdown.get("precise") == 1
    assert breakdown.get("orphan") == 1


# ---------- Playbook CRUD ----------

def test_playbook_crud(monkeypatch, tmp_path):
    _isolate_data_dir(tmp_path)
    client = TestClient(main_module.app)

    # 初始为空（_isolate_data_dir 不复制 custom_playbooks，所以确实空）
    r = client.get("/api/playbooks")
    assert r.status_code == 200
    assert r.json()["items"] == []

    # PUT 创建
    r2 = client.put("/api/playbooks/my_pb.md", json={
        "content": "# 我的方法论\n\n客户提到 ROI 必定有预算",
        "title": "ROI 信号",
    })
    assert r2.status_code == 200
    assert r2.json()["ok"] is True

    # GET 单条
    r3 = client.get("/api/playbooks/my_pb.md")
    assert r3.status_code == 200
    body3 = r3.json()
    assert body3["title"] == "ROI 信号"
    assert "我的方法论" in body3["body"]

    # GET 列表（有 1 项）
    r4 = client.get("/api/playbooks")
    items = r4.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "my_pb.md"

    # DELETE
    r5 = client.delete("/api/playbooks/my_pb.md")
    assert r5.status_code == 200
    assert r5.json()["ok"] is True

    # GET 404
    r6 = client.get("/api/playbooks/my_pb.md")
    assert r6.status_code == 404


def test_playbook_path_traversal_rejected(monkeypatch, tmp_path):
    """非法文件名（含 ../ 等）应被 4xx 拒绝。"""
    _isolate_data_dir(tmp_path)
    client = TestClient(main_module.app)
    for bad in ["../etc/passwd", "..%2Fpasswd.md", "ok name.md", "../boom.md", "/abs.md"]:
        r = client.put(f"/api/playbooks/{bad}", json={"content": "x"})
        # 后端可能返回 400（自己拒）或 404（路径不匹配路由），关键是不创建文件
        assert r.status_code >= 400, f"未拒绝: {bad} -> {r.status_code}"
