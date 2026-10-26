# -*- coding: utf-8 -*-
"""固定命令解析（§2.1 / §4.1 / 2026-10-16 用户指令三 / 2026-10-17 用户指令一）。

⭐ **精确匹配口径**（2026-10-16 用户指令三.3，唯一入口规则）：
整串**完全等于**命令词才触发；「打卡了」「打卡 abc」「绑定: 群名」
等一切变体一律视为**无关文本** -> 回帮助文案 HELP_TEXT。⛔ 不做前缀匹配、
不做词边界猜测、不收同义词。

硬约束（不得擅自变更）：
- 入口命令五个：打卡 / 总结 / 报数 / 导出 / 实名；「导出」一键导出全部业务
  （日报与周报同为总结内容，只是数据量不同）；「实名」两轮登记真实姓名
  （手机号自动取平台注册号码，⭐ D45）
- 「绑定」是群接入命令（P0）：⛔ 不是业务命令，在群授权检查**之前**
  处理（未绑定群也要能回话），管理员鉴权在处理器内做（fail-closed）；
  **两轮**：发「绑定」-> 提示输入群名称 -> 下一条纯文本即群名完成绑定。
  ⭐ 2026-10-17 用户指令一：第一轮**只识别精确「绑定」二字**，
  ⛔ 「绑定: 群名」一步到位路径已移除（群名一律等第二轮纯文本）；
  群 / 实例改名请到管理后台（admin/api.py 更新接口）
- 「总结」**恒走两轮**（⛔ 移除一轮直接提交）：发「总结」-> 提示 ->
  下一条纯文本即总结内容；周期按群配置，不接受命令行参数
- 未命中 -> 固定帮助文本；不调 LLM、不转交任何其他路由器

归一化规则（§3.2 第 2 步 + §4.1 第 1 步）：
- 去除 @ 提及（平台把 @ 渲染进文本的多种写法都兼容）
- 全半角统一（全角空格/字母数字统一为半角）
- 去前后空白
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

CMD_CHECKIN = "打卡"
CMD_SUMMARY = "总结"
CMD_REPORT = "报数"
CMD_EXPORT = "导出"       # 一键导出全部业务（2026-10-16 用户指令三.1）
CMD_REALNAME = "实名"      # 两轮实名登记（2026-10-19 用户指令）
CMD_BIND = "绑定"          # 群接入（P0）：非业务命令，见模块头注

COMMANDS = (CMD_CHECKIN, CMD_SUMMARY, CMD_REPORT, CMD_EXPORT, CMD_REALNAME)

# 群绑定命令族（P0）：整串精确匹配；在群授权前处理
BIND_COMMANDS = (CMD_BIND,)

# ⭐ 导出 = 一键导出**全部业务**（2026-10-16 用户指令三.1）：
# 打卡 / 总结 / 报数 三张表一次导出；日报与周报同为总结内容，
# 只是数据量不同（按群配置的周期口径取数），⛔ 不再拆成四个导出命令。
EXPORT_ALL_BIZS = ("checkin", "summary", "report")

# 解析结果类型
RESULT_DISPATCH = "dispatch"        # 命中唯一命令，可分派
RESULT_CONFLICT = "conflict"        # 多命令冲突（精确匹配下不可达，保留常量）
RESULT_UNKNOWN = "unknown"          # 未命中
RESULT_BAD_FORMAT = "bad_format"    # 命中命令但格式错误（精确匹配下不可达，保留常量）

# ⭐ 帮助文案（2026-10-16 用户指令二：整体替换为标准格式，⛔ 不再自行扩写）
HELP_TEXT = (
    "未识别该指令，可用命令如下：\n"
    "「打卡」每日按群配置的时间段打卡\n"
    "「总结」提交当日总结\n"
    "「报数」提交当日工作报数\n"
    "「实名」录入个人姓名与手机号至本群\n"
    "「导出」一键导出全部业务的统计表（仅后台授权的管理员可用）\n"
    "「绑定」将本群接入机器人（本群任一成员均可操作）"
)

CONFLICT_TEXT = "一次只提交一项业务，请分开发送。"

# 各业务格式错误时的固定使用说明（§2.1 格式错误行）。
# ⚠️ 2026-10-17 起精确匹配下 RESULT_BAD_FORMAT 结构性不可达（整串不等即
# unknown）；本表作为固定文案真源保留（reply_text 防御分支与测试引用）。
USAGE_TEXT = {
    CMD_CHECKIN: "请单独发送「打卡」两个字，按提示上传一张打卡图片。",
    CMD_SUMMARY: "请单独发送「总结」两个字，发送后按提示输入总结内容。",
    CMD_REPORT: "请单独发送「报数」两个字，按提示添加字段并填写数量。",
    CMD_EXPORT: "请单独发送「导出」两个字，一键导出全部业务的统计表（需导出授权）。",
    CMD_REALNAME: "请单独发送「实名」两个字，按提示发送真实姓名即完成登记（手机号自动取注册号码）。",
    CMD_BIND: "请单独发送「绑定」两个字，机器人会提示您输入群名称（本群任一成员均可操作）。",
}

# @ 提及的多种平台写法：@某人 、@所有人 、@手机号 等。匹配 @ 后跟非空白、
# 非中文标点的一串字符（含中英文姓名）。
# ⚠️ 2026-10-16 线上实测补丁：用户真实写法「@general-bot:我完成开发」——
# @ 名后面常紧跟中英文标点（:：,，.。!！?？、），旧正则在标点处截止，
# 剥完留下「:我完成开发」直接把冒号写进业务内容。故允许 @ 名后跟随
# 「空白 + 标点 + 空白」一并替换（⛔ 只清 @ 提及**后面**的，不影响其他位置，
# 非 @ 位置的标点原样保留）。
_AT_MENTION_RE = re.compile(
    r"@[\w\u4e00-\u9fff\-]{1,32}[ \t]*[:：,，.。!！?？、;；~～]*[ \t]*")

# 判定「独立出现」的词边界：命令词前后不能紧跟其它文字（中文/字母/数字/下划线）
_WORD_CHAR_RE = re.compile(r"[\w\u4e00-\u9fff]")


@dataclass
class ParseResult:
    """解析结果。除命令外不带任何语义判断，仅供路由层分派。"""

    result: str
    command: Optional[str] = None
    arg: Optional[str] = None            # 预留参数字段（精确匹配下无命令带参）
    body: str = ""                       # 去掉命令与参数后的正文（预留）
    matched: tuple = field(default_factory=tuple)  # 命中的命令集合（冲突时用）
    raw: str = ""


def normalize_text(raw: str) -> str:
    """载荷归一化：去 @ 提及、全半角统一、去前后空白（§4.1 第 1 步）。"""
    if not raw:
        return ""
    text = _AT_MENTION_RE.sub(" ", raw)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", " ")
    return text.strip()


def _independent_occurrences(text: str, word: str) -> list:
    """返回 word 在 text 中所有「独立出现」的起始位置。

    ⚠️ **2026-10-16 起不再用于命令识别**（用户指令三.3 改为整串精确匹配），
    仅保留给「确认覆盖 / 取消」这类**精确相等**的判定之外的旧调用点排查用。
    """
    positions = []
    start = 0
    while True:
        idx = text.find(word, start)
        if idx < 0:
            return positions
        before_ok = idx == 0 or not _WORD_CHAR_RE.match(text[idx - 1])
        if before_ok:
            positions.append(idx)
        start = idx + 1


def parse(raw: str) -> ParseResult:
    """确定性解析入口（§4.1 实现流程 / ⭐ 精确匹配口径）。

    流程（2026-10-16 用户指令三.3 + 2026-10-17 用户指令一）：
    1. 归一化（去 @ 提及、全半角、去首尾空白）
    2. 空文本 -> unknown（帮助文案）
    3. 整串**完全等于**某个命令词（打卡 / 总结 / 报数 / 导出 / 绑定）
       -> dispatch
    4. 其余一切文本（含「打卡了」「绑定: 群名」等变体）
       -> unknown -> 帮助文案 HELP_TEXT
    """
    text = normalize_text(raw)
    if not text:
        return ParseResult(RESULT_UNKNOWN, raw=raw)

    # ---- ⭐ 整串精确匹配（唯一触发方式）----
    for word in COMMANDS + BIND_COMMANDS:
        if text == word:
            return ParseResult(RESULT_DISPATCH, command=word, raw=raw)

    # ---- 其余一律无关文本（⛔ 不猜、不做前缀/词边界匹配）----
    return ParseResult(RESULT_UNKNOWN, raw=raw)


def reply_text(result: ParseResult) -> str:
    """解析结果 -> 固定回复文案（唯一文案来源，不存在第二套）。"""
    if result.result == RESULT_CONFLICT:
        return CONFLICT_TEXT
    if result.result == RESULT_UNKNOWN:
        return HELP_TEXT
    if result.result == RESULT_BAD_FORMAT and result.command:
        return USAGE_TEXT[result.command]
    return ""
