"""
Tool: query_product

故意采用「朴素关键词加权」而非向量召回：
  - Demo 阶段需要 **可审计**——评审能在 Trace 里清楚看到为什么这条命中
  - 业务资料量小（3 个产品 + 19 个 feature + 3 个案例），关键词足够
  - 真上线时可平替为 BM25 / 向量库，接口保持稳定

返回结果每条都带 source_id：
  - 产品级命中 -> product_id
  - 功能级命中 -> feature_id（同时附 parent_product_id）
  - 案例级命中 -> case_id
LLM 在生成 evidence[].source_id 时必须从这些 id 里取。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "product_catalog.json"

# 命中权重（值越大优先级越高）。可调，face validity 即可。
W_NAME = 5
W_TAGLINE = 3
W_SOLVES = 4
W_INDUSTRY = 3
W_USECASE = 3
W_DESC = 2
W_FEATURE_NAME = 4
W_FEATURE_DESC = 2
W_CASE_INDUSTRY = 3
W_CASE_SCENARIO = 2

_STOPWORDS = {
    "的", "了", "和", "与", "及", "或", "是", "有", "在", "我们", "你们", "他们",
    "想", "要", "可以", "能", "需要", "做", "帮",
    "the", "a", "an", "of", "for", "and", "or", "to", "is", "are", "we", "you", "they",
}


# ----------------- 加载 & 索引（首次调用时构建） -----------------

_catalog_cache: Optional[dict] = None


def _load_catalog() -> dict:
    global _catalog_cache
    if _catalog_cache is None:
        with CATALOG_PATH.open(encoding="utf-8") as fp:
            _catalog_cache = json.load(fp)
    return _catalog_cache


def reset_cache() -> None:
    """测试用：让单测能注入临时 catalog"""
    global _catalog_cache
    _catalog_cache = None


# ----------------- 分词 -----------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-龥]")  # 英文/数字按词，中文按字


def _tokens(text: str) -> list[str]:
    if not text:
        return []
    raw = _TOKEN_RE.findall(text.lower())
    return [t for t in raw if t not in _STOPWORDS]


def _score(query_tokens: list[str], field_text: str, weight: int) -> int:
    """对单个字段计分：每命中一个 query token +weight；同 token 仅记一次"""
    if not field_text:
        return 0
    field_lower = field_text.lower()
    hits = sum(1 for t in set(query_tokens) if t in field_lower)
    return hits * weight


# ----------------- 主入口 -----------------

QUERY_PRODUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索关键词或产品/功能名"},
        "top_k": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10,
                  "description": "返回前 k 条命中（按相关性降序）"},
    },
    "required": ["query"],
}


def query_product(args: dict) -> dict:
    """根据关键词在 product_catalog.json 里检索

    返回结构（Tool 输出尽可能扁平、可读，便于 LLM 直接引用）：
    {
      "query": "...",
      "hits": [
        {
          "source_id": "PROD-CS-AGENT",
          "kind": "product" | "feature" | "case",
          "score": 12,
          "summary": "客服智能体：7x24 不眠不休...",
          "data": { ... 原始字段 ... }
        },
        ...
      ],
      "total_matches": int,
      "note": "若 hits 为空，请提示需人工确认，不要编造"
    }
    """
    query = (args or {}).get("query", "")
    if not isinstance(query, str) or not query.strip():
        return {"query": query, "hits": [], "total_matches": 0,
                "note": "query 为空，无法检索"}

    top_k = int((args or {}).get("top_k", 3))
    top_k = max(1, min(top_k, 10))

    q_tokens = _tokens(query)
    if not q_tokens:
        return {"query": query, "hits": [], "total_matches": 0,
                "note": "未提取到有效关键词"}

    catalog = _load_catalog()

    candidates: list[dict[str, Any]] = []

    # ---- 产品级 + 功能级 ----
    for product in catalog.get("products", []):
        score = 0
        score += _score(q_tokens, product.get("name", ""), W_NAME)
        score += _score(q_tokens, product.get("tagline", ""), W_TAGLINE)
        score += _score(q_tokens, product.get("description", ""), W_DESC)
        score += _score(q_tokens, " ".join(product.get("solves_problems", [])), W_SOLVES)
        score += _score(q_tokens, " ".join(product.get("target_industries", [])), W_INDUSTRY)
        score += _score(q_tokens, " ".join(product.get("typical_use_cases", [])), W_USECASE)

        if score > 0:
            candidates.append({
                "source_id": product["source_id"],
                "kind": "product",
                "score": score,
                "summary": f"{product['name']}：{product.get('tagline', '')}",
                "data": {
                    "product_id": product["product_id"],
                    "name": product["name"],
                    "tagline": product.get("tagline", ""),
                    "solves_problems": product.get("solves_problems", []),
                    "target_industries": product.get("target_industries", []),
                    "typical_use_cases": product.get("typical_use_cases", []),
                    "delivery_window": product.get("delivery_window", ""),
                    "pricing": product.get("pricing", ""),
                    "evidence_refs": product.get("evidence_refs", []),
                },
            })

        # 功能级也参与匹配
        for feat in product.get("key_features", []):
            f_score = (
                _score(q_tokens, feat.get("name", ""), W_FEATURE_NAME)
                + _score(q_tokens, feat.get("desc", ""), W_FEATURE_DESC)
            )
            if f_score > 0:
                candidates.append({
                    "source_id": feat["feature_id"],
                    "kind": "feature",
                    "score": f_score,
                    "summary": f"{feat['name']}（{product['name']}）：{feat.get('desc', '')}",
                    "data": {
                        "feature_id": feat["feature_id"],
                        "parent_product_id": product["product_id"],
                        "name": feat["name"],
                        "desc": feat.get("desc", ""),
                    },
                })

    # ---- 参考案例 ----
    for case in catalog.get("reference_cases", []):
        c_score = (
            _score(q_tokens, case.get("industry", ""), W_CASE_INDUSTRY)
            + _score(q_tokens, case.get("scenario", ""), W_CASE_SCENARIO)
        )
        if c_score > 0:
            candidates.append({
                "source_id": case["source_id"],
                "kind": "case",
                "score": c_score,
                "summary": f"案例 · {case['industry']}：{case['scenario']}",
                "data": {
                    "case_id": case["case_id"],
                    "anonymized_name": case.get("anonymized_name", ""),
                    "industry": case.get("industry", ""),
                    "scenario": case.get("scenario", ""),
                    "before_after": case.get("before_after", {}),
                },
            })

    # 排序 & 截断
    candidates.sort(key=lambda x: x["score"], reverse=True)
    hits = candidates[:top_k]

    return {
        "query": query,
        "hits": hits,
        "total_matches": len(candidates),
        "note": (
            "若 hits 为空，按 forbidden_claims 的安全回复处理，禁止编造。"
            if not hits
            else "每条 hit 的 source_id 必须出现在 answer.evidence 里。"
        ),
    }
