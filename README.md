# AI Growth Copilot · 营销线索运营数字员工 Demo

一个可本地运行的小型 Copilot：接收一条潜在客户线索 → 经 ReAct 多轮推理 → 输出**带证据**的线索质量判断、风险点、跟进建议 + **可审计执行 Trace**。

> 📍 设计理由、关键判断、ReAct 各阶段如何体现 等内容请见 [`solution.md`](./solution.md)。
> 📍 所有值得讲解的设计点速查请见 [`design_points.md`](./design_points.md)。

---

## 🚀 快速开始

```bash
# 1) 配置 DeepSeek API Key
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY=sk-xxxxxxxx
# 申请地址：https://platform.deepseek.com/

# 2) 安装依赖
pip install -r requirements.txt

# 3) 启动
./run.sh
# 或：uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

# 4) 打开浏览器
# http://127.0.0.1:8000
```

启动后顶栏右侧应显示 `后端就绪 · lead-scoring-followup@0.1.0` 的绿色徽章。

---

## 🧪 跑测试

47 个单元测试，**不需要真实 API Key**（LLM 调用全部 mock）：

```bash
DEEPSEEK_API_KEY=sk-test-fake python3 -m pytest -v
```

预期输出：`47 passed`。

测试覆盖：

- `test_llm_client.py` (5) — DeepSeek SDK 封装、retry、JSON 模式、错误兜底
- `test_skill_loader.py` (9) — agentskills.io 规范、frontmatter 校验、权限白名单
- `test_query_product.py` (10) — 关键词命中、案例命中、source_id 透传、未命中安全回复
- `test_runner_smoke.py` (10) — ReAct 主循环、超轮收敛、JSON 修复、Tool 权限、`tool_iteration_requests`、自定义 playbook 加载
- `test_api.py` (13) — FastAPI 路由、输入校验、analysis_log / feedback 双轨 ID join（precise/fuzzy/orphan）、playbook CRUD 与路径越界拒绝

---

## 🧱 目录结构

```
ai-growth-copilot/
├── README.md                            本文件
├── solution.md                          设计理由 / AI 使用方式 / 风险控制 / 迭代计划
├── design_points.md                     18 个设计点 + 面试讲解手册
├── .env.example                         配置模板（DEEPSEEK_API_KEY 等）
├── requirements.txt
├── pytest.ini
├── run.sh                               一键启动脚本
│
├── backend/
│   ├── main.py                          FastAPI 入口 + 路由
│   ├── config.py                        .env 读取
│   ├── schemas.py                       Pydantic 数据模型（含 TraceStep、external_id / analysis_id 双轨 ID）
│   ├── persistence.py                   jsonl 持久化 + playbook 文件名安全校验
│   ├── llm_client.py                    DeepSeek 封装（OpenAI 兼容）
│   ├── skill_loader.py                  agentskills.io 规范的 Skill 加载器
│   ├── trace.py                         Trace 收集器 + Timer 工具
│   ├── runner.py                        ⭐ ReAct Agent Runner 核心
│   └── tools/
│       ├── registry.py                  Tool 注册表
│       └── query_product.py             query_product 实现
│
├── skills/
│   └── lead-scoring-followup/
│       └── SKILL.md                     Skill 定义（agentskills.io 规范）
│
├── data/                                Mock 业务资产
│   ├── product_catalog.json             3 产品 / 19 feature / 3 案例
│   ├── sales_sop.md                     销售跟进 SOP（含 SOP-* id）
│   ├── forbidden_claims.md              禁止承诺事项（含 FORBIDDEN-* id）
│   ├── leads.json                       26 条示例线索（含 4 个越狱测试）
│   └── custom_playbooks/                销售自助上传的方法论（扩展功能 PUT /api/playbooks）
│
├── frontend/                            单页 HTML/JS/CSS（无构建步骤）
│   ├── index.html
│   ├── app.js
│   └── style.css
│
└── tests/                               47 个 pytest 单测（不依赖真实 API Key）
    ├── test_api.py
    ├── test_llm_client.py
    ├── test_skill_loader.py
    ├── test_query_product.py
    └── test_runner_smoke.py
```

---

## 🔌 API

**核心**

| Method | Path | 用途 |
|---|---|---|
| GET | `/api/health` | 健康检查，返回 Skill 版本 / 已注册 Tool / max_turns |
| GET | `/api/leads` | 返回 `data/leads.json` 供前端示例下拉 |
| POST | `/api/analyze` | 核心：`{lead_text, external_id?}` → `{ok, result, trace, external_id, analysis_id}` |
| GET | `/` | 前端单页 |

**扩展（见 `solution.md` Q11）**

| Method | Path | 用途 |
|---|---|---|
| GET | `/api/analyses` | 最近 N 条分析日志（可按 `external_id` 过滤） |
| POST | `/api/feedback` | 销售提交实际成交结果（至少要带 `analysis_id` 或 `external_id`） |
| GET | `/api/analytics/feedback` | 聚合：混淆矩阵 + 打脸案例 + `match_kind_breakdown`（precise/fuzzy/orphan） |
| GET / PUT / DELETE | `/api/playbooks[/{name}]` | 业务人员自助维护自定义方法论 playbook（含路径越界保护） |

### ID 体系（L2 双轨）

- **`external_id`**（客户级，可选）：对外业务系统的客户引用，可以是 CRM 客户 ID / 微信 openid / 邮箱 hash / 表单 submission ID 等任意外部 ID。
- **`analysis_id`**（本次分析的精准 ID，服务端 UUID 必产）：`/api/analyze` 每次返回一个新的 uuid4 hex。feedback 提交时优先用它做 1:1 精准 join。

设计理由见 `design_points.md` D16 / D17。

### POST `/api/analyze`

请求体：

