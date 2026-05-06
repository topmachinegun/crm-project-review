#!/usr/bin/env python3
"""review_project.py — ClawCRM 项目评审数据采集管道（v0.3.0）。

用法：
    python3 review_project.py --project "XYZ客户"              # 或 --row-id <ROW_ID>
    python3 review_project.py --row-id <ROW_ID> --writeback-file report.md
    [--profile claw-crm] [--knowledge-id <KB_ID>]
    [--worksheet-hint 项目] [--log-field-hint 跟进]
    [--topk 8]

架构（v0.3.0 架构重构后）：
    业务 skill 不再关心传输层。所有对明道 HAP 的访问（Personal MCP /
    应用级 MCP / V3 REST）通过 `hap-access` CLI 统一调度，它负责：
      - 根据 profile 选择 mode（personal_mcp / app_mcp / v3_api）
      - Personal MCP 自动注入 appId + ai_description
      - app_mcp 自动拼 HAP-Appkey + HAP-Sign
      - v3_api 自动映射工具名 → REST 端点
    本脚本只调 `hap_call(tool, args)` / `hap_list_tools()`，args 里不再
    传 appId / ai_description / appkey / sign / mcp_url。

Profile：
    默认 `claw-crm`，可用 `--profile` 或 env `HAP_ACCESS_PROFILE` 覆盖。
    profile 文件由 hap-app-access 维护（见其 SKILL.md）；本脚本假设
    “应用访问通道都是正常的”，只做业务流程。

hap-access 可执行文件定位顺序：
    1. env `HAP_ACCESS_BIN`
    2. `$PATH` 里的 `hap-access`
    3. `~/Desktop/hap-app-access/scripts/hap-access`（开发环境常见位置）
    4. `/opt/hap-app-access/scripts/hap-access`（安装环境）

输出：stdout 一个 JSON bundle，结构：
    {
      "project": {
        "worksheetId": "...", "worksheetName": "...",
        "rowId": "...", "title": "...",
        "fields": { ...normalized record... },
        "followUpLogs": [ {text, time, source} ... ],
        "structure": { "controls":[{controlId, controlName, type, alias}, ...] },
        "writeBackField": {"controlId": "...", "controlName": "...", "alias": "..."}
      },
      "knowledgeHits": [ {chunkId, content, score, ...} ... ],
      "tools": [ "<name>", ... ],   # 仅暴露名字；schema 由 hap-access 服务端把关
      "diagnostics": [ ... ]
    }
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


# ------- 业务坐标（不涉及凭据，留在业务 skill 是合理的） -------
DEFAULT_KB_ID = "69ca75132970faa5ac6ce728"           # 项目管理知识库
DEFAULT_PROJECT_WS = "69ca1fb1d128aadb0c749d49"      # 项目管理 工作表
DEFAULT_WRITEBACK_CONTROLID = "69f956419f1956fc0e1867c3"  # AI评估 字段 controlId


# ------- 诊断 -------
_DIAGNOSTICS: list[str] = []


def diag(msg: str) -> None:
    """诊断日志统一打到 stderr，不污染 stdout JSON bundle。"""
    print(f"[diag] {msg}", file=sys.stderr, flush=True)


# ------- hap-access CLI 封装 -------
class HapCallError(RuntimeError):
    """hap-access 返回 ok=false 或 subprocess 失败时抛出。"""


def resolve_hap_access_bin() -> str:
    bin_env = os.environ.get("HAP_ACCESS_BIN", "").strip()
    if bin_env:
        p = Path(bin_env).expanduser()
        if p.exists():
            return str(p)
        raise HapCallError(f"HAP_ACCESS_BIN={bin_env} 指向的文件不存在")
    on_path = shutil.which("hap-access")
    if on_path:
        return on_path
    for cand in (
        Path.home() / "Desktop" / "hap-app-access" / "scripts" / "hap-access",
        Path("/opt/hap-app-access/scripts/hap-access"),
    ):
        if cand.exists():
            return str(cand)
    raise HapCallError(
        "找不到 hap-access CLI。请先安装 hap-app-access skill（见其 SKILL.md），"
        "或设置 env HAP_ACCESS_BIN 指向 scripts/hap-access。"
    )


def _run_hap_access(cmd: list[str]) -> dict:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as e:
        raise HapCallError(f"hap-access 调用超时：{' '.join(cmd)} ({e})")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        # 尽量给出结构化错误，便于上游 diag
        try:
            payload = json.loads(stdout) if stdout else {}
        except Exception:
            payload = {}
        msg = payload.get("error") or stderr or f"exit={proc.returncode}"
        raise HapCallError(f"hap-access 调用失败：{msg}")
    try:
        return json.loads(proc.stdout)
    except Exception as e:
        raise HapCallError(f"hap-access 返回非 JSON：{proc.stdout[:200]!r} ({e})")


def hap_call(profile: str, tool: str, args: dict, *, bin_path: str | None = None, app: str | None = None) -> Any:
    """调用 `hap-access call`，返回剥壳后的 data 层；失败抛 HapCallError。

    业务 skill 不再传 appId / ai_description / appkey / sign / mcp_url——
    这些由 hap-access 按 profile 或 --app 自动注入。
    v1.7+: 若传 app 参数，优先使用 --app（从统一配置解析），忽略 profile。
    """
    bin_path = bin_path or resolve_hap_access_bin()
    if app:
        cmd = [
            bin_path, "call",
            "--app", app,
            "--tool", tool,
            "--args", json.dumps(args, ensure_ascii=False),
        ]
    else:
        cmd = [
            bin_path, "call",
            "--profile", profile,
            "--tool", tool,
            "--args", json.dumps(args, ensure_ascii=False),
        ]
    payload = _run_hap_access(cmd)
    # 合并底层诊断
    for d in payload.get("diagnostics") or []:
        _DIAGNOSTICS.append(f"[{tool}] {d}")
    if not payload.get("ok"):
        err = payload.get("error") or "unknown"
        _DIAGNOSTICS.append(f"[{tool}] error={err}")
        raise HapCallError(f"{tool}: {err}")
    return payload.get("data")


def hap_list_tools(profile: str, *, bin_path: str | None = None, app: str | None = None) -> list[str]:
    """调用 `hap-access list-tools`，返回工具名列表。"""
    bin_path = bin_path or resolve_hap_access_bin()
    if app:
        cmd = [bin_path, "list-tools", "--app", app]
    else:
        cmd = [bin_path, "list-tools", "--profile", profile]
    payload = _run_hap_access(cmd)
    for d in payload.get("diagnostics") or []:
        _DIAGNOSTICS.append(f"[list-tools] {d}")
    if not payload.get("ok"):
        err = payload.get("error") or "unknown"
        raise HapCallError(f"list-tools: {err}")
    data = payload.get("data") or []
    # 兼容 [name, ...] 或 [{"name":...}, ...]
    names: list[str] = []
    for it in data:
        if isinstance(it, str):
            names.append(it)
        elif isinstance(it, dict) and it.get("name"):
            names.append(str(it["name"]))
    return names


# ------- 业务工具 -------
def _row_title(r: dict) -> str:
    return str(r.get("title") or r.get("name") or "")


# 去除中文公司名常见后缀/限定词，切出特征关键词。
_COMPANY_STOPWORDS = (
    "股份有限公司", "有限责任公司", "有限公司",
    "分公司", "子公司", "集团", "公司",
)


def extract_project_name_tokens(name: str) -> list[str]:
    """从项目名抽特征关键词。

    "中国石油天然气股份有限公司华北油田分公司"
      → ["华北油田", "中国石油天然气"]  # 长度优先给尾部（地区/业务特征）
    """
    stem = name or ""
    for w in _COMPANY_STOPWORDS:
        stem = stem.replace(w, " ")
    parts = re.split(r"[\s\-\u3001\u3002\uff0c\uff0c,\-\(\)\uff08\uff09]+", stem)
    tokens = [t.strip() for t in parts if t and len(t.strip()) >= 2]
    tokens.reverse()
    seen: set[str] = set()
    uniq: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def extract_logs_from_record(record: Any, struct_controls: list[dict]) -> tuple[list[dict], str | None]:
    """仅从项目主表记录里提取字段名含「日志」的字段，返回 (日志列表, 字段名)。

    数据源纪律：ClawCRM 项目日志唯一来源 = 「项目管理」工作表下名字含「日志」的字段。
    禁止扩展到 日报 / 沟通记录 / follow / log 等其他语义，以免引入外表数据。
    对关联子表，本函数不展开（需额外 get_record_relations）。
    """
    candidates: list[dict] = []
    hit_field: str | None = None
    log_keywords = ("日志",)
    if not isinstance(record, dict):
        return [], None
    for ctrl in struct_controls:
        name = str(ctrl.get("controlName", ""))
        alias = str(ctrl.get("alias", ""))
        if not any(k in name for k in log_keywords) and \
           not any(k in alias.lower() for k in log_keywords):
            continue
        v = record.get(alias) or record.get(ctrl.get("controlId", ""))
        if v is None:
            continue
        if isinstance(v, str) and v.strip():
            candidates.append({"text": v, "time": None, "source": name})
            hit_field = hit_field or name
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    txt = item.get("name") or item.get("text") or json.dumps(item, ensure_ascii=False)
                    candidates.append({"text": str(txt), "time": item.get("createTime"), "source": name})
            if candidates:
                hit_field = hit_field or name
    return candidates, hit_field


def build_queries(logs: list[dict], record: dict) -> dict[str, str]:
    """基于日志和记录字段构造 3 个查询词。"""
    log_blob = " ".join((l.get("text") or "") for l in logs)[-1500:]
    customer = str(record.get("title") or record.get("name") or record.get("客户名") or "")

    stage_keywords = []
    for kw in ("演示", "POC", "报价", "签约", "合同", "交付", "提案", "选型", "微信", "电话", "会议"):
        if kw in log_blob:
            stage_keywords.append(kw)
    query_stage = "销售阶段 " + " ".join(stage_keywords[:5]) if stage_keywords else "销售阶段 跟进"

    risk_signals = []
    for sig in ("预算", "决策", "竞品", "暂缓", "搁置", "下次", "等通知", "暂时"):
        if sig in log_blob:
            risk_signals.append(sig)
    query_risks = "风险 停滞 " + " ".join(risk_signals[:5]) if risk_signals else "客户流失 风险"

    query_icp = f"理想客户画像 ICP {customer}".strip()

    return {
        "query_stage": query_stage,
        "query_risks": query_risks,
        "query_icp": query_icp,
    }


# ------- 主流程 -------
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--profile",
                   default=os.environ.get("HAP_ACCESS_PROFILE", "claw-crm"),
                   help="hap-access profile 名（默认 claw-crm，env HAP_ACCESS_PROFILE 覆盖）")
    p.add_argument("--project", help="项目名（用于 get_record_list search 过滤）")
    p.add_argument("--row-id", help="项目记录 rowId（优先于 --project）")
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

    # 提前定位 hap-access，失败直接退出
    try:
        bin_path = resolve_hap_access_bin()
    except HapCallError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    diag(f"S1 hap-access bin={bin_path} profile={args.profile}")

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

        ws_id = args.writeback_worksheet
        control_id = args.writeback_controlid

        # 调用前校验：查 structure 对齐 controlId（仅作为 warning，不阻止写入）
        # 原因：AI评估 controlId 已硬编码在 SKILL.md 和本脚本中作为铁律坐标；
        # structure 返回假阴性不命中（权限过滤/字段排序）不代表“字段不存在”。
        # 最终以 update_record 本身的返回为准。
        try:
            struct = hap_call(args.profile, "get_worksheet_structure", {
                "worksheet_id": ws_id,
                "responseFormat": "json",
            }, bin_path=bin_path)
        except HapCallError as e:
            diag(f"get_worksheet_structure 失败（不阻塞写回）：{e}")
            struct = None

        fields = (struct or {}).get("fields", []) if isinstance(struct, dict) else []
        # 兼容两种 structure shape：{fields:[...]} 或 {controls:[...]}
        if not fields and isinstance(struct, dict):
            fields = struct.get("controls", []) or []

        match = None
        for f in fields:
            nm = str(f.get("name") or f.get("controlName", ""))
            al = str(f.get("alias", ""))
            if (f.get("controlId") or f.get("id")) == control_id \
               or nm == args.writeback_name \
               or al == args.writeback_alias:
                match = f
                break
        struct_verified = match is not None
        if not struct_verified:
            diag(
                f"AI评估 字段未在 structure 返回里命中（controlId={control_id}）；"
                f"它可能是权限过滤或结构排序问题，仍会用硬编码 controlId 继续写入。"
            )

        resolved_cid = (
            (match.get("controlId") or match.get("id") or control_id) if match else control_id
        )

        # 业务 skill 只表达 fields 语义；MCP vs V3 的 fields/controls 差异由
        # hap-access api_client 吸收。
        try:
            upd = hap_call(args.profile, "update_record", {
                "worksheet_id": ws_id,
                "row_id": args.row_id,
                "fields": [{"id": resolved_cid, "value": content}],
            }, bin_path=bin_path)
        except HapCallError as e:
            print(json.dumps({
                "ok": False,
                "error": str(e),
                "worksheetId": ws_id,
                "rowId": args.row_id,
                "controlId": resolved_cid,
                "diagnostics": _DIAGNOSTICS,
            }, ensure_ascii=False, indent=2))
            return 1

        # 成功判定：MCP 返回 rowId 字符串，V3 返回 dict/success
        if isinstance(upd, str):
            ok = upd == args.row_id
        elif isinstance(upd, dict):
            ok = upd.get("success") is not False and upd.get("error_code") in (None, 0)
        elif upd in (True, 1):
            ok = True
        else:
            ok = False

        print(json.dumps({
            "ok": ok,
            "worksheetId": ws_id,
            "rowId": args.row_id,
            "controlId": resolved_cid,
            "fieldName": (match.get("name") or match.get("controlName")) if match else args.writeback_name,
            "structureVerified": struct_verified,
            "charsWritten": len(content),
            "response": upd,
            "diagnostics": _DIAGNOSTICS,
        }, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    # ========== 写回模式结束 ==========

    if not (args.project or args.row_id):
        print("ERROR: 必须提供 --project 或 --row-id", file=sys.stderr)
        return 2

    # S2 tools/list （仅用于展示给 agent；schema 校验由 hap-access + 服务端把关）
    try:
        tool_names = hap_list_tools(args.profile, bin_path=bin_path)
    except HapCallError as e:
        diag(f"list-tools 失败（不阻塞主流程）：{e}")
        tool_names = []

    # S4 发现项目工作表
    try:
        ws_list = hap_call(args.profile, "get_app_worksheets_list", {}, bin_path=bin_path)
    except HapCallError as e:
        print(f"ERROR: get_app_worksheets_list 失败：{e}", file=sys.stderr)
        return 2
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
    candidates = [w for w in all_ws if args.worksheet_hint in w["worksheetName"]
                  and not any(k in w["worksheetName"] for k in ("跟进", "日志", "任务", "汇报"))]
    if not candidates and all_ws:
        candidates = [w for w in all_ws if args.worksheet_hint in w["worksheetName"]]
    exact = [w for w in candidates if w["worksheetName"] == "项目管理"]
    if exact:
        project_ws = exact[0]
    elif candidates:
        project_ws = candidates[0]
    else:
        _DIAGNOSTICS.append(
            f"未找到含 '{args.worksheet_hint}' 的工作表；现有工作表：{[w['worksheetName'] for w in all_ws]}"
        )

    diag(f"S4 allWorksheets = {[w['worksheetName'] for w in all_ws]}")
    if project_ws:
        diag(f"S4 picked projectWorksheet = {project_ws['worksheetName']} ({project_ws['worksheetId']})")

    if not project_ws:
        print(json.dumps({
            "project": None, "knowledgeHits": [], "tools": tool_names,
            "diagnostics": _DIAGNOSTICS, "allWorksheets": all_ws,
        }, ensure_ascii=False, indent=2))
        return 1

    ws_id = project_ws["worksheetId"]
    ws_name = project_ws["worksheetName"]

    # 获取工作表结构
    try:
        struct = hap_call(args.profile, "get_worksheet_structure", {
            "worksheet_id": ws_id,
        }, bin_path=bin_path)
    except HapCallError as e:
        diag(f"get_worksheet_structure 失败：{e}")
        struct = None

    controls: list[dict] = []
    if isinstance(struct, dict):
        controls = struct.get("controls") or struct.get("fields") or []
    elif isinstance(struct, list):
        for it in struct:
            if isinstance(it, dict) and "controls" in it:
                controls = it["controls"]
                break

    writeback_field = None
    for c in controls:
        nm = str(c.get("controlName") or c.get("name", ""))
        al = str(c.get("alias", ""))
        if nm == args.writeback_name or al == args.writeback_alias:
            writeback_field = {
                "controlId": c.get("controlId") or c.get("id"),
                "controlName": nm, "alias": al, "type": c.get("type"),
            }
            break

    # S5 定位 row
    row_id = args.row_id
    record: dict = {}
    record_title = ""

    if row_id:
        try:
            detail = hap_call(args.profile, "get_record_details", {
                "worksheet_id": ws_id,
                "row_id": row_id,
            }, bin_path=bin_path)
        except HapCallError as e:
            print(f"ERROR: get_record_details 失败：{e}", file=sys.stderr)
            return 2
        if isinstance(detail, dict):
            record = detail
            record_title = str(detail.get("title") or detail.get("name") or "")
        diag(f"S5 --row-id direct hit: rowid={row_id} title={record_title!r}")
    else:
        def _search_rows(keyword: str) -> list[dict]:
            try:
                listing = hap_call(args.profile, "get_record_list", {
                    "worksheet_id": ws_id,
                    "pageSize": 20,
                    "pageIndex": 1,
                    "search": keyword,
                }, bin_path=bin_path)
            except HapCallError as e:
                diag(f"get_record_list search={keyword!r} 失败：{e}")
                return []
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
        if not best and rows and 1 <= len(rows) <= 5:
            best = rows[0]
            diag(f"S5 title empty but search narrowed to {len(rows)} row(s); adopting rows[0] rowid={best.get('rowid') or best.get('rowId')}")

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
            if row_id:
                try:
                    detail = hap_call(args.profile, "get_record_details", {
                        "worksheet_id": ws_id,
                        "row_id": row_id,
                    }, bin_path=bin_path)
                except HapCallError as e:
                    diag(f"get_record_details fallback 失败：{e}")
                    detail = None
                if isinstance(detail, dict):
                    record = {**record, **detail}
        else:
            _DIAGNOSTICS.append(
                f"get_record_list 按 '{args.project}' 及切词回退均未命中；首轮返回 {len(rows)} 行。"
                f"提示：请核对项目管理表里的记录 title 是否为简称（如\"华北油田\"），"
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
            "diagnostics": _DIAGNOSTICS,
        }, ensure_ascii=False, indent=2))
        return 3

    # S6 抽日志
    diag("S6 controls (name:type:alias):")
    for c in controls:
        diag(f"  {c.get('controlName')!r}:type={c.get('type')}:alias={c.get('alias','')!r}")
    logs, log_source_field = extract_logs_from_record(record, controls)
    diag(f"S6 extract_logs_from_record: {len(logs)} logs, sourceField={log_source_field!r}")

    # 兼容架构：若主表无内嵌「日志」字段，日志存在独立工作表「项目日志」，
    # 通过 record.project[].sid == 主项目 rowId 关联。数据源纪律仍然满足：
    # 日志仅来自「项目日志」工作表，禁止从日报 / 沟通兜底。
    if not logs and row_id:
        log_ws_id = None
        log_ws_name = None
        for w in all_ws:
            if w["worksheetName"] == "项目日志":
                log_ws_id = w["worksheetId"]
                log_ws_name = w["worksheetName"]
                break
        if not log_ws_id:
            for w in all_ws:
                n = w["worksheetName"]
                if "项目" in n and "日志" in n and w["worksheetId"] != ws_id:
                    log_ws_id = w["worksheetId"]
                    log_ws_name = n
                    break
        diag(f"S6 fallback: independent log worksheet = {log_ws_name!r} ({log_ws_id})")
        if log_ws_id:
            search_terms: list[str] = []
            if record_title:
                search_terms.append(record_title)
            search_terms.extend(extract_project_name_tokens(args.project))
            if args.project:
                search_terms.append(args.project)
            seen_t: set[str] = set()
            ordered: list[str] = []
            for t in search_terms:
                if t and t not in seen_t:
                    seen_t.add(t)
                    ordered.append(t)
            candidates: list[dict] = []
            seen_ids: set[str] = set()
            for tok in ordered:
                try:
                    r = hap_call(args.profile, "get_record_list", {
                        "worksheet_id": log_ws_id,
                        "pageSize": 100,
                        "pageIndex": 1,
                        "search": tok,
                    }, bin_path=bin_path)
                except HapCallError as e:
                    diag(f"log search={tok!r} 失败：{e}")
                    continue
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
                for pp in proj:
                    if isinstance(pp, dict) and pp.get("sid") == row_id:
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
            "diagnostics": _DIAGNOSTICS,
        }, ensure_ascii=False, indent=2))
        return 4

    # S7 检索知识库
    queries = build_queries(logs, record)
    all_hits: list[dict] = []
    seen_chunks: set[str] = set()
    for qname, qtext in queries.items():
        try:
            r = hap_call(args.profile, "knowledge_search", {
                "knowledgeIds": [args.knowledge_id],
                "query": qtext,
                "searchMode": "hybrid",
                "topK": args.topk,
            }, bin_path=bin_path)
        except HapCallError as e:
            diag(f"knowledge_search {qname!r} 失败：{e}")
            continue
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
        },
        "queries": queries,
        "knowledgeHits": all_hits,
        "tools": tool_names,
        "diagnostics": _DIAGNOSTICS,
    }
    print(json.dumps(bundle, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
