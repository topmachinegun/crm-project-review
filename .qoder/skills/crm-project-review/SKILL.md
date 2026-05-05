---
name: crm-project-review
description: Evaluates a ClawCRM project record against the Mingdao knowledge base (project management KB) and produces a structured review covering stage, ICP fit, risks, next actions and SOP deviation, with the report optionally written back to a ClawCRM field. Use when the user says "评估项目", "项目跟进评审", "ClawCRM 项目 AI 评审", "帮我评 X 项目", "review claw project", or asks for a CRM project health check backed by Mingdao knowledge base.
---

# ClawCRM Project Review

Evaluates a ClawCRM project using its follow-up logs, cross-referenced against the Mingdao project-management knowledge base, and emits a structured review report. Optionally writes the report back into the project record.

## 1. When to Trigger

Strong trigger phrases (Chinese first):
- "评估项目 / 评估 X 项目 / 帮我评一下 X 项目"
- "项目跟进评审 / 项目健康度分析"
- "ClawCRM 项目 AI 评审 / 基于知识库评估项目"
- English equivalents: "review claw project", "assess project follow-up", "CRM project health check with KB"

The user will typically supply **either** a project name **or** a rowId. Both paths must be supported.

## 2. Prerequisites

| Item | Default Value | How to obtain if missing |
|---|---|---|
| Personal MCP token | **优先级**：arg `--mcp-url` > env `HAP_MCP_URL` > 直读 **hap-app-access Token Broker 中控服务**落盘的 token JSON（默认 `~/.local/share/hap-token-broker/tokens/claw-crm.json`）。三者都没时报错终止。**本 skill 不再自己刷 token**，刷新责任唯一属于 broker（守护进程 systemd 托管，详见 hap-app-access SKILL.md §5.10）。可选 env：`HAP_TOKEN_PROFILE`（默认 `claw-crm`）、`HAP_TOKEN_FILE`（显式覆盖文件路径）。调用前请确保 broker 已部署：`systemctl status hap-token-broker && hap-token status`。 |
| ClawCRM appId | `49392ae2-6aa0-4d69-b5e7-57d4fe3fc98e` | n/a (hard-coded default) |
| Knowledge base id | `69ca75132970faa5ac6ce728` ("项目管理知识库") | Call `get_app_knowledge_list(appId)` and re-select |
| Project worksheet | `69ca1fb1d128aadb0c749d49`（项目管理） | 固定锚点；如被 org 改名，`get_app_worksheets_list` 里选 name 含「项目管理」的那张，**不得**选「日报管理」「沟通」等别的表 |
| Follow-up log source | 两种合法形态之一：**(a)** 项目管理工作表里 `controlName` 含「日志」的字段（主表直属字段或同表下的子表）；**(b)** 同 app 下独立工作表「项目日志」(默认 id `69ca1fc9d128aadb0c749edf`)，通过其 `project` 关联字段 `project[].sid == 主项目 rowId` 反向关联。**两者任一命中即可**，均为合法唯一数据源。**禁止**从 日报管理 / 沟通记录 / follow / log 等别的工作表或字段拼凑。详见 §3.1 数据源纪律。 |
| Write-back field | default `ai_evaluation` (alias `AI评估`) | If absent, prompt the user; do NOT silently create it unless the user approves |

## 3. Iron Rule (inherits hap-app-access v1.5 §7.1)

**Always call `tools/list` once at the beginning of a session and drive every subsequent `tools/call` strictly by the returned `inputSchema`.** Observed quirks in this Personal MCP:

