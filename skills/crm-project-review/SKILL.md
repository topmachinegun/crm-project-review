---
name: crm_project_review
description: 基于明道云项目管理知识库，对 ClawCRM 项目记录进行结构化评审，涵盖项目阶段、ICP 匹配度、风险点、下一步动作和 SOP 偏离度五个维度，评审报告可选写回 ClawCRM 字段。当用户说"评估项目"、"项目跟进评审"、"ClawCRM 项目 AI 评审"、"帮我评 X 项目"、"review claw project"或需要基于明道云知识库做 CRM 项目健康度检查时触发。
---

> **本技能为 L3 参考示例**，保留在 hap-skill-claw-lite monorepo 中供开发新 L3 技能时参考。
> 分发版在独立 GitHub repo：[`crm-project-review`](https://github.com/topmachinegun/crm-project-review)。
> 平台（如 OpenClaw）应引用独立 repo，而非本仓库中的此副本。

# ClawCRM 项目评审

基于跟进日志，对照明道云项目管理知识库对 ClawCRM 项目进行多维度评审，生成结构化评审报告，并可选写回项目记录。

## 1. 触发条件

触发短语（中文优先）：
- "评估项目 / 评估 X 项目 / 帮我评一下 X 项目"
- "项目跟进评审 / 项目健康度分析"
- "ClawCRM 项目 AI 评审 / 基于知识库评估项目"
- 英文等价表述："review claw project"、"assess project follow-up"、"CRM project health check with KB"

用户通常提供**项目名**或 **rowId** 之一，两条路径均需支持。

## 2. 前置条件

| 项目 | 默认值 | 缺失时如何获取 |
|---|---|---|
| Token | 由外部进程管理刷新。本 skill 通过 `token_reader.get_mcp_url("claw-crm")` 读取 token 文件。 | token 文件不存在或过期时联系管理员刷新 |
| ClawCRM appId | `49392ae2-6aa0-4d69-b5e7-57d4fe3fc98e` | 无（硬编码默认值） |
| 知识库 ID | `69ca75132970faa5ac6ce728`（"项目管理知识库"） | 调用 `get_app_knowledge_list(appId)` 重新选择 |
| 项目工作表 | `69ca1fb1d128aadb0c749d49`（项目管理） | 固定锚点；如被 org 改名，`get_app_worksheets_list` 里选 name 含「项目管理」的那张，**不得**选「日报管理」「沟通」等别的表 |
| 跟进日志来源 | 两种合法形态之一：**(a)** 项目管理工作表里 `controlName` 含「日志」的字段；(**b)** 同 app 下独立工作表「项目日志」(默认 id `69ca1fc9d128aadb0c749edf`)，通过 `project[].sid == 主项目 rowId` 关联。**两者任一命中即可**，均为合法唯一数据源。 | 详见 §3.1 数据源纪律 |
| 写回字段 | 默认 `ai_evaluation`（别名 `AI评估`）；执行完成后自动勾选「是否需要AI评估」并更新「最后评估时间」 | 如不存在，提示用户；未经用户同意**不得**自动创建 |

### 三层架构中的位置

本 skill 位于 L3（业务技能层），依赖关系：
- **L1 Token Broker**：提供 Token（运行时由外部 Broker 进程管理）
- **L2 hap-app-access**：提供访问方法论 + 共享 Python 模块（`skills/hap-app-access/`）
  - `token_reader.py`：读取 token 文件
  - `mcp_client.py`：MCP JSON-RPC 客户端

> 分发：本文件为参考示例。平台使用的分发版在独立 repo `crm-project-review`。
> 开发新 L3 技能请参考 [L3 开发规范](../../docs/l3-development.md)。

## 3. 铁律（继承 hap-app-access §4.1）

**每次会话开始时必须先调用 `tools/list`，之后的每一次 `tools/call` 都必须严格遵循返回的 `inputSchema`。** 此 Personal MCP 的已知陷阱：

- 同一工具混用 snake_case 和 camelCase：`update_record` 在一次调用中同时需要 `worksheet_id`、`row_id`、`fields` **和** `appId`。
- `get_app_list` 用 `org_id`（snake_case）；`get_app_knowledge_list` / `knowledge_search` 用 `appId`（camelCase）。
- **所有涉及记录的工具都必须传 `ai_description` 字符串**。缺少则返回 `10001 Http Headers verification failed`。
- 错误码 `10001` 几乎总是意味着**参数名写错或缺少 `ai_description`**，而非 token/header 问题。
- **MCP filter 格式 ≠ REST API filter 格式**。`get_record_list` 的 `filter` 要求 root 必须是 `group`，condition 嵌套在 `children` 里：`{"type":"group","logic":"AND","children":[{"type":"condition","field":"...","operator":"eq","value":"1"}]}`。扁平的 `{controlId, operator, value}` REST 格式会被 **静默忽略**（不报错不报错，返回全表未过滤数据）。
- **`get_record_list` 返回的 `_id` 是别名，不是真 rowId**。`_id` 是 24 位 hex 内部标识，不能直接传给 `get_record_details`（会报 430002）。真 rowId 在 search 模式返回结果的 `rowId` 字段中。

### 3.1 数据源纪律（Single Source of Truth）

**ClawCRM 项目日志的唯一来源 = 销售同事主动登记的「日志」数据**。在本 org 实际架构下，有两种合法存放形态：

- **形态 A（主表内嵌）**：「项目管理」工作表下 `controlName` 含「日志」的字段。
- **形态 B（独立工作表反向关联）**：同 app 下独立工作表「项目日志」，每条记录通过 `project[].sid == 主项目 rowId` 方式指回。

任何一次项目评审：
1. **只读这两个数据源**。不要去 `日报管理`、`沟通记录`、`客户跟进` 等任何别的工作表找项目跟进数据。
2. **形态 B 必须用 `project[].sid == rowId` 严格过滤**。
3. **命中失败必须硬停止**。Agent **必须**把错误原文返回用户、要求补录，**不允许**拿别处的数据去兜底。

## 4. 端到端流程

按此清单逐步执行：

```
- [ ] S1 通过 token_reader.get_mcp_url("claw-crm") 读取 MCP URL
- [ ] S2 initialize + tools/list，缓存以下工具的 schema
- [ ] S3 定位 ClawCRM appId（用默认值或 get_org_list → get_app_list(org_id)）
- [ ] S4 发现项目工作表 + 跟进字段
- [ ] S5 解析目标项目记录（按 rowId 或按项目名匹配）
- [ ] S6 获取项目记录的跟进日志（全文 + 时间戳）
- [ ] S7 从日志构造检索词；调用 knowledge_search（hybrid, topK=8）
- [ ] S8 按固定评分标准（§5）生成评审报告
- [ ] S9 将报告写回 AI评估 字段，同时勾选「是否需要AI评估」、更新「最后评估时间」
- [ ] S10 向用户展示报告并确认写回成功
```

### 4.1 Hard Stop（硬停止分支）

| exit | error 字段 | 触发条件 | Agent 应答模板 |
|---|---|---|---|
| 2 | Token 不可用 | Token 文件不存在或已过期 | "Token 不可用，请检查 token 文件或联系管理员刷新" |
| 3 | `PROJECT_NOT_FOUND_IN_PROJECT_WS` | 项目管理工作表里没有匹配记录 | "项目「X」在 ClawCRM 的项目管理表里没有对应记录。请先在项目管理表登记该项目。" |
| 4 | `EMPTY_FOLLOW_UP_LOG` | 两种合法来源均为空 | "项目「X」已登记但没有找到任何跟进日志。请先补录最新跟进日志再发起评审。" |

**禁止的降级路径**：
- ❌ 「项目管理表没有 → 去日报管理找」
- ❌ 「日志字段空 → 知识库有 SOP → 直接假设项目阶段」
- ❌ 「用行业常识生成评审」

正确做法：**原样返回错误，请用户补数据，结束会话**。

### 4.2 批量定时评审（Cron 模式）

> 以下流程固化筛选逻辑，智能体**不得自行编造**替代方案。

**字段速查**：

| 用途 | controlId | 类型 |
|------|-----------|------|
| 项目管理表 | `69ca1fb1d128aadb0c749d49` | worksheet |
| 是否需要AI评估 (needAI) | `6a0a8c0dbf6da4a6790db190` | Checkbox |
| 最后评估时间 (lastEval) | `6a0a8c2a314b8166a324f6aa` | DateTime |
| AI评估 (aiEval) | `69f956419f1956fc0e1867c3` | Text |
| 项目日志表 | `69ca1fc9d128aadb0c749edf` | worksheet |
| 日志关联项目 (belongsto) | `69ca205fbf12c183aec577f1` | 关联字段 |
| 日志日期 (log_date) | `69ce50856b2525025fe6ce6b` | Date |

**铁律**：
- needAI 字段**只能由人工操作**，Cron 不得修改
- 无候选项目 → 静默结束，不发通知
- MCP/Hard Stop 异常 → 静默跳过该项目
- 任何一步的 `get_record_list` filter 必须使用 `{type:"group", children:[{type:"condition",...}]}` 格式（见 §3 铁律）
- **日期比较前必须归一化，且严格晚于**：log_date 是 Date (`YYYY-MM-DD`)，lastEval 是 DateTime (`YYYY-MM-DD HH:mm:ss`)。比较时统一截取 `[:10]`，且必须 `>`（严格晚于），不能用 `>=`，否则同一天的旧日志也会误判为新日志。

**步骤**：

```
- [ ] C1 查询 needAI=1 候选
       get_record_list(worksheet_id=69ca1fb1d128aadb0c749d49)
       filter: {type:"group", logic:"AND", children:[
         {type:"condition", field:"6a0a8c0dbf6da4a6790db190", operator:"eq", value:"1"}
       ]}
       ⚠️ 逐条用 get_record_details(row_id) 复核 needAI 真实值

- [ ] C2 安全门
       返回记录数 > 50 → 立即中止，发企微通知 WenJing：
       「needAI 过滤疑似失效，返回 {N} 条，已中止，请人工检查。」
       不得继续。

- [ ] C3 逐项目过滤（对每条 confirmed-needAI=1 的记录）
       a) 用 get_record_details(row_id) 取 lastEval 真实值
       b) lastEval 有值 → 查日志表(69ca1fc9d128aadb0c749edf)
          filter: belongsto=rowId AND log_date[:10] > lastEval[:10]
          有结果 → 进入评审
       c) lastEval 为空 → 查日志表 belongsto=rowId，total>0 → 进入评审
       d) 否则跳过

- [ ] C4 预检通知
       企微消息发给 WenJing，列出候选项目及过滤判断。
       无候选则静默结束。

- [ ] C5 逐项目评审
       对每个候选，执行 §4 单项目流程（S1-S10），或运行：
       python3 /root/.openclaw/skills/crm_project_review/scripts/review_project.py --project "<名称>" --topk 10

- [ ] C6 完成通知
       企微发给 WenJing，列出每项目的评审状态。
```

### 便捷方式：运行辅助脚本

```bash
# 脚本自动从 token 文件读取 token，无需 --mcp-url
python3 skills/crm-project-review/scripts/review_project.py \
  --project "XYZ有限公司"

# 或指定 rowId
python3 skills/crm-project-review/scripts/review_project.py \
  --row-id <ROW_ID>

# 写回模式
python3 skills/crm-project-review/scripts/review_project.py \
  --row-id <ROW_ID> \
  --writeback-file /tmp/report.md
```

脚本输出单个 JSON 文档，包含 `{project, knowledgeHits, tools}`。Agent 据此按 §5 评分标准撰写报告。

## 5. 固定评分标准（五个维度）

### 5.1 项目阶段 (Stage)
- **锚点**：KB 中的销售流程阶段（初次接触 / 转交伙伴 / 展示 / 辅助选型 / 消除疑虑 / 报价 / 签约 / 交付 / 顺藤摸瓜）
- **输出**：`{stage, evidence, confidence: 高/中/低, chunk_refs: [{chunkId, summary}...]}`
- **报告渲染**：在「依据」段落后另起一行，内联标注 `> KB: [chunkId前8位] 摘要（≤15字）`

### 5.2 ICP 匹配度 (ICP Fit)
- **锚点**：KB 中的 ICP 标准（员工数 ≥ 50 / 老板重视 / 有具体数字化问题 / IT 部门参与 / 行业契合 / 团队活力 / 品牌标杆）
- **输出**：`{score: 0-100, matched_criteria: [...], missing_criteria: [...], chunk_refs: [{chunkId, summary}...]}`
- **报告渲染**：在「已满足/待补强」列表后另起一行，内联标注 `> KB: [chunkId前8位] 摘要`

### 5.3 风险点 (Risks)
- **锚点**：日志中的停滞信号（长时间无互动、决策链不清、预算缩减、竞品介入、POC 拖延、合同条款争议）
- **输出**：list of `{risk, severity: 高/中/低, supporting_log_snippet, chunk_ref: {chunkId, summary}}`
- **报告渲染**：表格新增「KB 依据」列，每行列内标注 `[chunkId前8位] 摘要`

### 5.4 下一步动作 (Next Actions)
- **锚点**：KB 中当前阶段的 SOP 动作 + 检测到的风险
- **输出**：有序列表 `{action, deadline_hint, chunk_ref: {chunkId, summary}}`
- **负责人**：所有动作的负责人默认为项目本身的负责人，不需要在报告中建议具体责任人
- **报告渲染**：每条动作下方缩进一行，内联标注 `> KB: [chunkId前8位] 摘要`

### 5.5 SOP 偏离度 (SOP Deviation)
- **锚点**：日志中的已执行动作 vs KB 中该阶段的检查清单
- **输出**：`{expected_actions: [...], performed_actions: [...], missed: [{action, chunk_ref: {chunkId, summary}}...], deviation_score: 0-100}`
- **报告渲染**：每条遗漏动作后内联标注 `> KB: [chunkId前8位] 摘要`

每个维度至少在判断或动作后内联一个 `knowledgeHits[].chunkId`（格式见 §7）。

## 6. 知识库检索策略

> **知识库性质**：项目管理知识库（ID: `69ca75132970faa5ac6ce728`）由多条独立的文档/记录组成（销售流程 SOP、ICP 标准、风险清单、报价策略等），内容会持续增长和更新。每次 `knowledge_search` 结果取决于语义匹配度，同一查询不同时间返回结果可能不同。

1. 从日志中提取：客户行业、最近动作动词、未解决问题、阻塞点、数字信息
2. 构造 3 路并行检索：`query_stage`、`query_risks`、`query_icp`
3. 每次检索：`searchMode=hybrid`、`topK=5~10`、`knowledgeIds=[默认 KB]`
4. 按 `chunkId` 去重；按 score 保留全局前 10 条

## 7. 报告模板

每个维度的判断或动作后均需**内联标注**所依据的 KB chunk，格式为：
`> KB: [chunkId 前8位] 摘要（≤15字）`
不单独输出末尾的 chunk 索引表。

```markdown
# ClawCRM 项目评审：{{项目名}}

- 记录 ID：`{{rowId}}`
- 跟进日志条数：{{N}}
- 最近更新：{{latest_update_time}}
- 评审基准知识库：项目管理知识库（`{{knowledgeId}}`）

## 1. 项目阶段
**判定**：{{stage}}（置信度 {{confidence}}）
**依据**：{{evidence}}
> KB: [{{chunkId_short}}] {{chunk_summary}}

## 2. ICP 匹配度
**得分**：{{score}}/100
- ✅ 已满足：{{matched_criteria}}
- ⚠️ 待补强：{{missing_criteria}}
> KB: [{{chunkId_short}}] {{chunk_summary}}

## 3. 风险点
| 风险 | 严重度 | 日志证据 | KB 依据 |
|---|---|---|---|
| ... | 高/中/低 | "..." | [{{chunkId_short}}] {{chunk_summary}} |

## 4. 下一步动作（按优先级）
1. {{action}} — 建议时限：{{deadline_hint}}
   > KB: [{{chunkId_short}}] {{chunk_summary}}

## 5. SOP 偏离度
- 应做动作清单（KB）：{{expected_actions}}
- 已做动作清单（日志）：{{performed_actions}}
- 遗漏动作：
  - ❌ {{missed_action}} > KB: [{{chunkId_short}}] {{chunk_summary}}
- 偏离度：{{deviation_score}}/100

---
*生成时间：{{ts}}  |  模型引用知识库 {{knowledgeId}}*
```

**内联引用规则**：
- `chunkId_short` = chunkId 的前 8 位十六进制字符
- `chunk_summary` ≤ 15 字，直接提炼 chunk 摘要中的核心概念，不得照抄全文
- 每个维度至少内联一个 chunk；若同一判断有多个 chunk 支撑，逐行列出
- **不输出末尾的 chunk 索引表**

## 8. 写回策略

1. 在项目工作表结构中定位 AI评估 字段的 `controlId`。
2. 使用 `tools/list` 返回的正确 schema 调用 `update_record`，**一次调用同时更新**：
   - `AI评估`（`69f956419f1956fc0e1867c3`）— 评审报告正文
   - `是否需要AI评估`（`6a0a8c0dbf6da4a6790db190`）— 勾选为 `"1"`
   - `最后评估时间`（`6a0a8c2a314b8166a324f6aa`）— 当前时间戳 `YYYY-MM-DD HH:mm:ss`
3. 字段不存在 → **不得**自动创建。告知用户如何手动添加。
4. 写回失败时**不得**静默截断报告。

## 9. 常见陷阱

| 现象 | 根因 | 解法 |
|---|---|---|
| `get_app_list` 返回 `10001` | 传了 `projectId` 而非 `org_id` | 使用 snake_case 的 `org_id` |
| 任何 `get_record_*` 返回 `10001` | 缺少 `ai_description` | 所有记录相关工具必须传 `ai_description` |
| `knowledge_search` 返回 `knowledgeIds不能为空` | 缺少必填字段 | 始终传 `knowledgeIds` 数组 |
| 多个 org 下都有名为 "CRM" 的应用 | 名称冲突 | 用 `appName.strip().lower() == "clawcrm"` 过滤 |
| KB 命中结果跑题 | 检索词太直白 | 提取动作动词 + 行业术语 |
| `update_record` 静默无效果 | `controlId` 错误 | 重新执行 `get_worksheet_structure` |
| Token 过期 | 外部刷新进程未运行 | 联系管理员刷新 token |
| `get_record_list` 带 filter 返回全表（未过滤） | 用了 REST API 扁平格式 `{controlId, operator, value}`，MCP 要求 `{type:"group", children:[{type:"condition",...}]}` 嵌套结构。HAP 对未知 filter 格式**静默忽略**不报错 | 始终从 `tools/list` → `inputSchema` 确认 filter 结构，root 必须是 `group` |
| 日志时间比较假阴性/假阳性 | HAP 中 `跟进日志` 字段是 Date 类型（`YYYY-MM-DD`），`最后评估时间` 是 DateTime 类型（`YYYY-MM-DD HH:mm:ss`）。字符串直接比较：`"2026-06-05" < "2026-06-05 14:15:00"`（前缀规则）。用 `>=` 则同一天旧日志误判为新日志 | 统一截取 `[:10]` 后用 `>`（严格晚于），不用 `>=` |
| 大量 `exec` 调用拖慢评审（60+ 工具调用） | Agent 用 `exec` 跑 shell/Python 直连 HAP，而非通过 MCP 工具 | **严格使用 MCP 工具**（`get_record_list`、`knowledge_search`、`update_record` 等），禁止 `exec`/`curl`/`requests`/`urllib` 拼 HTTP 直连 |
| `get_record_details` 报 430002（rowId 无效） | 传入了 `get_record_list` 返回的 `_id`（24位hex别名），真 rowId 在 search 模式的 `rowId` 字段中 | 始终从 search 结果取 `rowId` 字段（UUID），不用 `_id` |
| `knowledge_search` 报 600302「集成应用不可用」 | Personal MCP (Bearer token) 下 KB 集成不可用，需 Fallback 到 App MCP (Appkey+Sign) | 已知行为，自动切换到 App MCP 重试，无需告警或中断 |

## 10. Related

- L2 `skills/hap_app_access/` — HAP 通用访问方法论 + 共享模块（本仓库）
- [L3 开发规范](../../docs/l3-development.md) — 开发新 L3 技能的标准指南
- 分发 repo：[`crm-project-review`](https://github.com/topmachinegun/crm-project-review)

---

**技能版本**：v3.5.2
**适用范围**：明道云 HAP（SaaS）

**v3.5.2 变更**：
- §4.2 C3 日期比较从 `>=` 修正为 `>`（严格晚于），修复同一天旧日志误判为新日志的假阳性

**v3.5.1 变更**：
- §3 铁律新增：`get_record_list` 的 `_id` 是别名，真 rowId 在 `rowId` 字段
- §9 常见陷阱新增 3 条：禁止 exec/curl 直连 HAP、rowId alias 导致 430002、knowledge_search 600302 fallback

**v3.5.0 变更**：
- 新增 §4.2 批量定时评审（Cron 模式），固化项目筛选范围控制：needAI=1 候选查询 → 50 条安全门 → 逐项目日志时间比对 → 预检通知 → 逐项目评审 → 完成通知

**v3.4.2 变更**：
- §9 常见陷阱新增：Date vs DateTime 字符串比较假阴性（`"2026-06-05" < "2026-06-05 14:15:00"`），比较前必须 `[:10]` 或 `strptime` 归一化

**v3.4.1 变更**：
- §3 铁律 + §9 常见陷阱新增：MCP `get_record_list` filter 必须使用 `group → condition` 嵌套结构，扁平 REST API 格式会被静默忽略（不报错不过滤，返回全表）

**v3.4.0 变更**：
- 报告模板改为内联 KB chunk 引用（每个判断/动作后标注 `[chunkId前8位] 摘要`），移除末尾 chunk 索引表
- §5 各维度输出规范新增 `chunk_refs` / `chunk_ref` 字段
- §7 报告模板表格新增「KB 依据」列（风险点）

**v3.2.0 变更**：
- 评审写回时自动勾选「是否需要AI评估」并更新「最后评估时间」

**v3.1.0 变更**：
- L1 Token Broker 源码移出本仓库，token 由外部进程管理
- 移除 Broker daemon 相关提示和命令

**v3.0.0 变更**：
- Token 管理全面剥离，统一走 L2 token_reader
- 移除 `HAP_MCP_URL` env 作为主路径
- 脚本改用 L2 共享模块（mcp_client.py + token_reader.py）
