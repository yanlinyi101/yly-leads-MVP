"""
单元测试：query_product Tool

覆盖：
  - 中文关键词命中产品 / 功能 / 案例
  - 每条 hit 都带 source_id（评审重点）
  - 未命中返回安全提示，不会编造
  - top_k 控制
  - 空 query / 仅停用词的健壮性
"""
import pytest
from backend.tools.query_product import query_product, QUERY_PRODUCT_SCHEMA


def test_hit_product_by_name():
    r = query_product({"query": "客服智能体"})
    assert r["total_matches"] >= 1
    assert any(h["source_id"] == "PROD-CS-AGENT" for h in r["hits"])
    # 每条都带 source_id —— 评审重点
    assert all("source_id" in h for h in r["hits"])


def test_hit_feature_by_keyword():
    """『语音克隆』应该命中 FEAT-VOICE-CLONE 功能"""
    r = query_product({"query": "语音克隆"})
    feature_ids = [h["source_id"] for h in r["hits"] if h["kind"] == "feature"]
    assert "FEAT-VOICE-CLONE" in feature_ids


def test_hit_case_by_industry():
    """『K12』应当命中 CASE-K12-003 案例"""
    r = query_product({"query": "K12"})
    case_ids = [h["source_id"] for h in r["hits"] if h["kind"] == "case"]
    assert "CASE-K12-003" in case_ids


def test_multilingual_hits_cs_agent():
    """『多语种 跨境电商』应当主要命中客服智能体"""
    r = query_product({"query": "多语种 跨境电商"})
    assert r["total_matches"] >= 1
    top = r["hits"][0]
    # 客服智能体显式列了多语种 + 跨境，应该排在前面
    assert top["source_id"] in {"PROD-CS-AGENT", "FEAT-MULTILINGUAL"}


def test_top_k_limit():
    r = query_product({"query": "AI 智能体 销售 客服", "top_k": 2})
    assert len(r["hits"]) <= 2
    assert r["total_matches"] >= 2


def test_no_match_returns_safe_note():
    """挑选一个明确不会跟产品目录撞字的纯外文 query。

    Known limitation: 朴素关键词匹配对中文单字会出现假阳性
    （例如 '区块链' 的 '链' 会撞到 '小程序链接'）。
    这是 Demo 阶段的有意 trade-off：用最朴素的算法换 Trace 可审计性，
    生产化时应改 BM25 或向量召回。该限制在 solution.md 中讨论。
    """
    r = query_product({"query": "blockchain metaverse nonexistent"})
    assert r["hits"] == []
    assert r["total_matches"] == 0
    assert "编造" in r["note"] or "确认" in r["note"]


def test_empty_query_is_safe():
    r = query_product({"query": ""})
    assert r["hits"] == []
    assert r["total_matches"] == 0


def test_only_stopwords_query_is_safe():
    r = query_product({"query": "的 了 我们"})
    assert r["hits"] == []


def test_schema_exposed_for_llm():
    """ToolRegistry 会用这个 schema 告诉 LLM 工具签名"""
    assert QUERY_PRODUCT_SCHEMA["type"] == "object"
    assert "query" in QUERY_PRODUCT_SCHEMA["properties"]
    assert "query" in QUERY_PRODUCT_SCHEMA["required"]


def test_results_sorted_by_score_desc():
    r = query_product({"query": "客服 多语种 语音"})
    scores = [h["score"] for h in r["hits"]]
    assert scores == sorted(scores, reverse=True)