- Same tool mixes snake_case and camelCase keys: `update_record` requires `worksheet_id`, `row_id`, `fields` **and** `appId` in one call.
- `get_app_list` uses `org_id` (snake_case); `get_app_knowledge_list` / `knowledge_search` use `appId` (camelCase).
- **Every record-related tool requires an `ai_description` string** (a natural-language description of what you're doing, e.g. `"Worksheet: 项目库, Record: XYZ客户. Fetch full record details."`). Missing it yields `10001 Http Headers verification failed`.
- Error `10001` almost always means **a wrong input key name or a missing `ai_description`**, not a token/header problem.

See `learned_skill: Personal MCP 知识库发现+检索全链路调用指南` for the full cheat sheet.

### 3.1 数据源纪律（Single Source of Truth）

**ClawCRM 项目日志的唯一来源 = 销售同事主动登记的「日志」数据**。在本 org 实际架构下，有两种合法存放形态（脚本同时兼容），**任一命中即视为合法数据源**：

- **形态 A（主表内嵌）**：「项目管理」工作表下 `controlName` 含「日志」的字段（主表直属字段或同工作表下的子表记录）。
- **形态 B（独立工作表反向关联）**：同 app 下独立工作表「项目日志」(默认 id `69ca1fc9d128aadb0c749edf`)，每条记录的 `project` 关联字段以 `project[].sid == 主项目 rowId` 方式指回项目管理表。脚本 S6 的兜底就是查这张表 + 严格过滤 `sid` 等于主项目 rowId。本 org 2026-04 实测走的就是这条路径（主表无内嵌日志字段，6 条日志全在独立工作表里）。

任何一次项目评审：

1. **只读这两个数据源**。不要去 `日报管理`、`沟通记录`、`客户跟进`、`follow_up`、`communication_log` 等任何别的工作表找项目跟进数据。日报和沟通表即使提到了这个项目，**也不是**项目评审的数据源。
2. **形态 B 必须用 `project[].sid == rowId` 严格过滤**，不得用项目名模糊包含替代——模糊包含会跨项目串流（同客户名下多个项目互相污染）。
3. **命中失败必须硬停止**。脚本已实现两种硬停止（exit 3 / exit 4），详见 §4.1。Agent **必须**把错误原文返回用户、要求补录，**不允许**拿别处的数据去兜底、推断或「结合知识库创造性补全」。

为什么这么严：项目评审要得出"阶段 / ICP / 风险 / 下一步 / SOP 偏离"五维结论，**所有这些结论的证据必须来自销售同事在项目日志里主动写下的记录**。日报、沟通里出现的项目名只是提及，不构成销售推进轨迹；用它们生成评审 = 把"别人顺口提了一句"当成"项目经理已经推进到这一步"，会直接污染决策。

## 4. End-to-End Workflow

Copy this checklist and tick each step:

```
- [ ] S1 Resolve MCP URL (reuse env or generate new token)
- [ ] S2 initialize + tools/list, cache schemas for the tools below
- [ ] S3 Locate ClawCRM appId (default or via get_org_list → get_app_list(org_id))
- [ ] S4 Discover project worksheet + follow-up field
- [ ] S5 Resolve the target project record (by rowId OR by name match)
- [ ] S6 Fetch the record's follow-up logs (full text + timestamps)
- [ ] S7 Build query terms from the logs; call knowledge_search (hybrid, topK=8)
- [ ] S8 Generate the review report using the fixed Rubric (§5)
- [ ] S9 Write the report back to the AI评估 field (or skip if field absent)
- [ ] S10 Show the report to the user and confirm write-back success
```

### 4.1 Hard Stop（硬停止分支）

脚本 `review_project.py` 在两种情况下会**主动终止**并返回非零 exit code + 结构化错误。Agent **必须**把错误原文告知用户并停止评审，**不得**切换数据源、不得用知识库创造性补全。

| exit | error 字段 | 触发条件 | Agent 应答模板 |
|---|---|---|---|
| 3 | `PROJECT_NOT_FOUND_IN_PROJECT_WS` | 项目管理工作表里没有匹配 `--project` 名字 / `--row-id` 的记录 | "项目「X」在 ClawCRM 的项目管理表里没有对应记录。请先在项目管理表登记该项目（包括销售负责人、客户背景、最近一次跟进日志），再发起评审。" |
| 4 | `EMPTY_FOLLOW_UP_LOG` | 项目记录存在，但两种合法来源（主表内嵌字段 + 独立「项目日志」工作表反向关联）均为空 | "项目「X」在项目管理表里已登记，但没有找到任何跟进日志（主表内嵌字段和独立『项目日志』工作表都查过）。项目评审的唯一数据源是这些日志；请先补录最新跟进日志再发起评审。" |

**禁止的降级路径**（OpenClaw Agent 在 2026-04 的一次评审中真实踩过的反模式，见 §12）：

- ❌ 「项目管理表没有 → 去日报管理找提到该项目的日报 → 拼凑跟进记录」
- ❌ 「日志字段空 → 知识库有 SOP → 直接按 SOP 模板假设项目走到哪步」
- ❌ 「rowId 空 → 但我知道这个项目存在 → 基于行业常识生成评审」

正确做法：**原样返回错误，请用户补数据，结束会话**。

### Convenience: run the helper script

The heavy discovery + fetch + search work is pre-scripted. Invoke it and consume its JSON:

```bash
# <SKILL_ROOT> = the directory that contains THIS SKILL.md
# i.e. run: SKILL_ROOT="$(dirname "$(realpath SKILL.md)")"
python3 "$SKILL_ROOT/scripts/review_project.py" \
  --mcp-url "https://api2.mingdao.com/mcp?Authorization=Bearer%20<TOKEN>" \
  --project "XYZ有限公司" \
  --app-id 49392ae2-6aa0-4d69-b5e7-57d4fe3fc98e \
  --knowledge-id 69ca75132970faa5ac6ce728 \
  --topk 8 \
  > /tmp/project_bundle.json
```

Or by rowId:
```bash
python3 "$SKILL_ROOT/scripts/review_project.py" \
  --mcp-url "$HAP_MCP_URL" --row-id <ROW_ID>
```

The script outputs a single JSON document with `{project, knowledgeHits, tools}`. The agent then writes the report using that bundle and the Rubric in §5.

## 5. Fixed Rubric (five dimensions)

The report MUST cover exactly these five dimensions, in this order, each with the fields shown.

### 5.1 项目阶段 (Stage)
- **Anchor**: Map against the Sales Process stages from the KB (Sales Playbook — 初次接触 / 转交伙伴 / 展示 / 辅助选型 / 消除疑虑 / 报价 / 签约 / 交付 / 顺藤摸瓜).
- **Output**: `{stage, evidence, confidence: 高/中/低}`

### 5.2 ICP 匹配度 (ICP Fit)
- **Anchor**: ICP criteria from the KB (员工数 ≥ 50 / 老板重视 / 有具体数字化问题 / IT 部门参与 / 行业契合 / 团队活力 / 品牌标杆).
- **Output**: `{score: 0-100, matched_criteria: [...], missing_criteria: [...]}`

### 5.3 风险点 (Risks)
- **Anchor**: Look for stall signals in logs (长时间无互动、决策链不清、预算缩减、竞品介入、POC 拖延、合同条款争议).
- **Output**: list of `{risk, severity: 高/中/低, supporting_log_snippet}`

### 5.4 下一步动作 (Next Actions)
- **Anchor**: Combine the current stage's SOP actions from the KB with the detected risks.
- **Output**: ordered list of `{action, owner_hint, deadline_hint, kb_reference_chunkId}`

### 5.5 SOP 偏离度 (SOP Deviation)
- **Anchor**: Compare logged actions against the stage's checklist in the KB.
- **Output**: `{expected_actions: [...], performed_actions: [...], missed: [...], deviation_score: 0-100}`

Every dimension must cite at least one `knowledgeHits[].chunkId` if the KB has relevant content; if the KB returns nothing relevant for a dimension, state "知识库未命中，采用通用判断" explicitly.

## 6. Knowledge Search Strategy

1. Extract from the logs: customer industry, latest action verb, open questions, blockers, numerical mentions (amount, headcount, timeline).
2. Build 3 parallel queries per review:
   - `query_stage`  = "销售阶段 " + last action verbs  → for dimension 5.1 & 5.4
   - `query_risks`  = the detected stall signal phrases joined by space → for 5.3
   - `query_icp`    = industry + company-size mentions → for 5.2
3. For each query: `searchMode=hybrid`, `topK=5~10`, `knowledgeIds=[default KB]`, `appId=<ClawCRM>`.
4. Deduplicate hits by `chunkId`; keep top 10 across all queries by score.

## 7. Report Template

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

## 8. Write-back Policy

1. After the report is generated, locate the write-back field's `controlId` in the project worksheet structure (bundle's `project.structure.controls`). Match by `controlName == "AI评估"` or `alias == "ai_evaluation"`.
2. **If the field exists** → call `update_record` with these exact keys (observed in `tools/list` schema):
   ```json
   {
     "worksheet_id": "<project_worksheet_id>",
     "row_id": "<project_row_id>",
     "appId": "49392ae2-6aa0-4d69-b5e7-57d4fe3fc98e",
     "fields": [{"controlId": "<ai_evaluation_controlId>", "value": "<markdown_report>"}],
     "ai_description": "Worksheet: <name>, Record: <title>. Write AI review report into AI评估 field."
   }
   ```
