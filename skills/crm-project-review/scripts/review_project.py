#!/usr/bin/env python3
"""review_project.py — ClawCRM 项目评审数据采集管道。

用法：
    python3 review_project.py \
        --mcp-url "https://api2.mingdao.com/mcp?Authorization=Bearer%20<TOK>" \
        --project "XYZ客户"                  # 或 --row-id <ROW_ID>
        [--app-id <APP_ID>] [--knowledge-id <KB_ID>]
        [--worksheet-hint 项目] [--log-field-hint 跟进]
        [--topk 8]

输出：stdout 一个 JSON bundle，结构：
    {
      "project": {
        "worksheetId": "...", "worksheetName": "...",
        "rowId": "...", "title": "...",
        "fields": { ...normalized record... },
        "followUpLogs": [ {text, time, source} ... ],
        "structure": { "controls":[{controlId, controlName, type, alias}, ...] },
        "writeBackField": {"controlId": "...", "controlName": "...", "alias": "..."}  # 或 null
      },
      "knowledgeHits": [ {chunkId, content, score, knowledgeName, source, query} ... ],
      "tools": { "<name>": {<inputSchema>} },  # 留给 agent 写 update_record 时对照
      "diagnostics": [ ...text... ]
    }

Agent 拿到 JSON 后，按 SKILL.md §5 Rubric 生成报告；
然后用 --writeback-file 写回。写回时自动勾选「是否需要AI评估」并更新「最后评估时间」。
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# L2 共享模块路径注入（hap-skill-claw-lite monorepo 约定）
_SKILLS_ROOT = Path(__file__).resolve().parent.parent.parent  # skills/
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

# L2 共享模块: hap-app-access
from hap_app_access.src.mcp_client import MCPClient  # noqa: E402
from hap_app_access.src.token_reader import get_mcp_url, TokenNotFoundError  # noqa: E402


def diag(msg: str) -> None:
    """诊断日志统一打到 stderr，不污染 stdout JSON bundle。"""
    print(f"[diag] {msg}", file=sys.stderr, flush=True)


def _row_title(r: dict) -> str:
    return str(r.get("title") or r.get("name") or "")


# 去除中文公司名常见后缀/限定词，切出特征关键词。
_COMPANY_STOPWORDS = (
    "股份有限公司", "有限责任公司", "有限公司",
    "分公司", "子公司", "集团", "公司",
)


def extract_project_name_tokens(name: str) -> list[str]:
    """从项目名抽特征关键词。

    “中国石油天然气股份有限公司华北油田分公司”
      → [“华北油田”, “中国石油天然气”]  # 长度优先给尾部（地区/业务特征）
    """
    stem = name or ""
    for w in _COMPANY_STOPWORDS:
        stem = stem.replace(w, " ")
    # 再按 CJK 以外的分隔符拆
    parts = re.split(r"[\s\-\u3001\u3002\uff0c\uff0c,\-\(\)\uff08\uff09]+", stem)
    tokens = [t.strip() for t in parts if t and len(t.strip()) >= 2]
    # 尾部地区/业务特征更具辨识度，优先试
    tokens.reverse()
    # 去重
    seen: set[str] = set()
    uniq: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq

DEFAULT_APP_ID = "49392ae2-6aa0-4d69-b5e7-57d4fe3fc98e"  # ClawCRM
DEFAULT_KB_ID = "69ca75132970faa5ac6ce728"  # 项目管理知识库
DEFAULT_PROJECT_WS = "69ca1fb1d128aadb0c749d49"  # 项目管理 工作表
DEFAULT_WRITEBACK_CONTROLID = "69f956419f1956fc0e1867c3"  # AI评估 字段
CHECK_NEED_AI_EVAL_ID = "6a0a8c0dbf6da4a6790db190"  # 是否需要AI评估 (Checkbox)
LAST_EVAL_TIME_ID = "6a0a8c2a314b8166a324f6aa"  # 最后评估时间 (DateTime)


# MCPClient 从 L2 hap-app-access 共享模块导入（见文件头部 import）
# 替代了旧版内联实现，统一 MCP JSON-RPC 调用逻辑



def ai_desc(s: str) -> str:
    return s[:180]  # 防止超长


def extract_logs_from_record(record: Any, struct_controls: list[dict]) -> tuple[list[dict], str | None]:
    """仅从项目主表记录里提取字段名含「日志」的字段，返回 (日志列表, 字段名)。

    数据源纪律：ClawCRM 项目日志唯一来源 = 「项目管理」工作表下名字含「日志」的字段。
    禁止扩展到 日报 / 沟通记录 / follow / log 等其他语义，以免引入外表数据。
    对关联子表，本函数不展开（需额外 get_record_relations）。
    """
    candidates: list[dict] = []
    hit_field: str | None = None
    # 唯一识别关键词：「日志」。不再匹配 跟进/记录/沟通/follow/log。
    log_keywords = ("日志",)
    if not isinstance(record, dict):
        return [], None
    for ctrl in struct_controls:
        name = str(ctrl.get("controlName", ""))
        alias = str(ctrl.get("alias", ""))
        if not any(k in name for k in log_keywords) and \
           not any(k in alias.lower() for k in log_keywords):
            continue
        # 尝试多种 key 名取值
        v = record.get(alias) or record.get(ctrl.get("controlId", ""))
        if v is None:
            continue
        if isinstance(v, str) and v.strip():
            candidates.append({"text": v, "time": None, "source": name})
            hit_field = hit_field or name
        elif isinstance(v, list):
            # 子表或多值
            for item in v:
                if isinstance(item, dict):
                    txt = item.get("name") or item.get("text") or json.dumps(item, ensure_ascii=False)
                    candidates.append({"text": str(txt), "time": item.get("createTime"), "source": name})
            if candidates:
                hit_field = hit_field or name
    return candidates, hit_field


def build_queries(logs: list[dict], record: dict) -> dict[str, str]:
    """基于日志和记录字段构造 3 个查询词。"""
    log_blob = " ".join((l.get("text") or "") for l in logs)[-1500:]  # 最后 1500 字
    customer = str(record.get("title") or record.get("name") or record.get("客户名") or "")

    # stage：最近的动作词
    stage_keywords = []
    for kw in ("演示", "POC", "报价", "签约", "合同", "交付", "提案", "选型", "微信", "电话", "会议"):
        if kw in log_blob:
            stage_keywords.append(kw)
    query_stage = "销售阶段 " + " ".join(stage_keywords[:5]) if stage_keywords else "销售阶段 跟进"

    # risks：停滞信号
    risk_signals = []
    for sig in ("预算", "决策", "竞品", "暂缓", "搁置", "下次", "等通知", "暂时"):
        if sig in log_blob:
            risk_signals.append(sig)
    query_risks = "风险 停滞 " + " ".join(risk_signals[:5]) if risk_signals else "客户流失 风险"

    # icp：行业 + 规模
    query_icp = f"理想客户画像 ICP {customer}".strip()

    return {
        "query_stage": query_stage,
        "query_risks": query_risks,
        "query_icp": query_icp,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mcp-url", default=os.environ.get("HAP_MCP_URL"),
                   help="Personal MCP URL (含 Authorization=Bearer%%20<token>)")
    p.add_argument("--project", help="项目名（用于 get_record_list search 过滤）")
    p.add_argument("--row-id", help="项目记录 rowId（优先于 --project）")
    p.add_argument("--app-id", default=DEFAULT_APP_ID)
    p.add_argument("--knowledge-id", default=DEFAULT_KB_ID)
    p.add_argument("--worksheet-hint", default="项目",
                   help="项目主工作表名称包含的关键词")
    p.add_argument("--log-field-hint", default="跟进",
                   help="跟进日志字段名包含的关键词")
    p.add_argument("--writeback-alias", default="ai_evaluation")
    p.add_argument("--writeback-name", default="AI评估")
    p.add_argument("--writeback-file",
                   help="若提供，则进入 写回模式：读取该文件的 Markdown 内容写回 AI评估 字段，不再拉日志/检索 KB。必须配合 --row-id。")
    p.add_argument("--writeback-controlid", default=DEFAULT_WRITEBACK_CONTROLID,
                   help="AI评估 字段的 controlId，默认使用业务坐标里的固定值；如结构变动再覆盖。")
    p.add_argument("--writeback-worksheet", default=DEFAULT_PROJECT_WS,
                   help="项目工作表 ID，默认使用业务坐标里的固定值。")
    p.add_argument("--topk", type=int, default=8)
    args = p.parse_args()

    if not args.mcp_url:
        # v3.0: Token 由 L1 Token Broker 中控服务统一管理
        # 通过 L2 token_reader 直读 Broker token 文件，不自行刷新
        profile = os.environ.get("HAP_TOKEN_PROFILE", "claw-crm")
        try:
            args.mcp_url = get_mcp_url(profile, check_expiry=True)
            diag(f"S1 mcp_url via token_reader profile={profile}")
        except TokenNotFoundError as e:
            print(f"ERROR: Token Broker token 不可用: {e}", file=sys.stderr)
            print("提示：确保 hap-token-broker 已部署并运行：", file=sys.stderr)
            print("  systemctl status hap-token-broker && hap-token status", file=sys.stderr)
            print("  或手动触发刷新: hap-token refresh {}".format(profile), file=sys.stderr)
            return 2

    # ========== 写回模式：只做写回，不拉日志/不检索 KB ==========
    if args.writeback_file:
        if not args.row_id:
            print("ERROR: 写回模式必须同时提供 --row-id", file=sys.stderr)
            return 2
        try:
            with open(args.writeback_file, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            print(f"ERROR: 读取 writeback-file 失败: {e}", file=sys.stderr)
            return 2
        if not content.strip():
            print("ERROR: writeback-file 内容为空，拒绝写回", file=sys.stderr)
            return 2

        cli = MCPClient(args.mcp_url, mode="personal_mcp")
        cli.ensure_initialized()

        ws_id = args.writeback_worksheet
        control_id = args.writeback_controlid

        # 调用前校验：查 structure 对齐 controlId（避免字段被删/重建）
        struct = cli.call("get_worksheet_structure", {
            "worksheet_id": ws_id,
            "appId": args.app_id,
            "responseFormat": "json",
            "ai_description": ai_desc(
                f"Worksheet: 项目管理. Verify AI评估 controlId before writeback."),
        })
        fields = (struct or {}).get("fields", []) if isinstance(struct, dict) else []
        match = None
        for f in fields:
            nm = str(f.get("name", ""))
            al = str(f.get("alias", ""))
            if (f.get("controlId") or f.get("id")) == control_id \
               or nm == args.writeback_name \
               or al == args.writeback_alias:
                match = f
                break
        if not match:
            print(json.dumps({
                "ok": False,
                "reason": "AI评估 字段未在工作表结构中命中；请人工确认字段名/alias/controlId。",
                "tried_controlId": control_id,
                "tried_name": args.writeback_name,
                "tried_alias": args.writeback_alias,
                "diagnostics": cli.diagnostics,
            }, ensure_ascii=False, indent=2))
            return 1

        resolved_cid = match.get("controlId") or match.get("id") or control_id

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        upd = cli.call("update_record", {
            "worksheet_id": ws_id,
            "row_id": args.row_id,
            "appId": args.app_id,
            "fields": [
                {"id": resolved_cid, "value": content},                    # AI评估
                {"id": CHECK_NEED_AI_EVAL_ID, "value": "1"},              # 是否需要AI评估
                {"id": LAST_EVAL_TIME_ID, "value": now_str},              # 最后评估时间
            ],
            "ai_description": ai_desc(
                f"Worksheet: 项目管理, Record: {args.row_id}. Write AI review report into AI评估 field, check 是否需要AI评估, update 最后评估时间."),
        })

        # update_record 成功时，cli.call() 剩下 data 层 = rowId 字符串；
        # 仅当返回 rowId 相等（或 dict 中 success!=false）才算成功，不再依赖 diagnostics
        if isinstance(upd, str):
            ok = upd == args.row_id
        elif isinstance(upd, dict):
            ok = upd.get("success") is not False and upd.get("error_code") in (None, 0)
        else:
            ok = False
        print(json.dumps({
            "ok": ok,
            "worksheetId": ws_id,
            "rowId": args.row_id,
            "controlId": resolved_cid,
            "fieldName": match.get("name"),
            "charsWritten": len(content),
            "response": upd,
            "diagnostics": cli.diagnostics,
        }, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    # ========== 写回模式结束 ==========

    if not (args.project or args.row_id):
        print("ERROR: 必须提供 --project 或 --row-id", file=sys.stderr)
        return 2

    cli = MCPClient(args.mcp_url, mode="personal_mcp")

    # S2 initialize + tools/list
    cli.ensure_initialized()
    tl_raw = cli.list_tools()
    tools = {t["name"]: t.get("inputSchema", {}) for t in tl_raw}

    # S4 发现项目工作表
    ws_list = cli.call("get_app_worksheets_list", {"appId": args.app_id})
    project_ws = None

    # 打印原始 worksheets 列表一次，方便诊断工作表表名实际长什么样。

    def _walk_ws(o):
        if isinstance(o, dict):
            name = o.get("worksheetName") or o.get("name") or ""
            wid = o.get("worksheetId") or o.get("id")
            if wid and name:
                yield {"worksheetId": wid, "worksheetName": name}
            for v in o.values():
                yield from _walk_ws(v)
        elif isinstance(o, list):
            for v in o:
                yield from _walk_ws(v)

    all_ws = list(_walk_ws(ws_list))
    # 排除 "跟进/任务/客户"，只要带 "项目" 的
    candidates = [w for w in all_ws if args.worksheet_hint in w["worksheetName"]
                  and not any(k in w["worksheetName"] for k in ("跟进", "日志", "任务", "汇报"))]
    if not candidates and all_ws:
        # 兜底：第一个含关键词的
        candidates = [w for w in all_ws if args.worksheet_hint in w["worksheetName"]]
    # 优先选名为「项目管理」的那张（严格匹配）；其次才是 candidates[0]。
    exact = [w for w in candidates if w["worksheetName"] == "项目管理"]
    if exact:
        project_ws = exact[0]
    elif candidates:
        project_ws = candidates[0]
    else:
        cli.diagnostics.append(f"未找到含 '{args.worksheet_hint}' 的工作表；现有工作表：{[w['worksheetName'] for w in all_ws]}")

    diag(f"S4 allWorksheets = {[w['worksheetName'] for w in all_ws]}")
    if project_ws:
        diag(f"S4 picked projectWorksheet = {project_ws['worksheetName']} ({project_ws['worksheetId']})")

    if not project_ws:
        print(json.dumps({"project": None, "knowledgeHits": [], "tools": tools,
                           "diagnostics": cli.diagnostics, "allWorksheets": all_ws}, ensure_ascii=False, indent=2))
        return 1

    ws_id = project_ws["worksheetId"]
    ws_name = project_ws["worksheetName"]

    # 获取工作表结构
    struct = cli.call("get_worksheet_structure", {
        "worksheet_id": ws_id,
        "appId": args.app_id,
        "responseFormat": "json",
        "ai_description": ai_desc(f"Worksheet: {ws_name}. Fetch structure for project review skill."),
    })
    # controls 的抽取：明道云返回结构通常是 {controls:[{controlId,controlName,type,alias,...}], ...}
    controls: list[dict] = []
    if isinstance(struct, dict):
        controls = struct.get("controls") or struct.get("fields") or []
    elif isinstance(struct, list):
        for it in struct:
            if isinstance(it, dict) and "controls" in it:
                controls = it["controls"]
                break

    # 找回写字段
    writeback_field = None
    for c in controls:
        nm = str(c.get("controlName", ""))
        al = str(c.get("alias", ""))
        cid = c.get("controlId") or c.get("id")
        if cid == args.writeback_controlid \
           or nm == args.writeback_name \
           or al == args.writeback_alias:
            writeback_field = {"controlId": cid, "controlName": nm, "alias": al,
                               "type": c.get("type")}
            break

    # S5 定位 row
    row_id = args.row_id
    record: dict = {}
    record_title = ""

    if row_id:
        detail = cli.call("get_record_details", {
            "worksheet_id": ws_id,
            "row_id": row_id,
            "appId": args.app_id,
            "ai_description": ai_desc(f"Worksheet: {ws_name}. Fetch full record for project review."),
        })
        if isinstance(detail, dict):
            record = detail
            record_title = str(detail.get("title") or detail.get("name") or "")
        diag(f"S5 --row-id direct hit: rowid={row_id} title={record_title!r}")
    else:
        # 按项目名模糊搜
        def _search_rows(keyword: str) -> list[dict]:
            listing = cli.call("get_record_list", {
                "worksheet_id": ws_id,
                "pageSize": 20,
                "pageIndex": 1,
                "search": keyword,
                "appId": args.app_id,
                "ai_description": ai_desc(f"Worksheet: {ws_name}. Search for project '{keyword}'."),
            })
            rr: list[dict] = []
            if isinstance(listing, dict):
                rr = listing.get("rows") or listing.get("data") or []
            elif isinstance(listing, list):
                rr = [r for r in listing if isinstance(r, dict)]
            return rr

        rows = _search_rows(args.project)
        diag(f"S5 search={args.project!r} got {len(rows)} rows")
        for i, r in enumerate(rows[:5]):
            diag(f"  row#{i} rowid={r.get('rowid') or r.get('rowId')} title={_row_title(r)[:60]!r}")

        best = None
        # 先试精确/双向包含，匹配不上且 rows 非空时信任 HAP search 结果。
        for r in rows:
            if args.project == _row_title(r):
                best = r
                break
        if not best and rows:
            for r in rows:
                title = _row_title(r)
                if title and (args.project in title or title in args.project):
                    best = r
                    break
        # HAP 的 search 已经做了语义模糊匹配；命中行数不多时 list 返回的 title 可能为空。
        # 这种情况下直接信任 search，采纳第一条命中行；后续 get_record_details 会拿到完整记录做核对。
        if not best and rows and 1 <= len(rows) <= 5:
            best = rows[0]
            diag(f"S5 title empty but search narrowed to {len(rows)} row(s); adopting rows[0] rowid={best.get('rowid') or best.get('rowId')}")

        # 回退：切词搜索。将“股份有限公司/分公司”等停用词剔掉后，用特征关键词逐个重搜。
        if not best:
            tokens = extract_project_name_tokens(args.project)
            diag(f"S5 primary search miss; fallback tokens={tokens}")
            for tok in tokens:
                rows_t = _search_rows(tok)
                diag(f"  retry search={tok!r} got {len(rows_t)} rows")
                for i, r in enumerate(rows_t[:5]):
                    diag(f"    row#{i} rowid={r.get('rowid') or r.get('rowId')} title={_row_title(r)[:60]!r}")
                for r in rows_t:
                    title = _row_title(r)
                    if title and tok in title:
                        best = r
                        break
                if not best and rows_t and 1 <= len(rows_t) <= 5:
                    best = rows_t[0]
                    diag(f"  token {tok!r}: title empty; adopting rows_t[0] rowid={best.get('rowid') or best.get('rowId')}")
                if best:
                    rows = rows or rows_t
                    break

        if best:
            record = best
            row_id = best.get("rowId") or best.get("rowid") or best.get("id")
            record_title = str(best.get("title") or best.get("name") or "")
            # 为保险，再拉一次 full details（list 接口常常只返回部分字段）
            if row_id:
                detail = cli.call("get_record_details", {
                    "worksheet_id": ws_id,
                    "row_id": row_id,
                    "appId": args.app_id,
                    "ai_description": ai_desc(f"Worksheet: {ws_name}. Fetch full record for project review."),
                })
                if isinstance(detail, dict):
                    record = {**record, **detail}
        else:
            cli.diagnostics.append(
                f"get_record_list 按 '{args.project}' 及切词回退均未命中；首轮返回 {len(rows)} 行。"
                f"提示：请核对项目管理表里的记录 title 是否为简称（如“华北油田”），"
                f"或直接用 --row-id 跳过搜索。"
            )

    # ★ 硬停止 1：项目未在「项目管理」表登记
    if not row_id:
        print(json.dumps({
            "error": "PROJECT_NOT_FOUND_IN_PROJECT_WS",
            "message": f"项目「{args.project}」在项目管理工作表中未找到记录。ClawCRM 项目日志唯一来源 = 项目管理.日志字段；不允许从日报管理 / 沟通等其他表兜底。请先在项目管理表中登记该项目再评审。",
            "project": {
                "worksheetId": ws_id,
                "worksheetName": ws_name,
                "searchKey": args.project,
            },
            "diagnostics": cli.diagnostics,
        }, ensure_ascii=False, indent=2))
        return 3

    # S6 抽日志
    diag(f"S6 controls (name:type:alias):")
    for c in controls:
        diag(f"  {c.get('controlName')!r}:type={c.get('type')}:alias={c.get('alias','')!r}")
    logs, log_source_field = extract_logs_from_record(record, controls)
    diag(f"S6 extract_logs_from_record: {len(logs)} logs, sourceField={log_source_field!r}")

    # 兼容架构：若主表没有内嵌「日志」字段（常见），日志则存在独立工作表「项目日志」，
    # 通过 record.project[].sid == 主项目 rowId 关联。数据源纪律仍然满足：日志仅来自「项目日志」工作表，
    # 禁止庭日报管理 / 沟通记录等别的工作表。
    if not logs and row_id:
        log_ws_id = None
        log_ws_name = None
        for w in all_ws:
            if w["worksheetName"] == "项目日志":
                log_ws_id = w["worksheetId"]
                log_ws_name = w["worksheetName"]
                break
        if not log_ws_id:
            # 模糊回退：名字含「项目」且含「日志」的独立工作表
            for w in all_ws:
                n = w["worksheetName"]
                if "项目" in n and "日志" in n and w["worksheetId"] != ws_id:
                    log_ws_id = w["worksheetId"]
                    log_ws_name = n
                    break
        diag(f"S6 fallback: independent log worksheet = {log_ws_name!r} ({log_ws_id})")
        if log_ws_id:
            # 用项目名切词在项目日志工作表里 search，拿候选行
            search_terms: list[str] = []
            if record_title:
                search_terms.append(record_title)
            search_terms.extend(extract_project_name_tokens(args.project))
            if args.project:
                search_terms.append(args.project)
            # 去重，保留顺序
            seen_t: set[str] = set()
            ordered: list[str] = []
            for t in search_terms:
                if t and t not in seen_t:
                    seen_t.add(t)
                    ordered.append(t)
            candidates: list[dict] = []
            seen_ids: set[str] = set()
            for tok in ordered:
                r = cli.call("get_record_list", {
                    "worksheet_id": log_ws_id,
                    "pageSize": 100,
                    "pageIndex": 1,
                    "search": tok,
                    "appId": args.app_id,
                    "ai_description": ai_desc(
                        f"Worksheet: {log_ws_name}. Search project logs by token '{tok}' for project review."
                    ),
                })
                rs: list[dict] = []
                if isinstance(r, dict):
                    rs = r.get("rows") or r.get("data") or []
                elif isinstance(r, list):
                    rs = [x for x in r if isinstance(x, dict)]
                diag(f"  search={tok!r} -> {len(rs)} rows in {log_ws_name}")
                for it in rs:
                    iid = it.get("_id") or it.get("rowId") or it.get("rowid") or ""
                    if iid and iid not in seen_ids:
                        seen_ids.add(iid)
                        candidates.append(it)

            # 严格过滤：project[].sid == 主项目 rowId
            matched: list[dict] = []
            for c in candidates:
                proj = c.get("project")
                if not isinstance(proj, list):
                    continue
                for p in proj:
                    if isinstance(p, dict) and p.get("sid") == row_id:
                        matched.append(c)
                        break
            diag(f"S6 fallback matched {len(matched)} logs by project.sid == {row_id}")

            for m in matched:
                text_parts: list[str] = []
                if m.get("log_title"):
                    text_parts.append(str(m["log_title"]))
                if m.get("content"):
                    text_parts.append(str(m["content"]))
                log_type_val = m.get("log_type")
                if isinstance(log_type_val, list) and log_type_val and isinstance(log_type_val[0], dict):
                    text_parts.append(f"[{log_type_val[0].get('value','')}]")
                logs.append({
                    "text": " | ".join(text_parts) or json.dumps(m, ensure_ascii=False)[:500],
                    "time": m.get("_createdAt") or m.get("createTime"),
                    "source": f"{log_ws_name}(关联嵌入)",
                    "logType": (log_type_val[0].get("value") if isinstance(log_type_val, list) and log_type_val and isinstance(log_type_val[0], dict) else None),
                    "title": m.get("log_title"),
                    "rowId": m.get("rowId") or m.get("_id"),
                })
            if logs:
                log_source_field = f"{log_ws_name}(独立工作表·反向关联)"

    # ★ 硬停止 2：日志字段为空
    if not logs:
        print(json.dumps({
            "error": "EMPTY_FOLLOW_UP_LOG",
            "message": f"项目「{record_title or args.project}」已在项目管理表登记，但「日志」字段为空。ClawCRM 项目评审的唯一数据源是项目管理.日志；日志缺失时不允许评审，也不允许从其他工作表（日报 / 沟通等）拼凑数据。请补录日志后重试。",
            "project": {
                "worksheetId": ws_id,
                "worksheetName": ws_name,
                "rowId": row_id,
                "title": record_title,
            },
            "diagnostics": cli.diagnostics,
        }, ensure_ascii=False, indent=2))
        return 4

    # S7 检索知识库
    queries = build_queries(logs, record)
    all_hits: list[dict] = []
    seen_chunks: set[str] = set()
    for qname, qtext in queries.items():
        r = cli.call("knowledge_search", {
            "appId": args.app_id,
            "knowledgeIds": [args.knowledge_id],
            "query": qtext,
            "searchMode": "hybrid",
            "topK": args.topk,
        })
        chunks: list[dict] = []
        if isinstance(r, dict):
            chunks = r.get("chunks") or []
        elif isinstance(r, list):
            for it in r:
                if isinstance(it, dict):
                    chunks += it.get("chunks") or []
        for h in chunks:
            cid = h.get("chunkId")
            if cid and cid not in seen_chunks:
                seen_chunks.add(cid)
                h["_query"] = qname
                all_hits.append(h)
    # 按 score 排序留前 topk*2
    all_hits.sort(key=lambda x: x.get("score", 0), reverse=True)
    all_hits = all_hits[: args.topk * 2]

    bundle = {
        "project": {
            "worksheetId": ws_id,
            "worksheetName": ws_name,
            "rowId": row_id,
            "title": record_title,
            "fields": record,
            "followUpLogs": logs,
            "logSourceField": log_source_field,
            "structure": {"controls": controls},
            "writeBackField": writeback_field,
            "checkEvalField": {"controlId": CHECK_NEED_AI_EVAL_ID, "controlName": "是否需要AI评估"},
            "lastEvalTimeField": {"controlId": LAST_EVAL_TIME_ID, "controlName": "最后评估时间"},
        },
        "queries": queries,
        "knowledgeHits": all_hits,
        "tools": {k: tools[k] for k in (
            "update_record", "get_record_details", "get_record_list",
            "get_record_relations", "get_worksheet_structure",
            "get_app_worksheets_list", "knowledge_search", "get_app_knowledge_list",
        ) if k in tools},
        "diagnostics": cli.diagnostics,
    }
    print(json.dumps(bundle, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
