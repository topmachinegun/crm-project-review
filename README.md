# crm-project-review

一个 **Agent Skill**（遵循 Anthropic 开放的 [Agent Skills](https://docs.claude.com/en/docs/agents-and-tools/agent-skills) 约定：YAML frontmatter + Markdown），基于明道云项目管理知识库对 ClawCRM 里的项目记录进行 AI 评审，输出「阶段判定 / ICP 匹配度 / 风险点 / 下一步动作 / SOP 偏离度」五维结构化报告，可选写回到 ClawCRM 指定字段。

> 本 skill 不绑定任何特定 agent 客户端（Qoder / Claude Code / Cursor / 自研 agent 等均可直接使用）。

## 功能

- 📋 **五维评分**：对齐销售 SOP 知识库，输出可追溯（带 chunkId 引用）的结构化评审。
- 🔍 **日志驱动**：自动拉取项目关联的全部跟进日志作为评审证据。
- 📚 **知识库交叉**：对每个项目并行执行 3 条策略性查询（stage / risks / icp），聚合 Top-K 证据。
- ✍️ **一键回传**：评审结果可写回项目记录的 `AI评估` 字段。

## 定位

这是一个**业务层** skill，专注于评审业务逻辑本身。授权、MCP 协议调用等基础设施职责由前置 skill 承担：

| 职责 | 承担 skill |
|---|---|
| MCP Token 生成 / 刷新 | [`hap-oauth-mcp`](https://github.com/mingdaocom/hap-skills) |
| MCP 协议调用规范 | [`hap-app-access`](https://github.com/mingdaocom/hap-skills) |
| 项目评审业务本身 | 本 skill |

## 安装

本 skill 的实体在 `.qoder/skills/crm-project-review/`（为了 Qoder 开箱即用），但内容本身完全通用。请按你使用的 agent 客户端把整个 `crm-project-review/` 目录复制到对应位置：

| Agent 客户端 | 工作区导入 | 用户级导入 |
|---|---|---|
| Qoder | `.qoder/skills/crm-project-review/` | `~/.qoder/skills/crm-project-review/` |
| Claude Code | `.claude/skills/crm-project-review/` | `~/.claude/skills/crm-project-review/` |
| 其他遵循 Agent Skills 约定的客户端 | 参照该客户端文档 | 同左 |

只需 `SKILL.md` + `scripts/` 同时拷贝。skill 无额外依赖（标准库的 Python 3 即可运行）。

## 使用

### 触发词
> 「评估项目」「帮我评 XXX 项目」「基于知识库评估项目」「review claw project」

### 运行前置
- 已配置 `HAP_MCP_URL`（由 `hap-oauth-mcp` 生成）
- ClawCRM 项目管理工作表里已存在 `AI评估` 多行文本字段（如需回传）

### 典型流程

```bash
export HAP_MCP_URL="<由 hap-oauth-mcp 生成>"

# 1) 拉取证据（项目记录 + 跟进日志 + KB 命中）
python3 .qoder/skills/crm-project-review/scripts/review_project.py \
  --project "XYZ有限公司" --topk 8 > bundle.json

# 2) agent 根据 bundle 撰写报告，落盘为 report.md

# 3) 把报告写回 AI评估 字段
python3 .qoder/skills/crm-project-review/scripts/review_project.py \
  --row-id <ROW_ID> --writeback-file report.md
```

详见 [SKILL.md](./.qoder/skills/crm-project-review/SKILL.md)。

## 业务坐标

本 skill 内置的坐标针对**特定 ClawCRM 应用**，如果你的 CRM 结构不同，请修改 `scripts/review_project.py` 里的 `DEFAULT_*` 常量或在 `SKILL.md` §3 中覆盖：

- `appId`、项目管理知识库 ID、项目 / 日志 工作表 ID
- `项目名 / 日志关联 / AI评估` 字段 ID

## 许可证

MIT —— 详见 [LICENSE](./LICENSE)
