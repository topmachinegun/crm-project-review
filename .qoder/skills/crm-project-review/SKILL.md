---
name: crm-project-review
description: 基于明道云项目管理知识库对 ClawCRM 里的项目记录进行 AI 评审，输出涵盖「阶段判定 / ICP 匹配度 / 风险点 / 下一步动作 / SOP 偏离度」五个维度的结构化评审报告，可选写回 ClawCRM 指定字段。触发词："评估项目"、"项目跟进评审"、"ClawCRM 项目 AI 评审"、"帮我评 X 项目"、"基于知识库评估项目"、"review claw project"。
---

# ClawCRM 项目评审

基于 ClawCRM 项目记录的跟进日志，结合明道云项目管理知识库进行交叉比对，输出结构化评审报告，并可选择把报告写回项目记录。

## 1. 触发时机

强触发词（优先中文）：
- "评估项目 / 评估 X 项目 / 帮我评一下 X 项目"
- "项目跟进评审 / 项目健康度分析"
- "ClawCRM 项目 AI 评审 / 基于知识库评估项目"
- 英文等价："review claw project"、"assess project follow-up"、"CRM project health check with KB"

用户通常会提供**项目名**或 **rowId** 中的一个，两条路径都必须支持。

## 2. 前置依赖（非业务，外链）

本技能**只负责项目评审业务**，不处理授权与底层 MCP 协议：

| 职责 | 承担技能 | 在本 skill 中的假设 |
|---|---|---|
| MCP Token / URL 的生成与刷新 | **`hap-oauth-mcp`** | 通过环境变量 `HAP_MCP_URL` 或参数 `--mcp-url` 消费；本 skill 不生成、不存储、不刷新 |
| MCP 协议调用规范（`tools/list`、`ai_description`、键名 snake/camel 混用、`10001` 报错解读等） | **`hap-app-access` v1.5** | 本 skill 不复述；脚本与 agent 均默认遵守 |
| 知识库检索通用调用 | `learned_skill: Personal MCP 知识库发现+检索全链路调用指南` | 本 skill 只描述业务层的查询策略 |

> 遇到「Token 过期 / 10001 / headers verification」等基础设施类报错，**请切换到对应前置技能处理**，不要在本 skill 里修代码。

## 3. 业务输入（固定）

评审业务层所需的全部坐标（已在 ClawCRM 里调研确认，除非组织变更，一般不用改）：

| 要素 | 值 | 业务含义 |
|---|---|---|
| ClawCRM appId | `49392ae2-6aa0-4d69-b5e7-57d4fe3fc98e` | 应用 ID |
| 项目管理知识库 ID | `69ca75132970faa5ac6ce728` | 销售 SOP 全库，是评审的参照系 |
| 项目工作表 ID | `69ca1fb1d128aadb0c749d49`（名 `项目管理`） | 项目主表 |
| 项目名字段 | `project_name`（isTitle） | **按名搜索必须用 `project_name`**，不是通用 `title/name` |
| 跟进日志工作表 ID | `69ca1fc9d128aadb0c749edf`（名 `日志`） | 关联子表，独立存储 |
| 日志→项目 关联字段 ID | `69ca205fbf12c183aec577f1` | 在日志表里筛日志用，operator 用 `belongsto`（或 `eq`/`in`，均可） |
| 日志正文字段 alias | `content` | 跟进文本 |
| 日志类型字段 alias | `log_type` | 线索跟进日志 / 其他类型 |
| AI评估 写回字段 ID | `69f956419f1956fc0e1867c3` | 类型 Text 多行；评审报告写回目的地 |
| 目标项目识别 | 项目名 或 rowId | 二选一，rowId 优先级更高 |

## 4. 评审流程（业务核心）

假设 MCP 连通性已由 `hap-oauth-mcp` + `hap-app-access` 保证。本 skill 只关心以下业务步骤：

```
- [ ] S1 锁定目标项目记录（按 rowId 直取；或在 项目管理 工作表用 search 按 project_name 模糊匹配，人工确认后再取 rowId）
- [ ] S2 拉取该项目完整跟进日志（到 日志 工作表按 关联项目=<rowId> 过滤，按 ctime desc 排序，分页至拉完）
- [ ] S3 基于日志抽取查询词，检索 项目管理知识库（§6）
- [ ] S4 按 §5 五维评分框架生成评审报告
- [ ] S5 把报告写回 AI评估 字段（§8），如字段异常则仅预览不写回
- [ ] S6 向用户展示报告并确认写回结果
```

