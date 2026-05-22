"""
ReAct Agent Runner —— 项目核心 ⭐

============================================================
本模块负责：
  把一条原始营销线索文本 → 经过 ReAct（Reasoning / Act / Observe / Answer）
  四阶段的多轮循环 → 产出结构化的分析结果 + 可审计的执行 Trace。

为什么单独抽出 Runner（而不是把循环逻辑写在 FastAPI 路由里）？
  - 单元测试好做：可以 mock LLMClient 用脚本化响应跑各种边界
  - 业务可移植：换前端、换路由（如改 CLI、改批处理）时只复用 Runner
  - 边界清晰：路由层只负责协议层（HTTP），Runner 负责"代理决策"
============================================================

关键设计决策（候选人决定 · 面试时需能解释每一条的"为什么"）：

  D1【多轮自适应 + 上限 3 轮】
     由 REACT_MAX_TURNS 控制（.env 可调）。
     不固定单轮，因为部分线索（如 LEAD-005 多语种 + 图片识别）需要查多次目录；
     不放任意多轮，因为：
       (a) Demo 阶段 token 预算敏感
       (b) 上限明确才有"可预测的最大耗时"，方便接入实时业务

  D2【LLM 输出 route_decision JSON，后端解析】
     比起依赖模型原生 function calling：
       - 不绑定特定厂商的 function-calling 协议（DeepSeek/通义/Kimi 实现不一致）
       - JSON 字段可以放进 Trace 直接给评审看，路由决策"可审计"
       - 调试时人眼也能读
     代价：多了一层 JSON 解析失败的可能 → 见 D3。

  D3【错误恢复分层】
     LLM 调用层失败  → 同轮重试 1 次 → 仍失败则进入兜底 answer
     JSON 解析失败   → 把上一条带错的 assistant 消息塞回，让 LLM 修一次
     Tool 调用失败   → 不算 Runner 错误：把错误塞进 observe 给 LLM，由它决定是否换路或终止
     未授权 Tool     → Runner 直接拒绝并提示 LLM 终止（继续下一轮）
     SDK 自带的 max_retries 已经做了一层网络层重试，这里是业务层兜底。

  D4【超轮强制收敛】
     到达 max_turns 仍 need_tool=true 时，Runner 注入一条 system 提示
     "已达上限，必须立即给出 answer"，并在 Trace 写一条 max_turns_guard 步骤。
     这样面试官能直接看到状态转换，而不是看到 Runner 突然崩或硬切。

  D5【Prompt 全文塑入业务资产】
     Skill instructions + product_catalog 目录概览 + sales_sop 全文 + forbidden_claims 全文 + Tool 签名
     全部资产 < 5KB，DeepSeek 上下文足够，不做压缩。
     好处：LLM 一眼看到所有约束，少绕弯子；Trace 里存 prompt_version 即可复现。
     注意 product_catalog 只放"目录概览"（不展开 feature 详情），
     这样 LLM 会被引导主动调 query_product 拉细节，让 Trace 出现 Act/Observe 步骤。

  D6【Trace 暴露原则】
     暴露的：reasoning_summary（≤80 字，已在 SKILL.md 约束）、route_decision、
            tool I/O、prompt_version、latency_ms、错误信息、答案要点。
     不暴露的：raw chain-of-thought、完整 prompt 内容（仅暴露长度/哈希）。
     这条对应题目原文：
       "Trace 中不需要也不应展示完整的模型内部思维链，
        只需要展示可审计的关键决策摘要和执行步骤。"

  D7【tool_iteration_requests · 供应商迭代反馈通道】
     Runner 在两种场景下会主动记账：
       (a) LLM 请求了 Skill.allowed_tools 之外的 Tool（unauthorized）
       (b) 已授权 Tool 调用时抛异常（execution_error）
     这些记录会注入到最终 answer 的 tool_iteration_requests 字段，
     同时强制把 needs_human_review 设为 True。
     好处：
       - 供应商可以离线扫所有 answer，把高频出现的 unauthorized/execution_error
         汇总成"Tool 迭代需求队列"
       - 单条线索的销售看到 needs_human_review=True + tool_iteration_requests
         非空，能立刻知道"AI 不是判断不出来，是缺工具/工具坏了"
     字段名不出现在 prompt 里、LLM 也不感知，Runner 是唯一作者。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import get_settings
from .llm_client import LLMClient
from .skill_loader import Skill
from .tools.registry import ToolRegistry
from .trace import TraceCollector, Timer
from . import persistence

logger = logging.getLogger(__name__)

# 项目根目录与业务资产目录。把路径常量定义在模块级，避免每次 run() 都重新计算。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


# =============================================================================
# Prompt 模板与片段
# =============================================================================
#
# 把 system prompt 拆成模板 + 动态片段，原因：
#   1. 业务资产（catalog/sop/forbidden）可能随时改，但 prompt 结构稳定
#   2. 模板里每个 # 段落都对应一种"知识来源"，方便评审追溯
#   3. 想做 prompt 版本管理时，只需替换这个常量（配合 Skill.version 一起 bump）
#
# 注意末尾那句 "含字符串 JSON 是必需的" —— DeepSeek 的 json_object 模式要求
# messages 里至少出现一次 "JSON" 字样，否则会拒绝请求（这是 DeepSeek 的硬约束）。

_SYSTEM_PROMPT_TEMPLATE = """{skill_instructions}

