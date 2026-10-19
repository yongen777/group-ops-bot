# -*- coding: utf-8 -*-
"""统计导出处理器（§4.6 / Q-BIZ3 / 2026-10-16 用户指令三.1）。

⭐ **一键导出全部业务**（2026-10-16 用户指令三.1，⛔ 推翻旧「四命令独立导」）：
- 唯一入口 = 「导出」两个字（精确匹配）；一次导出**打卡 / 总结 / 报数**
  三张表（同一个 XLSX 的三个 sheet，不再拆四个命令）
- 日报与周报**同为总结内容**，只是数据量不同：总结 sheet 按**群配置的周期**
  取数（daily -> 当日、weekly -> 当前 ISO 周），⛔ 不拆日报/周报两个命令
- 权限（2026-10-19 用户指令：单权化）：⛔ 推翻 v4 按业务三权分权——
  白名单权限只有 **ADMIN_ALL** 一种（一键 = 全部管理员操作权限），
  授权对象限本群应交成员（后台从群成员下拉选择）。具备即导，
  不具备即拒，⛔ 不做"部分导出"以免口径含糊
- 报数 sheet **动态字段**：只展示有人填的字段（按 field_id 归并），列名取
  提交时快照（字段历史快照，⛔ 不随字段池后续改名而变）
- 历史数据保留：业务开关关闭不影响导出（导的是历史账）

⚠️ 阻塞项：平台 file 消息发送能力未确认（D12）。本模块完成
「鉴权 + 查询 + 生成 XLSX + 落盘」；发送未接通时如实告知，
⛔ 不把本地文件路径当作已送达。
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from openpyxl import Workbook

from .. import commands
from .. import period as period_mod
from .base import HandlerContext, group_period_type

BIZ = "stats"

# 导出所需白名单权限（2026-10-19 用户指令：单权化，⛔ 推翻旧三权分权）
EXPORT_PERMISSION = "ADMIN_ALL"
EXPORT_LABEL = {"checkin": "打卡", "summary": "总结", "report": "报数"}

NOT_IN_WHITELIST_TEXT = "您没有导出权限，请联系管理员（导出需白名单授权）"
QUERY_FAILED_TEXT = "统计查询失败（结果未知），请稍后重试"
FILE_SEND_BLOCKED_TEXT = ("统计表已生成，但平台文件发送能力尚未接通，"
                          "暂无法在群内发送，请联系管理员")
EXPORT_SENT_TEXT = "统计表已发送到群：{name}"


def handle_command(parsed, ctx: HandlerContext) -> str:
    """「导出」入口：一键导出全部业务（打卡 / 总结 / 报数）。"""
    # ---- 权限：单权 ADMIN_ALL（2026-10-19 用户指令，⛔ fail-closed）----
    if not ctx.state.has_whitelist_permission(ctx.group_id, ctx.phone,
                                              EXPORT_PERMISSION):
        return NOT_IN_WHITELIST_TEXT

    # ---- 取数：打卡 / 报数 = 当日；总结 = 群配置周期（日报当日 / 周报本周）----
    summary_ptype = group_period_type(ctx)
    records = {}
    periods = {}
    for biz in commands.EXPORT_ALL_BIZS:
        period_k = _period_for(biz, summary_ptype, ctx.now)
        try:
            records[biz] = _query_records(ctx, biz, period_k)
        except Exception:
            # 查询类失败：⛔ 不猜数据、不生成空表冒充成功
            return QUERY_FAILED_TEXT
        periods[biz] = period_k

    workbook = build_all_export_workbook(records, periods, summary_ptype)
    out_dir = Path(ctx.config.data_dir) / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_group = re.sub(r"[^0-9A-Za-z_-]", "_", ctx.group_id)[:40]
    path = out_dir / ("export_all_%s_%s.xlsx"
                      % (safe_group,
                         period_mod.daily_key(ctx.now).replace("-", "")))
    with open(path, "wb") as fh:
        fh.write(workbook)

    # 阶段八：文件发送走平台适配器（D12 未通时 fail-closed，如实告知）
    file_sender = getattr(ctx.sender, "send_file", None)
    if file_sender is not None:
        try:
            sent, reason = file_sender(ctx.group_id, path,
                                       filename=path.name)
        except Exception as exc:  # 适配器异常不当已发送
            sent, reason = False, "file send error: %s" % exc
        if sent:
            return EXPORT_SENT_TEXT.format(name=path.name)
        return (FILE_SEND_BLOCKED_TEXT
                + "（本地路径 %s，仅开发期可见）" % path)
    # 无适配器注入（旧测试/裸调用）：与发送失败同口径
    return FILE_SEND_BLOCKED_TEXT + "（本地路径 %s，仅开发期可见）" % path


def _period_for(biz: str, summary_ptype, now) -> str:
    """导出周期：打卡 / 报数 = 当日；总结 = 群配置周期（§2.3）。"""
    if biz == "summary":
        return period_mod.period_key(summary_ptype, now)
    return period_mod.daily_key(now)


def _query_records(ctx: HandlerContext, biz: str, period_k: str) -> list:
    """按业务查一个周期的记录（⛔ 不跨业务混查）。"""
    query = {"checkin": ctx.backend.checkin_query_records,
             "summary": ctx.backend.summary_query_records,
             "report": ctx.backend.report_query_records}[biz]
    return query(group_id=ctx.group_id,
                 period_key=period_k).get("records", [])


def build_all_export_workbook(records: dict, periods: dict,
                              summary_ptype=None) -> bytes:
    """一键导出的 XLSX：三个 sheet（打卡 / 总结 / 报数）。

    records[biz] = 该业务当期记录；periods[biz] = 该业务当期周期键
    （写进文件名与群内话术的口径来源）。
    """
    wb = Workbook()
    wb.remove(wb.active)
    for biz in commands.EXPORT_ALL_BIZS:
        ws = wb.create_sheet(title=EXPORT_LABEL[biz])
        _fill_sheet(ws, biz, summary_ptype, records.get(biz) or [])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_export_workbook(biz: str, period_type, period_key: str,
                          records: list) -> bytes:
    """生成单业务 XLSX（纯函数，便于测试）。

    - checkin / summary：固定列表格
    - report：**动态字段列** —— 只展示有人填的字段（按 field_id 归并，
      列名取提交时快照；字段池后续改名/停用不影响历史导出口径）
    """
    wb = Workbook()
    ws = wb.active
    _fill_sheet(ws, biz, period_type, records)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _fill_sheet(ws, biz: str, period_type, records: list) -> None:
    """把一个业务的记录写进给定 sheet（单业务与一键导出共用 ⛔ 单一真相）。"""
    if biz == "checkin":
        ws.title = "打卡"
        ws.append(["手机号", "姓名", "时段", "打卡时间", "状态", "录入来源",
                   "图片名"])
        for rec in records:
            ws.append([rec.get("phone"), rec.get("name") or "",
                       "上午" if rec.get("session") == "am" else "下午",
                       rec.get("checkin_time") or "",
                       rec.get("status") or "", rec.get("entry_source") or "",
                       rec.get("image_name") or ""])

    elif biz == "summary":
        ws.title = "总结"
        ws.append(["手机号", "姓名", "周期类型", "内容", "录入来源",
                   "更新时间"])
        for rec in records:
            ws.append([rec.get("phone"), rec.get("name") or "",
                       rec.get("period_type") or period_type or "",
                       rec.get("content") or "",
                       rec.get("entry_source") or "",
                       rec.get("updated_time") or ""])

    else:  # report：动态字段宽表（每人一行）
        ws.title = "报数"
        columns = []          # [(field_id, 列名)]，按 field_id 升序
        seen = set()
        for rec in records:
            for item in rec.get("items") or []:
                fid = item.get("field_id")
                if fid in seen:
                    continue
                seen.add(fid)
                name = item.get("field_name") or ("字段%s" % fid)
                unit = item.get("unit")
                columns.append((fid, "%s（%s）" % (name, unit) if unit
                                else name))
        columns.sort(key=lambda c: (c[0] is None, c[0]))
        ws.append(["手机号", "姓名", "角色"] + [header for _, header
                                                in columns])
        rows = {}
        for rec in records:
            phone = rec.get("phone")
            row = rows.setdefault(phone, {"name": rec.get("name") or "",
                                          "role": rec.get("role") or "",
                                          "values": {}})
            for item in rec.get("items") or []:
                # 同字段多次出现取最后一条（防御性；一周期一次提交下不会发生）
                row["values"][item.get("field_id")] = item.get("quantity")
        for phone in sorted(rows):
            row = rows[phone]
            ws.append([phone, row["name"], row["role"]] +
                      [row["values"].get(fid, "") for fid, _ in columns])