### 便捷用法：直接跑脚本

S1–S3 的拉取与检索工作已经封装到 `scripts/review_project.py`。脚本**仅消费** `HAP_MCP_URL`（由 `hap-oauth-mcp` 提供），不做 Token 管理：

```bash
# <SKILL_ROOT> = 本 SKILL.md 所在目录
export HAP_MCP_URL="<由 hap-oauth-mcp 生成的 URL>"

python3 "$SKILL_ROOT/scripts/review_project.py" \
  --project "XYZ有限公司" \
  --topk 8 \
  > /tmp/project_bundle.json
```

或按 rowId 调用：
```bash
python3 "$SKILL_ROOT/scripts/review_project.py" --row-id <ROW_ID>
```

脚本输出 `{project, knowledgeHits, tools}` 结构的 JSON。Agent 拿到 bundle 后，按 §5 评分框架撰写报告即可。若 `HAP_MCP_URL` 未设置或失效，**请先调用 `hap-oauth-mcp`**，本 skill 不处理。

### 便捷用法：把报告写回 AI评估 字段

报告由 agent 生成后，落到本地文件再通过脚本的**写回模式**回传。写回模式**只做写回**，不会再拉日志/检索 KB：

```bash
python3 "$SKILL_ROOT/scripts/review_project.py" \
  --row-id <ROW_ID> \
  --writeback-file <报告的 markdown 文件路径>
```

脚本输出形如：
```json
{
  "ok": true,
  "worksheetId": "69ca1fb1d128aadb0c749d49",
  "rowId": "<ROW_ID>",
  "controlId": "69f956419f1956fc0e1867c3",
  "fieldName": "AI评估",
  "charsWritten": <N>
}
```

脚本在写回前会自动查一次工作表结构校验 `AI评估` 字段的 controlId 仍然存在；若字段被删/改，会返回 `ok:false` + `reason`，不做任何静默写入。写回模式必须传 `--row-id`，不能用 `--project`（避免歧义）。

## 5. 固定评分框架（五个维度）

报告必须按以下顺序覆盖这五个维度，字段严格对齐。

### 5.1 项目阶段（Stage）
- **参照**：映射到知识库销售流程阶段（Sales Playbook —— 初次接触 / 转交伙伴 / 展示 / 辅助选型 / 消除疑虑 / 报价 / 签约 / 交付 / 顺藤摸瓜）。
- **输出**：`{stage, evidence, confidence: 高/中/低}`

### 5.2 ICP 匹配度（ICP Fit）
- **参照**：知识库中的 ICP 标准（员工数 ≥ 50 / 老板重视 / 有具体数字化问题 / IT 部门参与 / 行业契合 / 团队活力 / 品牌标杆）。
- **输出**：`{score: 0-100, matched_criteria: [...], missing_criteria: [...]}`

### 5.3 风险点（Risks）
- **参照**：日志中的停滞信号（长时间无互动 / 决策链不清 / 预算缩减 / 竞品介入 / POC 拖延 / 合同条款争议）。
- **输出**：`{risk, severity: 高/中/低, supporting_log_snippet}` 的列表。

### 5.4 下一步动作（Next Actions）
- **参照**：当前阶段对应的 SOP 动作 + 已识别的风险点。
- **输出**：有序列表 `{action, owner_hint, deadline_hint, kb_reference_chunkId}`。

### 5.5 SOP 偏离度（SOP Deviation）
- **参照**：把日志里实际发生的动作与知识库中该阶段应做清单进行比对。
- **输出**：`{expected_actions: [...], performed_actions: [...], missed: [...], deviation_score: 0-100}`。

每个维度至少引用一个 `knowledgeHits[].chunkId`；若知识库对某个维度没有命中，必须显式注明「知识库未命中，采用通用判断」。

## 6. 知识库检索策略

1. 从日志中抽取：客户行业、最近动作动词、开放问题、阻碍点、数字化信息（金额、人数、时间线）。
2. 每次评审构造 3 条并行查询：
   - `query_stage`  = `"销售阶段 " + 最近动作动词` → 用于 5.1 与 5.4
   - `query_risks`  = 检测到的停滞信号短语拼接 → 用于 5.3
   - `query_icp`    = 行业 + 公司规模描述 → 用于 5.2
3. 每条查询参数：`searchMode=hybrid`、`topK=5~10`、`knowledgeIds=[默认知识库]`、`appId=<ClawCRM>`。
4. 以 `chunkId` 去重；按 score 取全部查询合并后的 Top 10。

## 7. 报告模板