3. **If the field is absent** → do NOT auto-create. Show the user the manual build instruction (HAP UI → worksheet → add field of type 40 multi-line text named "AI评估" alias `ai_evaluation`) and keep the full report in chat.
4. Never truncate the report silently on write-back failure — surface the error verbatim and keep the full report in the chat.

## 9. Common Pitfalls

| Symptom | Root cause | Fix |
|---|---|---|
| `10001 Http Headers verification failed` on `get_app_list` | Passed `projectId` instead of `org_id` | Use snake_case `org_id` (per §3 iron rule) |
| `10001` on any `get_record_*` | Missing `ai_description` | Every record tool requires `ai_description` — a natural language sentence describing the call |
| `knowledge_search` returns `knowledgeIds不能为空` | Missed required field | Always pass the full `knowledgeIds` array even for a single KB |
| Multiple apps named "CRM" across orgs | Name collision | Filter by `appName.strip().lower() == "clawcrm"` AND org = "明道云数字化企业" |
| KB hits look off-topic | Query too literal | Extract action verbs + industry terms, not raw log sentences |
| `update_record` silently no-op | Wrong `controlId` | Re-run `get_worksheet_structure` and map by `controlName`, not English guess |
| Tokens leaked in shell history | `--password` inline | Use the script's `HAP_MCP_URL` env var path instead |

