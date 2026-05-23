"""
Pydantic 模型：API 输入输出 + 内部数据结构

ID 体系说明（L2 重构后）：
  - external_id：对外业务系统的"客户级"标识（如 CRM 客户 ID / 微信 openid / 邮箱 hash /
    官网表单 submission ID 等任意来源的客户引用）。本项目不解析其内容，只作为
    customer-level 的 join 维度。可选——非微信源 / 匿名表单也允许为空。
  - analysis_id：本次 /api/analyze 调用的精准 ID（UUID，服务端生成，回传给前端）。
    feedback 提交时优先用 analysis_id 做精准 join，避免"同一客户多次咨询 attribution 模糊"
    的问题。是 analysis-level 的 join 维度。

  customer 维度（external_id）和 analysis 维度（analysis_id）解耦：
    - 一个 external_id 可以对应 N 个 analysis_id（同一客户多次咨询）
    - 一条 feedback 通过 analysis_id 精准回填到某一次分析
    - 若 feedback 仅带 external_id（如线下手填），fallback 到"最近一次 analysis"做 join，
      但会被 analytics 标记为模糊匹配，不影响精准样本计数

  Outcome 用 Literal 限制枚举值，让 FastAPI 自动 422，非合法值不会进入业务层。
"""
from typing import Any, Optional, Literal
from pydantic import BaseModel, Field


# -----------------------------------------------------------------------------
# Analyze
# -----------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    lead_text: str = Field(..., description="线索原始文本，可包含来源/公司/留言等")
    lead_id: Optional[str] = Field(None, description="可选，用于追踪")
    external_id: Optional[str] = Field(
        None,
        description="客户级外部 ID（CRM 客户 ID / 微信 openid / 邮箱 hash 等），用于客户维度的反馈聚合",
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
    external_id: Optional[str] = Field(None, description="原样回传，客户级关联用")
    analysis_id: Optional[str] = Field(
        None,
        description="本次分析的精准 ID（服务端 UUID）。feedback 提交时带上即可精准回填",
    )


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
    analysis_id: Optional[str] = Field(
        None,
        description="本次分析的精准 ID（推荐携带）。若提供，将精准 join 到该次 analysis_log",
    )
    external_id: Optional[str] = Field(
        None,
        description="客户级外部 ID。若未提供 analysis_id 则用此作为 fallback join key",
    )
    outcome: Outcome = Field(..., description="销售确认的最终结果")
    deal_amount: Optional[float] = Field(
        None, ge=0, description="成交金额（仅 outcome=deal 时有意义）"
    )
    note: Optional[str] = Field(None, description="备注，描述真实情境")

    def join_key_kind(self) -> str:
        """返回这条 feedback 用于 join 的维度标签，方便 analytics 标注精度。"""
        if self.analysis_id:
            return "precise"      # analysis_id 精准
        if self.external_id:
            return "fuzzy"        # external_id + 最近一次，模糊
        return "orphan"           # 两者都没有，无法 join


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