```json
{
  "lead_text": "客户原文 / 留言 / 销售备注...",
  "external_id": "crm-CUST-12345"
}
```

响应：

```json
{
  "ok": true,
  "external_id": "crm-CUST-12345",
  "analysis_id": "b05877f6104740cb86e661beaed7fbe3",
  "result": {
    "lead_tier": "A",
    "intent_level": "高",
    "pain_points": [...],
    "missing_info": [...],
    "risks": [...],
    "recommended_product": "PROD-CS-AGENT",
    "next_actions": [...],
    "draft_reply": "...",
    "needs_human_review": false,
    "triggered_rules": [],
    "evidence": [{"claim": "...", "source_id": "PROD-CS-AGENT"}],
    "tool_iteration_requests": []
  },
  "trace": [
    {"step_id": 1, "step_type": "init", "skill_name": "...", ...},
    {"step_id": 2, "step_type": "reasoning", "output_summary": "...", ...},
    {"step_id": 3, "step_type": "act", "tool_name": "query_product", ...},
    {"step_id": 4, "step_type": "observe", ...},
    {"step_id": 5, "step_type": "reasoning", ...},
    {"step_id": 6, "step_type": "answer", ...}
  ]
}
```

### POST `/api/feedback`

请求体（`analysis_id` / `external_id` 至少给一个，否则 400）：

```json
{
  "analysis_id": "b05877f6104740cb86e661beaed7fbe3",
  "external_id": "crm-CUST-12345",
  "outcome": "deal",
  "deal_amount": 80000.0,
  "note": "签了 12 个月"
}
```

---

## 📐 ReAct 最小闭环

```
线索上下文输入
  ↓
lead-scoring-followup Skill 激活（注入 instructions + 业务资产到 system prompt）
  ↓
Agent Runner 循环（最多 3 轮）：
  Reasoning Summary（LLM 输出 route_decision JSON）
    → 若 need_tool=true：Act（调 query_product）→ Observe（结果喂回）→ 下一轮
    → 若 need_tool=false：直接 Answer
  超过上限：强制总结轮（system 注入禁止再调 Tool 提示）
  ↓
LLM 生成结构化 answer（含 evidence + source_id）
  ↓
前端展示结果分段 + Trace 时间线（每步可点开看 JSON）
```

详见 [`design_points.md`](./design_points.md) D1-D7。

---

## 🛡️ 防 AI 编造（评审重点）

按**技术层**切分的三层防御（另一视角："按生命周期阶段切"见 [`solution.md` Q6](./solution.md)，两个视角互补）：

1. **Prompt 层**：`SKILL.md` 明确约束"每条事实必须带 source_id"；`forbidden_claims.md` 全文塞入 system，每条 `FORBIDDEN-*` 配安全回复模板
2. **业务资产层**：`product_catalog.json` 故意不放价格；每个 product / feature / case 都有 source_id；客户案例脱敏
3. **Runner 层**：未授权 Tool / Tool 执行失败 → 记 `tool_iteration_requests` + 强制 `needs_human_review=True`

演示推荐：选择 **LEAD-006 / LEAD-015 / LEAD-020 / LEAD-026**（4 个越狱测试）观察 AI 如何拒绝报价、拒绝伪造客户名、拒绝角色扮演、拒绝泄露系统 Prompt。详见 [`design_points.md`](./design_points.md) D9 / D10。

## 🧩 已实现的扩展（见 `solution.md` Q11）

- **`POST /api/feedback` + `GET /api/analytics/feedback`** —— 销售回写实际成交结果，系统聚合出混淆矩阵 + 打脸样本 + `match_kind_breakdown`（precise/fuzzy/orphan 三档反映 attribution 精度），让 Q9 的两个主指标（人工接受率、跟进转化率）可被实测。
- **`PUT /api/playbooks/{name}`** —— 一线业务人员可以把自己的高转化方法论 / 踩坑记录直接上传成 playbook，Runner 在构造 system prompt 时自动加载，不需要工程介入。

---

## ⚙️ 配置项

`.env` 全部可配项：

| 变量 | 默认值 | 含义 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | DeepSeek API Key（必填） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek 入口 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `REACT_MAX_TURNS` | `3` | ReAct 循环上限 |
| `LLM_TIMEOUT_SECONDS` | `30` | 单次 LLM 调用超时 |
| `LLM_MAX_RETRIES` | `2` | SDK 层重试次数 |
| `APP_HOST` | `127.0.0.1` | 服务监听地址 |
| `APP_PORT` | `8000` | 服务监听端口 |

---

## ❓ FAQ

**Q：能不能换成 OpenAI / 通义 / Kimi？**
A：LLMClient 用的是 OpenAI 兼容 SDK，改 `.env` 里的 `DEEPSEEK_BASE_URL` + `DEEPSEEK_MODEL` 即可（前提是目标模型支持 `response_format=json_object`）。

**Q：query_product 用关键词匹配，会不会精度差？**
A：会，特别是中文单字假阳性（如"区块链"的"链"撞到"小程序链接"）。这是 Demo 阶段有意的 trade-off — 用最朴素的算法换 Trace 可审计性。生产时换 BM25 或向量召回，Runner / Skill / Prompt 接口都不变。详见 [`design_points.md`](./design_points.md) D11。

**Q：Trace 为什么不展示完整的 chain-of-thought？**
A：题目原文：*"Trace 中不需要也不应展示完整的模型内部思维链，只需要展示可审计的关键决策摘要和执行步骤。"* 详见 [`design_points.md`](./design_points.md) D6。

**Q：本地没装 Python 3.10 怎么办？**
A：本项目用了 `list[dict]` 这种 PEP 585 语法 + `dataclass` `slots`，需要 Python ≥ 3.10。建议用 pyenv 或 conda 装一个。
