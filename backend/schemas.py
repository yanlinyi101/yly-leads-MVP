"""
Pydantic 模型：API 输入输出 + 内部数据结构

注意：
  - openid 是"对外业务系统的客户/会话 ID"（如微信 openid / CRM 客户 ID）；
    本项目不解析 openid 内容，只把它作为 join key 关联 analysis_log 与 feedback_log。
  - Outcome 用 Literal 限制枚举值，让 FastAPI 自动 422，非合法值不会进入业务层。
"""
from typing import Any, Optional, Literal
from pydantic import BaseModel, Field


# -----------------------------------------------------------------------------
# Analyze
# -----------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    lead_text: str = Field(..., description="线索原始文本，可包含来源/公司/留言等")
    lead_id: Optional[str] = Field(None, description="可选，用于追踪")
    openid: Optional[str] = Field(
        None,
        description="外部业务 ID（如微信 openid / CRM 客户 ID），用于关联反馈与成交结果",
    )


class TraceStep(BaseModel):
    step_id: int
    step_type: str          # reasoning | act | observe | answer | error
    skill_name: Optional[str] = None
    prompt_version: Optional[str] = None
    context_summary: Optional[str] = None
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None
    tool_output: Optional[Any] = None
    output_summary: Optional[str] = None   # 决策摘要，非完整 CoT
    latency_ms: Optional[int] = None
    error: Optional[str] = None


class AnalyzeResponse(BaseModel):
    ok: bool
    result: Optional[dict] = None
    trace: list[TraceStep] = []
    error: Optional[str] = None
    openid: Optional[str] = Field(None, description="原样回传，便于前端关联反馈")


# -----------------------------------------------------------------------------
# Feedback（销售补充"实际成交结果"，用于构造混淆矩阵）
# -----------------------------------------------------------------------------

# Outcome 枚举：
#   deal     = 成交
#   no_deal  = 客户明确拒绝/无意向（最终判定）
#   pending  = 仍在跟进
#   lost     = 流失（中断联络/竞品赢单等）
Outcome = Literal["deal", "no_deal", "pending", "lost"]


class FeedbackRequest(BaseModel):
    openid: str = Field(..., description="必填，关联 analysis_log")
    outcome: Outcome = Field(..., description="销售确认的最终结果")
    deal_amount: Optional[float] = Field(
        None, ge=0, description="成交金额（仅 outcome=deal 时有意义）"
    )
    note: Optional[str] = Field(None, description="备注，描述真实情境")


# -----------------------------------------------------------------------------
# Playbook
# -----------------------------------------------------------------------------

class PlaybookPutRequest(BaseModel):
    """PUT /api/playbooks/{name} 的请求体。

    content 是 markdown 正文（可包含或不包含 frontmatter）；
    title 可选——若提供则覆盖 frontmatter 里的 title。
    """
    content: str = Field(..., description="markdown 正文，可含 frontmatter")
    title: Optional[str] = None