```markdown
# ClawCRM 项目评审：{{项目名}}

- 记录 ID：`{{rowId}}`
- 跟进日志条数：{{N}}
- 最近更新：{{latest_update_time}}
- 评审基准知识库：项目管理知识库（`{{knowledgeId}}`）

## 1. 项目阶段
**判定**：{{stage}}（置信度 {{confidence}}）
**依据**：{{evidence}}
**KB 引用**：chunk `{{chunkId}}` — {{short_quote}}

## 2. ICP 匹配度
**得分**：{{score}}/100
- ✅ 已满足：{{matched_criteria}}
- ⚠️ 待补强：{{missing_criteria}}

## 3. 风险点
| 风险 | 严重度 | 日志证据 |
|---|---|---|
| ... | 高/中/低 | "..." |

## 4. 下一步动作（按优先级）
1. {{action}} — 建议负责人：{{owner_hint}}，建议时限：{{deadline_hint}}（依据 `{{chunkId}}`）
2. ...

## 5. SOP 偏离度
- **应做动作清单**（KB）：{{expected_actions}}
- **已做动作清单**（日志）：{{performed_actions}}
- **遗漏动作**：{{missed}}
- **偏离度**：{{deviation_score}}/100

---
*生成时间：{{ts}}  |  模型引用知识库 {{knowledgeId}}*
```

## 8. 写回策略

本节只描述业务规则，`update_record` 的键名与 `ai_description` 要求参见 `hap-app-access`。

1. 写回目标：项目管理工作表该 rowId 的 `AI评估` 字段（controlId `69f956419f1956fc0e1867c3`，Text 多行）。
2. 写回内容：§7 报告模板渲染后的完整 Markdown，**不得截断**。
3. 写回通道：推荐走脚本 `--writeback-file` 开关（见 §4）；脚本内部遵守 `hap-app-access` 约定（`fields: [{id, value}]`、`ai_description` 必填）。
4. 字段不存在时 → **不要**自动创建；提示用户在 HAP UI 手工新增（多行文本，名称 `AI评估`，alias 可选 `ai_evaluation`），并在对话中保留完整报告。
5. 写回失败 → 原样透出错误，保留完整报告在对话中，**不**吞错、不**重试**静默写回。
6. 预览模式（用户明确表示「只预览」或首次测试）→ 跳过写回（不调 `--writeback-file`），只在对话中展示报告。

## 9. 业务坑位

（仅与评审业务相关；MCP 协议类坑位见 `hap-app-access`。）

| 症状 | 业务根因 | 修复 |
|---|---|---|
| 按项目名搜无命中 | 记录的标题字段是 `project_name`，而不是通用 `title/name` | 匹配时用 `project_name`；或改用 rowId |
| 同公司多条项目记录（如同一客户对应不同联系人 / 不同渠道） | ClawCRM 允许同公司按联系人/渠道多次建档 | 模糊命中多条时向用户确认后再评；在报告中交叉提示另一条记录的状态 |
| 跟进日志 `get_record_details` 返回的只是计数 | 日志是**独立关联子表**，不是主表多值字段 | 改到 日志 工作表用 `关联项目 belongsto <rowId>` 过滤查询 |
| KB 命中跑偏 | 查询词太直白（整句日志喂给 hybrid） | 抽取动作动词 + 行业词 + 停滞信号短语（见 §6） |
| 客户走「社区版申请密钥」路径 | 商业化漏斗断裂风险 | 必须在报告 §3 风险点里显式提示，并在 §4 行动里加入「付费转化路径确认」 |
| `ICP评估` / `ICP等级评估` 字段为空 | 销售侧未做正式 ICP 打分 | 在报告 §5 SOP 偏离度中计入「未做 ICP 打分」，并在 §4 建议补填 |

## 10. 前置技能依赖

- **`hap-oauth-mcp`**（强依赖）—— 负责 MCP Token 生成与刷新。本 skill 只消费 `HAP_MCP_URL`。
- **`hap-app-access` v1.5**（强依赖）—— 负责 MCP 协议调用规范（`tools/list` 铁律、`ai_description`、键名约定、`10001` 解读等）。
- `learned_skill: Personal MCP 知识库发现+检索全链路调用指南` —— 知识库调用速查。

## 11. 脚本

随 skill 附带的 `scripts/review_project.py` 自动完成 S1–S7，返回 agent 可直接消费的 JSON 文档。如果 schema 有变动再去读源码；不要把脚本逻辑复制到对话里——**直接调用脚本**。