## 10. Related

- `hap-app-access` skill — protocol and path decisions (v1.5)
- `hap-oauth-mcp` skill — token generation pipeline
- learned_skill `Personal MCP 知识库发现+检索全链路调用指南` — end-to-end call cheat sheet

## 11. Script

The bundled `scripts/review_project.py` handles S1–S7 automatically and returns a JSON document consumable by the agent. Read it once if schema changes; never copy its logic into chat — **invoke the script** instead.

Bundle 里 `project.logSourceField` 会显式告诉 Agent 日志到底来自哪个字段（例如 `"日志"` / `"跟进日志"` / `"项目日志-子表"`）。如果 bundle 里有 `error` 字段 → 参见 §4.1 Hard Stop，**必须**原样返回用户，不得继续。

## 12. Anti-pattern（反模式 · 禁止照搬）

以下是 OpenClaw 智能体在一次实际评审中出现的**错误行为**，记录下来作为反面教材：

> 脚本跑完了，但发现一个关键问题——项目管理 worksheet 里没有这个项目的记录（rowId 为空，跟进日志也返回 0 条）。
> 说明这个项目要么还没录入项目管理表，要么是跟进记录散在别处（日报管理里有提到但项目管理表里没有）。
> **不过脚本已经成功调用了知识库（Sales Playbook），我结合日报里已有的跟进记录+知识库内容来生成评审报告**：……

**为什么这是错的**：

1. 脚本返回 `error=PROJECT_NOT_FOUND_IN_PROJECT_WS`（exit 3）就应当终止，Agent 擅自切换数据源。
2. 日报管理表里提到项目 ≠ 项目已推进；把"别人顺口提了一句"当成销售推进轨迹，直接污染阶段判定和 SOP 偏离度。
3. 知识库只能作为**评判标尺**（Rubric / SOP 对照），不能当作**事实来源**来补全缺失的项目进展。
4. 生成出来的报告看起来"有理有据"，但销售同事拿去跟客户核对时会发现全部是幻觉，直接毁掉用户对 AI 评审的信任。

**正确行为**：遇到 Hard Stop → 原样告知用户 → 让用户去项目管理表补录记录/日志 → 补录完再重跑评审。评审的门槛就是"日志必须被销售同事主动写下来"，这是纪律不是 bug。
