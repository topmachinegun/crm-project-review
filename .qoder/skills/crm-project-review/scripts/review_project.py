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

Agent 拿到 JSON 后，按 SKILL.md §5 Rubric 生成报告；§8 Write-back。
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid
from typing import Any

DEFAULT_APP_ID = "49392ae2-6aa0-4d69-b5e7-57d4fe3fc98e"  # ClawCRM
DEFAULT_KB_ID = "69ca75132970faa5ac6ce728"  # 项目管理知识库
DEFAULT_PROJECT_WS = "69ca1fb1d128aadb0c749d49"  # 项目管理 工作表
DEFAULT_WRITEBACK_CONTROLID = "69f956419f1956fc0e1867c3"  # AI评估 字段


class MCPClient:
    def __init__(self, url: str):
        self.url = url
        self.diagnostics: list[str] = []

    def rpc(self, method: str, params: dict | None = None) -> dict:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
        if params is not None:
            body["params"] = params
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        if raw.startswith("event:") or "data:" in raw[:40]:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
        return json.loads(raw)

    def call(self, name: str, args: dict) -> Any:
        """call tool，自动剥壳返回 data 部分。失败把错误记进 diagnostics。"""
        resp = self.rpc("tools/call", {"name": name, "arguments": args})
        content = resp.get("result", {}).get("content", [])
        parsed: list[Any] = []
        for c in content:
            if c.get("type") == "text":
                t = c.get("text", "")
                try:
                    parsed.append(json.loads(t))
                except Exception:
                    parsed.append(t)
            else:
                parsed.append(c)
        # 明道云每条返回包一层 data/error_code
        for item in parsed:
            if isinstance(item, dict):
                if item.get("success") is False or item.get("error_code") not in (None, 0):
                    self.diagnostics.append(
                        f"[{name}] error_code={item.get('error_code')} msg={item.get('error_msg') or item.get('error')}"
                    )
                if "data" in item:
                    return item["data"]
        return parsed


def ai_desc(s: str) -> str:
    return s[:180]  # 防止超长


def extract_logs_from_record(record: Any, struct_controls: list[dict]) -> list[dict]:
    """从 record 里找多行文本类"跟进日志"字段，返回 [{text,time,source}]。
    对关联子表，不在此处展开（需要额外 get_record_relations 调用）。
    """
    candidates: list[dict] = []
    # 可能字段名：跟进日志 / 跟进记录 / 沟通记录 / 客户跟进 / 日志
    log_keywords = ("跟进", "日志", "沟通", "follow", "log", "记录")
    if not isinstance(record, dict):
        return []
    for ctrl in struct_controls:
        name = str(ctrl.get("controlName", ""))
        alias = str(ctrl.get("alias", ""))
        if not any(k in name.lower() or k in alias.lower() for k in log_keywords) and \
           not any(k in name for k in log_keywords):
            continue
        # 尝试多种 key 名取值
        v = record.get(alias) or record.get(ctrl.get("controlId", ""))
        if v is None:
            continue
        if isinstance(v, str) and v.strip():
            candidates.append({"text": v, "time": None, "source": name})
        elif isinstance(v, list):
            # 子表或多值
            for item in v:
                if isinstance(item, dict):
                    txt = item.get("name") or item.get("text") or json.dumps(item, ensure_ascii=False)
                    candidates.append({"text": str(txt), "time": item.get("createTime"), "source": name})
    return candidates


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
        print("ERROR: --mcp-url 或 HAP_MCP_URL 必须提供", file=sys.stderr)
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

        cli = MCPClient(args.mcp_url)
        cli.rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "crm-project-review", "version": "0.1"},
        })
        try:
            cli.rpc("notifications/initialized")
        except Exception:
            pass

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

        upd = cli.call("update_record", {
            "worksheet_id": ws_id,
            "row_id": args.row_id,
            "appId": args.app_id,
            "fields": [{"id": resolved_cid, "value": content}],
            "ai_description": ai_desc(
                f"Worksheet: 项目管理, Record: {args.row_id}. Write AI review report into AI评估 field."),
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

    cli = MCPClient(args.mcp_url)

    # S2 initialize + tools/list
    cli.rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "crm-project-review", "version": "0.1"},
    })
    try:
        cli.rpc("notifications/initialized")
    except Exception:
        pass
    tl = cli.rpc("tools/list")
    tools = {t["name"]: t.get("inputSchema", {}) for t in tl.get("result", {}).get("tools", [])}

    # S4 发现项目工作表
    ws_list = cli.call("get_app_worksheets_list", {"appId": args.app_id})
    project_ws = None

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
    if candidates:
        project_ws = candidates[0]
    else:
        cli.diagnostics.append(f"未找到含 '{args.worksheet_hint}' 的工作表；现有工作表：{[w['worksheetName'] for w in all_ws]}")

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
        if nm == args.writeback_name or al == args.writeback_alias:
            writeback_field = {"controlId": c.get("controlId"), "controlName": nm, "alias": al,
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
    else:
        # 按项目名模糊搜
        listing = cli.call("get_record_list", {
            "worksheet_id": ws_id,
            "pageSize": 20,
            "pageIndex": 1,
            "search": args.project,
            "appId": args.app_id,
            "ai_description": ai_desc(f"Worksheet: {ws_name}. Search for project '{args.project}'."),
        })
        rows: list[dict] = []
        if isinstance(listing, dict):
            rows = listing.get("rows") or listing.get("data") or []
        elif isinstance(listing, list):
            rows = [r for r in listing if isinstance(r, dict)]
        # 精确 + 模糊打分
        best = None
        for r in rows:
            title = str(r.get("title") or r.get("name") or "")
            if args.project == title:
                best = r
                break
        if not best and rows:
            for r in rows:
                title = str(r.get("title") or r.get("name") or "")
                if args.project in title:
                    best = r
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
            cli.diagnostics.append(f"get_record_list 按 '{args.project}' 未命中；返回 {len(rows)} 行")

    # S6 抽日志
    logs = extract_logs_from_record(record, controls)
    # 若主表里没抽到，尝试：找一个 type 为关联/子表 controls（常见 type 29/34/51），跑 get_record_relations
    if not logs and row_id:
        for c in controls:
            name = str(c.get("controlName", ""))
            ctype = c.get("type")
            if not any(k in name for k in ("跟进", "日志", "沟通")):
                continue
            if ctype not in (29, 34, 51, "29", "34", "51"):
                continue
            rel = cli.call("get_record_relations", {
                "worksheet_id": ws_id,
                "row_id": row_id,
                "field": c.get("alias") or c.get("controlId"),
                "pageSize": 50,
                "pageIndex": 1,
                "appId": args.app_id,
                "ai_description": ai_desc(
                    f"Worksheet: {ws_name}, Record: {record_title}, Field: {name}. Fetch related follow-up rows."
                ),
            })
            sub_rows: list[dict] = []
            if isinstance(rel, dict):
                sub_rows = rel.get("rows") or rel.get("data") or []
            elif isinstance(rel, list):
                sub_rows = [r for r in rel if isinstance(r, dict)]
            for sr in sub_rows:
                blob = sr.get("title") or sr.get("content") or sr.get("remark") or json.dumps(sr, ensure_ascii=False)
                logs.append({"text": str(blob), "time": sr.get("ctime") or sr.get("createTime"),
                             "source": name})
            if logs:
                break

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
            "structure": {"controls": controls},
            "writeBackField": writeback_field,
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
