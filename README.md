# crm-project-review

一个 **Agent Skill**（遵循 Anthropic 开放的 [Agent Skills](https://docs.claude.com/en/docs/agents-and-tools/agent-skills) 约定：YAML frontmatter + Markdown），基于明道云项目管理知识库对 ClawCRM 里的项目记录进行 AI 评审，输出「阶段判定 / ICP 匹配度 / 风险点 / 下一步动作 / SOP 偏离度」五维结构化报告，可选写回到 ClawCRM 指定字段。

> 本 skill 不绑定任何特定 agent 客户端（Qoder / Claude Code / Cursor / 自研 agent 等均可直接使用）。

## 功能

- 📋 **五维评分**：对齐销售 SOP 知识库，输出可追溯（带 chunkId 引用）的结构化评审。
- 🔍 **日志驱动**：自动拉取项目关联的全部跟进日志作为评审证据。
- 📚 **知识库交叉**：对每个项目并行执行 3 条策略性查询（stage / risks / icp），聚合 Top-K 证据。
- ✍️ **一键回传**：评审结果可写回项目记录的 `AI评估` 字段。

## 定位

这是一个**业务层** skill，专注于评审业务逻辑本身。**v0.3.0 起**本 skill 不再直接处理任何凭据或传输层细节，全部下沉到 `hap-app-access`：

| 职责 | 承担 skill |
|---|---|
| 凭据存放（AppKey+Sign / Personal MCP URL / V3 API base） | [`hap-app-access`](https://github.com/mingdaocom/hap-skills) §5.11 profile |
| MCP / V3 API 调用 + `{ok, mode, data, diagnostics}` 统一契约 | [`hap-app-access`](https://github.com/mingdaocom/hap-skills) §5.12 `hap-access` CLI |
| Personal MCP token 生成 / 刷新 | [`hap-oauth-mcp`](https://github.com/mingdaocom/hap-skills) |
| 项目评审业务本身 | 本 skill |

## 安装

**权威位置**：`skills/crm-project-review/`（根目录、客户端中立）。仓库内的 `.qoder/skills/crm-project-review/` 仅为 symlink，让本项目在 Qoder 打开时自身也能被加载；其他客户端不需要这个位置。

按你使用的 agent 客户端把整个 `skills/crm-project-review/` 目录复制或 symlink 到对应位置：

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

1. 安装并可调用 `hap-access` CLI（由 `hap-app-access` skill 提供，见其 §5.12）。
2. 建好 profile（默认名 `claw-crm`）。**推荐**用本仓随包的模板复制到服务器后填值：

   ```bash
   mkdir -p ~/.local/share/hap-app-access/profiles
   cp skills/crm-project-review/config/profile.claw-crm.template.json \
      ~/.local/share/hap-app-access/profiles/claw-crm.json
   chmod 600 ~/.local/share/hap-app-access/profiles/claw-crm.json
   # 编辑副本：替换 <REPLACE_WITH_CLAWCRM_APPKEY> / <REPLACE_WITH_CLAWCRM_SIGN>
   # 删除 _readme 和 _alternatives 字段
   hap-access profile --validate claw-crm
   ```

   模板默认 `mode=app_mcp`（无人值守场景），`_alternatives` 里并列了 `personal_mcp` / `v3_api` 的变体；或直接走 `hap-access profile --init claw-crm --mode ...` 交互式创建。
   profile 字段约定详见 [hap-app-access SKILL.md §5.11](https://github.com/mingdaocom/hap-skills)；业务 skill 完全不接触 `HAP_MCP_URL` / `HAP_APP_KEY` / `HAP_SIGN_KEY` 等环境变量。
3. ClawCRM 项目管理工作表里需存在 `AI评估` 多行文本字段（如需回传）。

### 典型流程

```bash
# 1) 拉取证据（项目记录 + 跟进日志 + KB 命中）
python3 skills/crm-project-review/scripts/review_project.py \
  --profile claw-crm \
  --project "XYZ有限公司" --topk 8 > bundle.json

# 2) agent 根据 bundle 撰写报告，落盘为 report.md

# 3) 把报告写回 AI评估 字段
python3 skills/crm-project-review/scripts/review_project.py \
  --profile claw-crm \
  --row-id <ROW_ID> --writeback-file report.md
```

`--profile` 可省略，脚本默认读取 env `HAP_ACCESS_PROFILE`，再 fallback 到 `claw-crm`。

### 切换 mode 零代码

要把写回路径从 Personal MCP 换成 AppKey+Sign（V3 API），**不需要改任何代码或命令行参数**，只改 profile：

```bash
hap-access profile --init claw-crm-writeback --mode v3_api
python3 skills/crm-project-review/scripts/review_project.py \
  --profile claw-crm-writeback \
  --row-id <ROW_ID> --writeback-file report.md
```

注意：`v3_api` mode 不支持 `knowledge_search`（hap-access 会抛 `UnsupportedTool`），因此评审主流程仍须走 `personal_mcp` / `app_mcp`，写回分支可随意切。

详见 [SKILL.md](./skills/crm-project-review/SKILL.md) §2.0 / §8.4。

## 业务坐标

本 skill 内置的坐标针对**特定 ClawCRM 应用**，如果你的 CRM 结构不同，请修改 `scripts/review_project.py` 里的 `DEFAULT_*` 常量或在 `SKILL.md` §2.1 中覆盖：

- 项目管理知识库 ID、项目 / 日志 工作表 ID
- `项目名 / 日志关联 / AI评估` 字段 ID

> `appId` 由 profile 管理，**不再**出现在业务脚本里。

## 许可证

MIT —— 详见 [LICENSE](./LICENSE)