# 业务资产 · 产品目录概览（{catalog_summary_kind}）
以下是你可调用 query_product 之前就已知的产品/案例概览。具体细节必须用 query_product 查询后再引用。

{catalog_summary}

# 业务资产 · 销售 SOP（全文 · 引用时使用 SOP-* id）

{sales_sop}

# 业务资产 · 禁止承诺事项（全文 · 触发时使用 FORBIDDEN-* id）

{forbidden_claims}

# 销售自定义方法论 Playbook

{custom_playbooks_section}

# 可用工具

{tools_section}

# 重要：输出契约

- 每一轮你都必须返回 **合法 JSON**（response_format=json_object）。
- 字段约定见 SKILL.md。不要在 JSON 之外输出任何文字。
- 含字符串 "JSON" 是必需的（DeepSeek 要求）。"""


# 达到 max_turns 上限时注入的"最后一推"。
# 不在 system_prompt 里就声明"如果到了第 N 轮你必须 ..."，因为：
#   (a) 即使提前声明，LLM 也可能"忘记"
#   (b) 单独追加一条 system 消息让 Trace 能看到"这条规则是在第 N+1 轮才生效"
#   (c) 调用层可以拓展成"超轮后切换更小的 prompt"以省 token
_FINAL_TURN_NUDGE = (
    "已达到工具调用轮数上限。本轮你**必须**输出 need_tool=false 并给出完整 answer。"
    "禁止再请求调用任何 Tool。仍需引用 source_id（必须返回 JSON）。"
)


def _build_catalog_summary(catalog_path: Path) -> tuple[str, str]:
    """生成给 LLM 的产品目录"概览"（注意：不展开 feature 详情）。

    为什么只放概览而不全文：
      - 全文塞进 system 也行（量小），但那样 LLM 就没必要调 query_product 了，
        Trace 里就看不到 Act/Observe 这两步——而题目明确要求展示 ReAct 完整链路。
      - 概览只暴露 source_id + 一句话定位，足够 LLM 决定"要不要查"和"查什么"。

    返回 (summary_text, summary_kind_label)：
      - summary_text 用于填模板
      - summary_kind_label 用于填模板的标题描述（如"含 3 产品 + 3 案例"）
    """
    with catalog_path.open(encoding="utf-8") as fp:
        cat = json.load(fp)
    lines = []
    # 产品行：source_id | name — tagline（解决的问题列表）
    for p in cat.get("products", []):
        lines.append(
            f"- {p['source_id']} | {p['name']} — {p.get('tagline', '')}（解决：{'/'.join(p.get('solves_problems', []))}）"
        )
    # 案例行：source_id | 案例 · 行业 · 脱敏名
    for c in cat.get("reference_cases", []):
        lines.append(
            f"- {c['source_id']} | 案例 · {c.get('industry', '')} · {c.get('anonymized_name', '')}"
        )
    return "\n".join(lines), f"含 {len(cat.get('products', []))} 产品 + {len(cat.get('reference_cases', []))} 案例"


def _build_tools_section(tools: ToolRegistry) -> str:
    """把 Tool 注册表渲染成给 LLM 看的 Markdown 描述。

    每个 Tool 输出：名字 + 用途 + JSON schema。
    LLM 看到 schema 后会按 schema 出参数（即便没用模型原生 function-calling，
    显式给 schema 仍能显著提升参数准确率）。
    """
    specs = tools.describe_all()
    if not specs:
        return "（暂无可用 Tool）"
    lines = []
    for s in specs:
        lines.append(
            f"## {s['name']}\n"
            f"用途：{s['description']}\n"
            f"参数 schema：{json.dumps(s['parameters_schema'], ensure_ascii=False)}"
        )
    return "\n\n".join(lines)


# =============================================================================
# 兜底 answer
# =============================================================================
#
# 当 LLM 调用 / JSON 解析 / answer 字段任何一环出错时，Runner 不能让前端拿到空响应，
# 也不能让 AI 临时编造内容（违反"防 AI 编造"原则）。
# 这个 fallback 给出的是一份"安全保守"的 answer：
#   - lead_tier = "C"：默认低意向（不会让销售错失高价值线索时白白接管）
#   - needs_human_review = True：明确要求人工介入
#   - draft_reply 用最通用的礼貌话术，不引用任何产品/价格/案例
#   - evidence 只挂一条 RUNNER-FALLBACK 标识，便于审计追溯
#
# 注意：这里的 source_id "RUNNER-FALLBACK" 不在三大业务资产里，
# 是 Runner 自创的"系统级"id，意图就是让审计能一眼区分"这是兜底而非真业务"。

def _safe_fallback_answer(reason: str) -> dict:
    return {
        "lead_tier": "C",
        "intent_level": "未判定",
        "pain_points": [],
        "missing_info": ["AI 自动分析失败，需人工补全所有字段"],
        "risks": [f"自动分析未完成：{reason}"],
        "recommended_product": None,
        "next_actions": ["人工接手该线索，按 SOP-LEAD-TIER 重新评估"],
        "draft_reply": "您好，您的咨询我们已经收到，稍后销售同事会与您联系，确认具体需求。",
        "needs_human_review": True,
        "triggered_rules": [],
        "evidence": [{"claim": "AI 流程兜底", "source_id": "RUNNER-FALLBACK"}],
    }


# =============================================================================
# Runner
# =============================================================================

@dataclass
class RunnerResult:
    """Runner.run() 的返回值。

    answer: 给前端展示的最终结构化结果（无论正常 / 兜底，都保证结构稳定）
    finished:
      - True  → LLM 正常输出了 answer（含被强制总结的情况）
      - False → 走了兜底分支，前端可据此提醒用户"AI 未完成自动分析"
    """
    answer: dict
    finished: bool


class ReActRunner:
    """ReAct 多轮 Runner：Reasoning Summary → Act → Observe → Answer

    依赖通过构造函数注入（DI），方便测试时塞 mock：
      llm   ：LLMClient 实例（生产是 DeepSeek 实例，测试是 MagicMock）
      skill ：当前激活的 Skill（决定 instructions + allowed_tools）
      tools ：Tool 注册表（query_product 等）
      trace ：每个 run() 用一个新的 TraceCollector，把所有步骤记下来
      max_turns：超过该轮数即触发强制总结，缺省读 .env 的 REACT_MAX_TURNS
    """

    def __init__(
        self,
        llm: LLMClient,
        skill: Skill,
        tools: ToolRegistry,
        trace: TraceCollector,
        max_turns: Optional[int] = None,
    ) -> None:
        self.llm = llm
        self.skill = skill
        self.tools = tools
        self.trace = trace
        # 显式传 max_turns 优先（测试场景），否则从配置读
        self.max_turns = max_turns if max_turns is not None else get_settings().react_max_turns

        # D7【tool_iteration_requests】：本次 run() 中累计的"工具迭代请求"。
        # 每个元素 = {tool_name, tool_args, turn, reason, detail}
        # 进入这里的两种来源：
        #   - reason="unauthorized"   : LLM 调用了 Skill 白名单之外的 Tool
        #   - reason="execution_error": 已授权 Tool 真实运行时抛异常
        # 在 _finalize_answer() 中注入到 answer 并强制 needs_human_review=True。
        self._tool_iteration_requests: list[dict] = []

        # 每次 _build_system_prompt 都会刷新这个值（自定义 playbook 加载数），
        # 供 init trace 的 context_summary 引用。
        self._last_playbooks_loaded: int = 0

    # -------------------------------------------------------------------------
    # 内部：tool_iteration_requests 记账 & answer 收尾
    # -------------------------------------------------------------------------

    def _record_tool_iteration_request(
        self,
        *,
        tool_name: str,
        tool_args: dict,
        turn: int,
        reason: str,
        detail: str,
    ) -> None:
        """记一条供应商可消费的"Tool 迭代需求"。

        Runner 是唯一作者（LLM 不感知此字段名），保证：
          - 记录的客观性：要么是权限层拒绝（unauthorized），要么是 try/except 兜住的异常
          - 数据可聚合：下游可以离线扫一段时间内所有 answer，按 reason + tool_name 直方图
        """
        self._tool_iteration_requests.append({
            "tool_name": tool_name,
            "tool_args": tool_args,
            "turn": turn,
            "reason": reason,         # unauthorized | execution_error
            "detail": detail,
        })

    def _finalize_answer(self, answer: dict, *, finished: bool) -> "RunnerResult":
        """统一收尾：注入 tool_iteration_requests，必要时改 needs_human_review。

        所有 return RunnerResult(...) 都必须通过这里，否则就漏字段。
        """
        # 注入字段（即便是空列表也保留，让下游消费方写代码时不必判空）
        answer = dict(answer)  # 避免修改调用方传入的引用
        answer["tool_iteration_requests"] = list(self._tool_iteration_requests)

        # 任何拒绝/失败都强制走人工审核
        if self._tool_iteration_requests:
            answer["needs_human_review"] = True
            # 把 RUNNER-* 类型的元信息也写进 evidence，让审计能溯源
            evidence = list(answer.get("evidence") or [])
            evidence.append({
                "claim": (
                    f"Runner 在本次执行中记录了 "
                    f"{len(self._tool_iteration_requests)} 条工具迭代请求"
                ),
                "source_id": "RUNNER-TOOL-ITERATION",
            })
            answer["evidence"] = evidence

        return RunnerResult(answer, finished=finished)

    # -------------------------------------------------------------------------
    # prompt 构造
    # -------------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """每次 run() 重新构造一次。

        没做缓存，原因：
          - 业务资产文件可能在调试过程中被修改，不缓存方便热更
          - 量小（< 5KB），每次读盘代价可忽略
          - 真要做缓存，应该用 mtime 校验，复杂度不值得

        返回值通过 self._last_playbooks_loaded 副作用记下加载了几份自定义 playbook，
        以便 init trace 的 context_summary 暴露给审计。
        """
        # 业务资产目录走 persistence.get_data_dir() 而非常量，
        # 让测试可以 monkeypatch 整套 data 目录（包括 product_catalog/sales_sop/playbook）
        data_dir = persistence.get_data_dir()
        catalog_summary, summary_kind = _build_catalog_summary(data_dir / "product_catalog.json")
        sales_sop = (data_dir / "sales_sop.md").read_text(encoding="utf-8")
        forbidden_claims = (data_dir / "forbidden_claims.md").read_text(encoding="utf-8")
        tools_section = _build_tools_section(self.tools)
        custom_section, n_playbooks = self._build_custom_playbooks_section()
        # 副作用：让 run() 写 init trace 时能拿到这个计数
        self._last_playbooks_loaded = n_playbooks
        return _SYSTEM_PROMPT_TEMPLATE.format(
            skill_instructions=self.skill.instructions,
            catalog_summary_kind=summary_kind,
            catalog_summary=catalog_summary,
            sales_sop=sales_sop,
            forbidden_claims=forbidden_claims,
            custom_playbooks_section=custom_section,
            tools_section=tools_section,
        )

    def _build_custom_playbooks_section(self) -> tuple[str, int]:
        """扫 data/custom_playbooks/*.md（跳过 _example.md），渲染成 prompt 片段。

        返回 (text, n_playbooks)。空时返回固定占位（不要让 prompt 出现空段标题）。
        单个 playbook 加载失败已经在 persistence.iter_active_playbooks 内部被
        logger.warning 兜住，不会炸 Runner。
        """
        chunks: list[str] = []
        for _name, title, body in persistence.iter_active_playbooks():
            chunks.append(f"### {title}\n\n{body}\n")
        if not chunks:
            return "（暂无自定义方法论）", 0
        return "\n".join(chunks), len(chunks)

    def _build_user_prompt(self, lead_text: str) -> str:
        """把客户线索原文用清晰边界包起来。

        用 === 三等号包裹是为了：
          - 帮 LLM 区分"提示词指令"和"用户输入"，防止 prompt injection 误伤
          - 评审看 Trace 时也能一眼分清"哪段是评估对象"
        """
        return (
            "请评估以下营销线索并按 SKILL.md 的输出格式返回 JSON。\n\n"
            "=== 线索原文 ===\n"
            f"{lead_text}\n"
            "=== 结束 ===\n"
        )

    # -------------------------------------------------------------------------
    # 主循环
    # -------------------------------------------------------------------------

    def run(self, lead_text: str) -> RunnerResult:
        """主入口：执行一次完整的 ReAct 循环，返回结构化 answer + Trace。

        循环不变量（loop invariant）：
          - messages 始终保持 OpenAI 风格的 [{role, content}, ...] 结构
          - 每经过一轮 LLM 调用，messages 会增加 1~3 条（assistant + 可选的 user observe）
          - trace 单调递增，永不回退
          - turn 从 1 开始数；turn > max_turns 即触发 forced_final
        """
        # 1) 构造初始消息：system + user
        #    _build_system_prompt 会顺便把"加载了几份 playbook"写到 self._last_playbooks_loaded
        self._last_playbooks_loaded = 0
        system_prompt = self._build_system_prompt()
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._build_user_prompt(lead_text)},
        ]

        # 2) Trace 第一条：init 步骤。记录关键元信息让评审能复现。
        #    注意：不把 system_prompt 全文写进 Trace（量大且敏感），只写长度+版本号
        self.trace.add(
            "init",
            skill_name=self.skill.name,
            prompt_version=self.skill.prompt_version,
            context_summary=(
                f"system prompt {len(system_prompt)} chars · "
                f"skill={self.skill.name}@{self.skill.version} · "
                f"max_turns={self.max_turns} · "
                f"playbooks_loaded={self._last_playbooks_loaded}"
            ),
        )

        turn = 0
        forced_final = False  # True 表示已经在执行"强制总结轮"

        while True:
            turn += 1
            is_over_limit = turn > self.max_turns

            # ---- 超轮守卫 ----
            # 注意触发条件：is_over_limit AND 还没强制过。
            # 只触发一次，避免重复 append 同一条 nudge。
            if is_over_limit and not forced_final:
                messages.append({"role": "system", "content": _FINAL_TURN_NUDGE})
                self.trace.add(
                    "max_turns_guard",
                    context_summary=f"已达 {self.max_turns} 轮上限，强制进入总结轮",
                )
                forced_final = True

            # ---- 调 LLM（含同轮重试 1 次）----
            llm_result, llm_error = self._call_llm_with_retry(messages, turn)
            if llm_error is not None:
                # 网络/SDK 双重重试后仍失败 → 兜底，结束循环
                self.trace.add("error", error=f"LLM 调用最终失败：{llm_error}")
                return self._finalize_answer(
                    _safe_fallback_answer(f"LLM 调用失败 - {llm_error}"),
                    finished=False,
                )

            # ---- JSON 解析（失败时给 LLM 一次同轮修复机会）----
            try:
                payload = llm_result.as_json()
            except (ValueError, json.JSONDecodeError) as e:
                # 第一次解析失败：把错的 assistant 内容塞回，再发一条 user 提示
                # 让 LLM 输出合法 JSON。这里 allow_retry=False 避免叠加重试。
                self.trace.add(
                    "reasoning",
                    output_summary="LLM 返回内容不是合法 JSON，触发同轮 JSON 修复",
                    latency_ms=llm_result.latency_ms,
                    error=str(e),
                )
                messages.append({"role": "assistant", "content": llm_result.content})
                messages.append({
                    "role": "user",
                    "content": "上一条回复不是合法 JSON。请严格按 SKILL.md 的输出格式只返回 JSON 对象（不要包代码块）。",
                })
                llm_result, llm_error = self._call_llm_with_retry(
                    messages, turn, allow_retry=False,
                )
                if llm_error is not None:
                    self.trace.add("error", error=f"JSON 修复轮失败：{llm_error}")
                    return self._finalize_answer(
                        _safe_fallback_answer("JSON 修复失败"), finished=False,
                    )
                try:
                    payload = llm_result.as_json()
                except (ValueError, json.JSONDecodeError) as e2:
                    # 修复两轮都拿不到合法 JSON → 兜底
                    self.trace.add("error", error=f"JSON 修复后仍非法：{e2}")
                    return self._finalize_answer(
                        _safe_fallback_answer("JSON 解析失败"), finished=False,
                    )

            # ---- 解析成功 → 把这轮 assistant 内容加入历史 ----
            # 必须 append，否则下一轮 LLM 看不到自己之前说过什么，可能反复要求调 Tool
            messages.append({"role": "assistant", "content": llm_result.content})

            # ---- 抽取 LLM 这轮的决策 ----
            # reasoning_summary：≤80 字（SKILL.md 已约束），这里再截 200 字双保险，
            # 防止 LLM 不守约定时 Trace 被撑爆
            reasoning_summary = str(payload.get("reasoning_summary", ""))[:200]
            route = payload.get("route_decision") or {}
            need_tool = bool(route.get("need_tool", False))

            # 写 reasoning trace（每轮都写一条，无论 need_tool 真假）
            self.trace.add(
                "reasoning",
                output_summary=reasoning_summary,
                latency_ms=llm_result.latency_ms,
                context_summary=f"turn={turn} · need_tool={need_tool}",
            )

            # ---- 终止分支 ----
            # 进入终止的两种情况：
            #   1) LLM 自主声明 need_tool=false
            #   2) 已经在强制总结轮（forced_final），即便 LLM 还想调 Tool 也不再让它调
            if not need_tool or forced_final:
                answer = payload.get("answer")
                if not isinstance(answer, dict):
                    # 声明结束但没给 answer → 这是 LLM 违约，走兜底
                    self.trace.add(
                        "error",
                        error="LLM 声明 need_tool=false 但未给出 answer 字段",
                    )
                    return self._finalize_answer(
                        _safe_fallback_answer("answer 缺失"), finished=False,
                    )

                # 正常出 answer：Trace 写 answer 步骤，简要摘几个关键字段进 output_summary
                # 让评审一眼看到 lead_tier / 是否需要人工 / 触发了哪些 forbidden 规则
                self.trace.add(
                    "answer",
                    output_summary=(
                        f"lead_tier={answer.get('lead_tier')} · "
                        f"needs_human_review={answer.get('needs_human_review')} · "
                        f"triggered_rules={answer.get('triggered_rules', [])}"
                    ),
                )
                return self._finalize_answer(answer, finished=True)

            # ---- 继续 Act：LLM 要求调用某个 Tool ----
            tool_name = route.get("tool_name", "")
            tool_args = route.get("tool_args", {}) or {}

            # 权限检查：Skill 的 allowed_tools 是白名单。
            # 为什么不让 LLM 任意调任何已注册 Tool？
            #   - 真实业务里 Skill 是"角色"，不同角色可见的能力不同
            #   - 评审追问"为什么需要 Skill"时，这个机制就是答案：Skill 是权限边界
            if not self.skill.is_tool_allowed(tool_name):
                err = f"Skill '{self.skill.name}' 未授权调用 Tool '{tool_name}'"
                self.trace.add(
                    "act",
                    tool_name=tool_name,
                    tool_input=tool_args,
                    error=err,
                )
                # D7：记账为"工具迭代请求"。下游可据此判断是不是要给该 Skill
                # 加新 Tool 或者放开权限。
                self._record_tool_iteration_request(
                    tool_name=tool_name,
                    tool_args=tool_args,
                    turn=turn,
                    reason="unauthorized",
                    detail=err,
                )
                # 把拒绝原因塞回去，让 LLM 改走 answer 路径（而不是死循环）
                messages.append({
                    "role": "user",
                    "content": (
                        f"工具调用被拒绝：{err}。"
                        "请直接给出 answer（need_tool=false）并返回 JSON。"
                    ),
                })
                continue  # 进入下一轮，LLM 看到拒绝原因后通常会改路

            # ---- 真正调用 Tool ----
            # 用 Timer 上下文管理器测耗时，方便 Trace 体现 Tool 性能
            with Timer() as t:
                try:
                    tool_output = self.tools.call(tool_name, tool_args)
                    tool_error = None
                except Exception as e:  # pylint: disable=broad-except
                    # 主动 catch 所有异常：Tool 是用户提供的代码，
                    # 不能让一个 Tool bug 直接把整个 Runner 拉崩
                    tool_output = None
                    tool_error = str(e)
                    logger.warning("Tool %s 调用失败: %s", tool_name, e)

            # 写 act trace：注意 tool_output 只有在成功时才放，错误时放 None
            self.trace.add(
                "act",
                tool_name=tool_name,
                tool_input=tool_args,
                tool_output=tool_output if tool_error is None else None,
                latency_ms=t.ms,
                error=tool_error,
            )

            # D7：Tool 真实运行抛异常 → 记账为"工具迭代请求"（execution_error）。
            # 与 unauthorized 区分，下游可以分流到"修 bug"vs"加能力"两个不同的处理队列。
            if tool_error is not None:
                self._record_tool_iteration_request(
                    tool_name=tool_name,
                    tool_args=tool_args,
                    turn=turn,
                    reason="execution_error",
                    detail=tool_error,
                )

            # ---- Observe：把 Tool 结果作为新一条 user 消息喂回 LLM ----
            # 包装成 {"ok": bool, "data" | "error": ...}，让 LLM 明确感知调用结果
            observe_payload = (
                {"ok": True, "data": tool_output}
                if tool_error is None
                else {"ok": False, "error": tool_error}
            )
            # Trace 里只写 observe 摘要（命中条数），不写完整 tool_output——
            # 因为完整结果已经在 act 步骤里展示过，避免冗余撑爆 Trace
            self.trace.add(
                "observe",
                tool_name=tool_name,
                output_summary=(
                    f"hits={len(tool_output.get('hits', []))}/"
                    f"{tool_output.get('total_matches', 0)}"
                    if tool_error is None and isinstance(tool_output, dict)
                    else f"tool error: {tool_error}"
                ),
            )
            messages.append({
                "role": "user",
                "content": (
                    f"工具 {tool_name} 返回结果（JSON）：\n"
                    f"{json.dumps(observe_payload, ensure_ascii=False)}\n\n"
                    "请基于此结果继续。若信息已足够请输出 need_tool=false 并给出 answer。"
                ),
            })

            # 隐式 continue：进入 while 顶部下一轮
            # 下一轮开始时会先判断是否超限 → 是则进强制总结分支

    # -------------------------------------------------------------------------
    # helpers
    # -------------------------------------------------------------------------

    def _call_llm_with_retry(
        self,
        messages: list[dict],
        turn: int,
        allow_retry: bool = True,
    ) -> tuple[object, Optional[str]]:
        """带"业务层"重试一次的 LLM 调用。

        注意这里和 LLMClient 内部的 SDK retry 是叠加的：
          - SDK 层（openai.OpenAI(max_retries=...)）：处理 HTTP 408/5xx/连接错误等
          - 这里这一层：处理"网络层重试也没救回来"的最后一次机会，
                       例如鉴权失败/限流持续/API key 错误等

        参数:
          allow_retry: False 时只调一次，给 JSON 修复轮用——避免重试嵌套放大延时

        返回:
          (ChatResult, None) on success
          (None, error_str) on failure
        """
        try:
            return self.llm.chat(messages, json_mode=True), None
        except RuntimeError as e:
            if not allow_retry:
                return None, str(e)
            # 第一次失败：记 warn trace（不算 error，错误程度更轻），然后重试
            logger.info("LLM turn=%d 首次失败，重试一次：%s", turn, e)
            self.trace.add("warn", error=f"LLM 首次失败将重试：{e}")
            try:
                return self.llm.chat(messages, json_mode=True), None
            except RuntimeError as e2:
                return None, str(e2)
