"""
FastAPI 入口

路由：
  GET  /api/health                健康检查
  GET  /api/leads                 返回示例线索列表（前端下拉用）
  POST /api/analyze               核心：吃线索 → 跑 Runner → 返回 {result, trace}
  GET  /api/analyses              最近 N 条分析日志（可按 openid 过滤）

  POST /api/feedback              销售提交"实际成交结果"
  GET  /api/analytics/feedback    聚合：混淆矩阵 + 打脸案例 + 数据一致性

  GET    /api/playbooks           列表
  GET    /api/playbooks/{name}    单条
  PUT    /api/playbooks/{name}    新建/覆盖
  DELETE /api/playbooks/{name}    删除

  GET  /                          前端单页（StaticFiles 托管 frontend/）

设计要点：
  - Runner 实例每次请求都新建（每个请求一个独立 TraceCollector）
  - LLMClient / Skill / ToolRegistry 在应用启动时初始化一次，跨请求复用
  - 分析日志 / 反馈日志 / playbook 都走 backend.persistence
  - openid 不做格式校验（外部 ID 我们不约束格式），但反馈端 openid 必填
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    FeedbackRequest,
    PlaybookPutRequest,
)
from .config import get_settings
from .llm_client import LLMClient
from .skill_loader import load_skill, SkillLoadError
from .tools.registry import ToolRegistry, ToolSpec
from .tools.query_product import query_product, QUERY_PRODUCT_SCHEMA
from .trace import TraceCollector
from .runner import ReActRunner
from . import persistence
from .persistence import (
    append_jsonl,
    read_jsonl,
    iso_now,
    safe_playbook_path,
    PlaybookNameError,
    list_playbooks,
    get_playbooks_dir,
    parse_playbook_text,
    dump_playbook_text,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = PROJECT_ROOT / "skills" / "lead-scoring-followup"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# 日志文件名（集中常量化便于审计 / 测试 monkeypatch）
ANALYSIS_LOG = "analysis_log.jsonl"
FEEDBACK_LOG = "feedback_log.jsonl"


# -----------------------------------------------------------------------------
# 应用初始化
# -----------------------------------------------------------------------------

app = FastAPI(title="AI Growth Copilot", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# 共享单例：Skill + Tool 注册表
# LLMClient 因为读环境变量、且测试时常被 mock，所以 lazy 化
# -----------------------------------------------------------------------------

_skill = None
_tools: Optional[ToolRegistry] = None
_llm: Optional[LLMClient] = None


def _get_skill():
    global _skill
    if _skill is None:
        try:
            _skill = load_skill(SKILL_DIR)
        except SkillLoadError as e:
            raise RuntimeError(f"Skill 加载失败：{e}") from e
    return _skill


def _get_tools() -> ToolRegistry:
    global _tools
    if _tools is None:
        reg = ToolRegistry()
        reg.register(ToolSpec(
            name="query_product",
            description="检索本地产品/服务/案例资料，返回带 source_id 的结果。",
            parameters_schema=QUERY_PRODUCT_SCHEMA,
            fn=query_product,
        ))
        _tools = reg
    return _tools


def _get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm


# -----------------------------------------------------------------------------
# 路由：基础
# -----------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    """简单健康检查。也用来验证 Skill / Tools 是否加载 OK。"""
    try:
        skill = _get_skill()
        tools = _get_tools()
        return {
            "ok": True,
            "skill": skill.prompt_version,
            "allowed_tools": list(skill.allowed_tools),
            "registered_tools": [t["name"] for t in tools.describe_all()],
            "react_max_turns": get_settings().react_max_turns,
        }
    except Exception as e:  # pylint: disable=broad-except
        return {"ok": False, "error": str(e)}


@app.get("/api/leads")
def list_leads() -> dict:
    """返回 data/leads.json 给前端做示例下拉。

    注意这里取的是 persistence.get_data_dir()，方便测试切到 tmp_path 时
    依然能拿到 leads（前提是测试需要时再 cp 一份过去；不需要则前端就走空）。
    实际默认指向 PROJECT_ROOT/data。
    """
    leads_path = persistence.get_data_dir() / "leads.json"
    if not leads_path.is_file():
        # 测试目录里没有 leads.json 时不直接 5xx，返回空表
        return {"leads": []}
    with leads_path.open(encoding="utf-8") as fp:
        leads = json.load(fp)
    return {"leads": leads}


# -----------------------------------------------------------------------------
# 路由：核心 analyze
# -----------------------------------------------------------------------------

def _extract_prompt_version_from_trace(trace_steps: list) -> Optional[str]:
    """从 trace 的 init 步骤里取 skill 版本号（name@version）。

    Trace 第一条总是 init（参见 runner.run），其 prompt_version 字段就是答案。
    没找到时返回 None（不抛错）。
    """
    for step in trace_steps:
        step_type = getattr(step, "step_type", None) or (
            step.get("step_type") if isinstance(step, dict) else None
        )
        if step_type == "init":
            pv = getattr(step, "prompt_version", None) or (
                step.get("prompt_version") if isinstance(step, dict) else None
            )
            return pv
    return None


def _record_analysis(
    *,
    openid: Optional[str],
    lead_text: str,
    answer: dict,
    trace_steps: list,
) -> None:
    """把一次成功 / 兜底的分析压一行进 analysis_log.jsonl。

    截断 lead_text 防止单条记录炸大；trace 本身不写进日志（已可在前端展示）。
    """
    record = {
        "timestamp": iso_now(),
        "openid": openid,
        # 截断防爆：500 字以内，超出加省略号
        "lead_text": (lead_text[:500] + "…") if len(lead_text) > 500 else lead_text,
        "lead_tier": answer.get("lead_tier"),
        "intent_level": answer.get("intent_level"),
        "recommended_product": answer.get("recommended_product"),
        "needs_human_review": answer.get("needs_human_review"),
        "triggered_rules": list(answer.get("triggered_rules") or []),
        "prompt_version": _extract_prompt_version_from_trace(trace_steps),
        "trace_step_count": len(trace_steps),
    }
    try:
        append_jsonl(ANALYSIS_LOG, record)
    except OSError as e:
        # 写日志失败不要拉崩接口——只记一条 warning
        logger.warning("写 analysis_log 失败: %s", e)


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
    """核心入口：跑一次 ReAct 循环。

    返回结构：
      ok=True 永远（即便 Runner 走了兜底分支，兜底 answer 也是合法 result）
      result = answer dict
      trace  = list[TraceStep]
      openid = 原样回传
      error  = 仅在路由层硬错误（Skill 加载失败、LLMClient 初始化失败等）时填
    """
    lead_text = (req.lead_text or "").strip()
    if not lead_text:
        raise HTTPException(status_code=400, detail="lead_text 不能为空")

    try:
        skill = _get_skill()
        tools = _get_tools()
        llm = _get_llm()
    except RuntimeError as e:
        # 启动期资源缺失（如 API key 没配）→ 这是 5xx 性质的故障
        logger.exception("依赖初始化失败")
        return AnalyzeResponse(ok=False, error=str(e), trace=[], openid=req.openid)

    trace = TraceCollector()
    runner = ReActRunner(llm=llm, skill=skill, tools=tools, trace=trace)

    try:
        result = runner.run(lead_text)
    except Exception as e:  # pylint: disable=broad-except
        # Runner 内部已经把可预期错误兜成 _safe_fallback_answer 了
        # 走到这里说明出了未预期异常——记录后给前端一份兜底响应
        logger.exception("Runner 抛出未捕获异常")
        return AnalyzeResponse(
            ok=False,
            error=f"内部错误：{e}",
            trace=trace.steps(),
            openid=req.openid,
        )

    # 即便走了兜底分支也要记日志，这样反馈环可以看到"AI 当时给了什么判断"
    _record_analysis(
        openid=req.openid,
        lead_text=lead_text,
        answer=result.answer,
        trace_steps=trace.steps(),
    )

    return AnalyzeResponse(
        ok=True,
        result=result.answer,
        trace=trace.steps(),
        openid=req.openid,
    )


@app.get("/api/analyses")
def list_analyses(
    limit: int = Query(20, ge=1, le=200),
    openid: Optional[str] = None,
) -> dict:
    """最近 N 条分析日志（timestamp desc）。

    可选 openid 过滤：用于在前端"该客户的历史分析"视图。
    """
    rows = read_jsonl(ANALYSIS_LOG)
    if openid:
        rows = [r for r in rows if r.get("openid") == openid]
    # 直接按时间字符串倒排即可（ISO8601 字典序 = 时间序）
    rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return {"items": rows[:limit], "total": len(rows)}


# -----------------------------------------------------------------------------
# 路由：反馈循环
# -----------------------------------------------------------------------------

@app.post("/api/feedback")
def post_feedback(req: FeedbackRequest) -> dict:
    """销售提交"实际成交结果"。outcome 枚举校验由 Pydantic Literal 做。

    刻意不在这一步 join analysis_log——保持写入路径轻量，
    join 留到 analytics 查询时再做（多次反馈也允许，按时间序保存）。
    """
    record = {
        "timestamp": iso_now(),
        "openid": req.openid,
        "outcome": req.outcome,
        "deal_amount": req.deal_amount,
        "note": req.note,
    }
    try:
        append_jsonl(FEEDBACK_LOG, record)
    except OSError as e:
        logger.exception("写 feedback_log 失败")
        raise HTTPException(status_code=500, detail=f"写入失败：{e}") from e
    return {"ok": True, "recorded": record}


# Surprises 的判定规则
# "打脸"定义：
#   - AI 预测 A 级（高价值）但实际 no_deal / lost → 高估
#   - AI 预测 D 级（不相关）但实际 deal           → 低估
# B/C/pending 不算"打脸"——只针对极端预测和明确反向结果。
_SURPRISE_RULES = (
    ("A", {"no_deal", "lost"}),
    ("D", {"deal"}),
)


@app.get("/api/analytics/feedback")
def feedback_analytics() -> dict:
    """聚合反馈：

    - total_feedback        反馈总数
    - confusion_matrix      预测 tier × outcome 的混淆矩阵
                            外层 dict 始终包含 A/B/C/D 四个 key，内层只填出现过的 outcome
    - surprises             "AI 高估/低估"的样本（最多 10 条，按 timestamp desc）
    - no_match_feedback_count
                            feedback 中 openid 在 analysis_log 找不到的数量
                            （数据一致性指标：销售有没有给"没分析过"的 openid 提反馈？）

    实现策略：
      - 每条 feedback 用 openid join 最近一次 analysis_log
        （为什么取最近一次：同一个 openid 可能被分析多次；以销售收到反馈时刻
        前的最新一次预测为准，更接近"销售当时看到的 AI 建议"）
    """
    analyses = read_jsonl(ANALYSIS_LOG)
    feedbacks = read_jsonl(FEEDBACK_LOG)

    # 按 openid -> 最新一次 analysis 建索引（max timestamp）
    latest_by_openid: dict[str, dict] = {}
    for row in analyses:
        oid = row.get("openid")
        if not oid:
            continue
        prev = latest_by_openid.get(oid)
        if prev is None or row.get("timestamp", "") > prev.get("timestamp", ""):
            latest_by_openid[oid] = row

    # 混淆矩阵：A/B/C/D 四行（未匹配的归到 "UNMATCHED" 一行以便审计）
    matrix: dict[str, Counter] = {tier: Counter() for tier in ("A", "B", "C", "D")}
    matrix["UNMATCHED"] = Counter()

    surprises: list[dict] = []
    no_match = 0

    for fb in feedbacks:
        oid = fb.get("openid")
        outcome = fb.get("outcome")
        if not oid or not outcome:
            continue
        match = latest_by_openid.get(oid)
        if match is None:
            no_match += 1
            matrix["UNMATCHED"][outcome] += 1
            continue
        tier = match.get("lead_tier") or "UNMATCHED"
        if tier not in matrix:
            # 未知 tier（理论上不该发生；防呆）
            matrix[tier] = Counter()
        matrix[tier][outcome] += 1

        # 判断"打脸"
        for surprise_tier, surprise_outcomes in _SURPRISE_RULES:
            if tier == surprise_tier and outcome in surprise_outcomes:
                surprises.append({
                    "openid": oid,
                    "predicted_tier": tier,
                    "outcome": outcome,
                    "note": fb.get("note"),
                    "timestamp": fb.get("timestamp"),
                })
                break

    # Counter → dict 便于 JSON 输出
    matrix_out = {tier: dict(c) for tier, c in matrix.items()}
    surprises.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    return {
        "total_feedback": len(feedbacks),
        "confusion_matrix": matrix_out,
        "surprises": surprises[:10],
        "no_match_feedback_count": no_match,
    }


# -----------------------------------------------------------------------------
# 路由：自定义 Playbook CRUD
# -----------------------------------------------------------------------------

@app.get("/api/playbooks")
def playbooks_list() -> dict:
    return {"items": list_playbooks()}


@app.get("/api/playbooks/{name}")
def playbook_get(name: str) -> dict:
    try:
        path = safe_playbook_path(name)
    except PlaybookNameError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"playbook 不存在: {name}")
    text = path.read_text(encoding="utf-8")
    meta, body = parse_playbook_text(text)
    return {
        "name": name,
        "title": str(meta.get("title") or Path(name).stem),
        "content": text,
        "body": body,
        "frontmatter": meta,
    }


@app.put("/api/playbooks/{name}")
def playbook_put(name: str, req: PlaybookPutRequest) -> dict:
    """新建或覆盖。

    入参 content 可以已经带 frontmatter，也可以不带。
    如果 title 单独传入，则覆盖到 frontmatter（保证文件里始终有 title 字段）。
    """
    try:
        path = safe_playbook_path(name)
    except PlaybookNameError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    incoming = req.content or ""
    meta, body = parse_playbook_text(incoming)
    if req.title:
        meta["title"] = req.title
    if "title" not in meta or not meta["title"]:
        # 缺省 title 用文件名 stem，避免前端列表展示空白
        meta["title"] = Path(name).stem
    final_text = dump_playbook_text(meta, body or incoming.strip())

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(final_text, encoding="utf-8")
    return {"ok": True, "name": name, "title": str(meta["title"])}


@app.delete("/api/playbooks/{name}")
def playbook_delete(name: str) -> dict:
    try:
        path = safe_playbook_path(name)
    except PlaybookNameError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"playbook 不存在: {name}")
    path.unlink()
    return {"ok": True}


# -----------------------------------------------------------------------------
# 前端静态文件托管（必须放最后，否则 /api/* 会被前端 catch-all 抢走）
# -----------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
