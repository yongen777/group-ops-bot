# -*- coding: utf-8 -*-
"""群绑定处理器（P0：机器人进群后由管理员「绑定」接入）。

定位（⛔ 不是业务处理器）：
- 在群授权检查**之前**由 Web 层调用 —— 未绑定群也要能回话，
  否则「进群 -> 绑定」这条第一个闭环断掉（§3.2 第 3 步之前）。
- 本处理器用回调里的**真实 group_id** 建 group_config（⛔ 不猜、不让用户手填），
  后续业务天然按 group_id 隔离（授权判定 = 有群配置且 active）。

⭐ **两轮绑定**（2026-10-16 用户指令三.4 + 2026-10-17 用户指令一）：
  第 1 轮：发「绑定」-> 鉴权通过 -> 挂 pending_session(biz=bind) -> 提示输入群名
  第 2 轮：下一条**纯文本**即群名称 -> 建群 + 五开关全关 + 记录绑定管理员
  等待期可发「取消」结束；⛔ 命令文本不作群名（明确提示，避免静默吞命令）
  ⭐ 2026-10-17 用户指令一：第一轮**只识别精确「绑定」二字**；
  ⛔ 已移除「绑定: 群名」一步到位路径 —— 群名一律等第二轮纯文本
  （群 / 实例改名请到管理后台，见 admin/api.py 更新接口）
  ⭐ 2026-10-17 用户指令六：焦点 TTL（默认 30 分钟，可配置）过期后，
  下一条非命令文本得到**明确超时提示**，⛔ 不静默失效

鉴权（⭐ 2026-10-20 用户指令：**全体群成员都可触发绑定**）：
- 群内「绑定」不再校验发送人手机号 —— 原先要求手机号在
  GOPS_BIND_ADMIN_PHONES（config.bind_admin_phones）内，用户实测「还没授权
  任何群成员为管理员，郑佳却能绑定」，据此确认期望语义是「本群任何成员都能
  接入机器人」；`bind_admin_phones` 仅作历史配置保留，⛔ 不再参与判定。
- 仍保留的护栏（与服务对象无关，任何绑定人都一样）：
  ① 已绑定 -> 明确告知已绑定，⛔ 不重复建、不改配置
  ② 实例被停用（status=DISABLED）-> 拒绝，⛔ 不借绑定命令暗中重新启用
  ③ 群标识缺失 / 群名空 / 群名超长 -> fail-closed 拒绝
  ④ 绑定成功写 `bind_admins=[发送人手机号]` + `security_event('group_bound')`
- 管理后台「发现但未绑定」一键绑定：admin 会话鉴权（app/admin/api.py）

幂等：已绑定（active）-> 明确告知已绑定，⛔ 不重复建、不改配置；
已停用 -> 指引去后台启用（⛔ 不借绑定命令暗中重新启用）。
"""

from __future__ import annotations

from typing import Optional

from .. import commands
from .base import BOT_MENTION_NAME, HandlerContext

BIZ = "bind"

BOUND_TEXT = ("绑定成功：本群已接入（群标识 {gid}）。"
              "五业务开关默认全关（fail-closed），请到管理后台按需开启；"
              "应交人员 / 字段池 / 白名单也请在后台配置。")

# ⭐ 绑定成功后附带的命令清单（2026-10-16 用户指令：绑定成功话术后面
# 加「可用命令如下」）。⛔ 与 `commands.HELP_TEXT` 的区别：本清单**不含
# 「绑定」**（刚绑定完无需再列），其余四行字面一致 —— 抽成常量由测试锁定，
# 避免两处文案各改各的。
BOUND_COMMANDS_GUIDE = ("可用命令如下：\n"
                        "「打卡」每日按群配置的时间段打卡\n"
                        "「总结」提交当日总结\n"
                        "「报数」提交当日工作报数\n"
                        "「实名」录入个人姓名与手机号至本群\n"
                        "「导出」一键导出全部业务的统计表（仅后台授权的管理员可用）")

# ⭐ 未绑定群统一引导（2026-10-19 用户指令：⛔ 不再静默）
# 正文里的 @机器人名 由 base.BOT_MENTION_NAME 单一真源注入；回推由
# sender 另外按 mention_phones 真正 @ 发送者（⛔ 不靠正文假装 @）。
# ⭐ 2026-10-20 用户指令：凡指「后台」的人一律写「后台管理员」—— 群管理员 ≠
# 后台管理员，前者在群里有管理权限但没有后台权限，写成「群管理员」会找错人。
# ⭐ 2026-10-20 用户指令（后一条，覆盖上面这条的落点）：绑定对**全体群成员**
# 开放 —— 未绑定群引导语改成「本群任一成员均可操作」。原文写「请后台管理员
# 发送」，会让普通成员以为自己不能绑，与放开后的口径矛盾。
UNBOUND_TEXT = ("本群尚未接入机器人，请发送「@%s 绑定」完成接入"
                "（本群任一成员均可操作）。" % BOT_MENTION_NAME)
# 已绑定但被停用：与「未绑定」区分（⛔ 不能骗用户去绑定，绑定也救不回停用群）
DISABLED_GROUP_TEXT = ("本群机器人已停用，请联系后台管理员在管理后台"
                       "将群状态改为启用后再使用。")


def bound_text(group_id: str) -> str:
    """绑定成功话术 = 接入说明 + 换行 + 命令清单（用户指令）。"""
    return BOUND_TEXT.format(gid=group_id) + "\n" + BOUND_COMMANDS_GUIDE
ALREADY_BOUND_TEXT = "本群已绑定，无需重复绑定（群标识 {gid}）"
DISABLED_TEXT = ("本群已被停用（群标识 {gid}）。"
                 "请在管理后台将状态改为 active 后使用，⛔ 不能通过绑定命令重新启用")
# ⭐ 2026-10-20：原 `NOT_ADMIN_TEXT`（无权执行绑定）已随手机号名单判定一起
# 移除 —— 用户确认绑定对**本群全体成员**开放（见模块 docstring 鉴权段）。
EMPTY_GROUP_TEXT = "绑定失败：回调里没有群标识"
INSTANCE_DISABLED_TEXT = ("本群机器人实例已停用，请先到管理后台启用后再绑定。")

# ---- 两轮对话话术（2026-10-16 用户指令三.4 + 2026-10-17 用户指令一/六）----
def ask_group_name_text(ttl_minutes: int) -> str:
    """第 1 轮提示（TTL 由配置注入：过期时间写进话术，用户看得见）。"""
    return ("请输入要绑定的群名称（例如：量子密信群机器人开发沟通群），"
            "发送后即完成绑定；发送「取消」可结束（%d 分钟内有效）。"
            % ttl_minutes)


CANCEL_TEXT = "取消"
EMPTY_GROUP_NAME_TEXT = "群名称不能为空，请重新发送「绑定」后输入群名称。"
TOO_LONG_GROUP_NAME_TEXT = "群名称过长（最多 64 字），请重新发送「绑定」后输入。"
CANCELED_TEXT = "已取消本次绑定，如需接入请重新发送「绑定」。"
PENDING_BUSY_TEXT = ("正在等待您输入群名称，命令类消息不作群名；"
                     "请发送群名称，或发送「取消」结束本次绑定。")
# ⭐ 2026-10-17 用户指令六：焦点过期明确告知（旧行为：过期即删 -> 落入
# 未绑定群静默分支，用户发群名却零反馈，只能反复重试）
BIND_EXPIRED_TEXT = ("绑定等待已超时（{ttl} 分钟无操作）已结束，"
                     "请重新发送「绑定」开始绑定。")

# 群名称上限（与 admin/api.py _MAX_NAME 一致）
MAX_GROUP_NAME_LEN = 64

INSTANCE_DISABLED = "DISABLED"


def handle_command(parsed, ctx: HandlerContext) -> str:
    """「绑定」入口（第 1 轮）。⛔ 不做任何业务判定，只负责群接入。

    ⭐ 2026-10-17 用户指令一：本命令**只由精确「绑定」二字触发**
    （commands.parse 已保证整串相等才 RESULT_DISPATCH）；⛔ 不存在
    「绑定: 群名」一步到位路径，群名一律等第二轮纯文本。
    """
    if not (ctx.group_id or "").strip():
        return EMPTY_GROUP_TEXT

    row = ctx.state.get_group_config(ctx.group_id)
    if row is not None:
        if not ctx.state.group_active(ctx.group_id):
            return DISABLED_TEXT.format(gid=ctx.group_id)
        # 幂等：已绑定 -> ⛔ 不进两轮（避免用户以为要再输一次群名）
        return ALREADY_BOUND_TEXT.format(gid=ctx.group_id)

    # ⭐ 2026-10-20 用户指令：**全体群成员都可触发绑定** —— 此处不再校验
    # 发送人手机号（原 `_is_bind_admin` 名单判定已移除）。
    # 机器人实例被停用时 ⛔ 不给接入（实例停用 = 后台明确关闭该群机器人）
    instance = ctx.state.get_robot_instance(ctx.group_id)
    if instance is not None and instance["status"] == INSTANCE_DISABLED:
        return INSTANCE_DISABLED_TEXT

    # ⭐ 两轮第 1 轮：挂等待会话 + 提示输入群名称（⛔ 本轮不建群）
    ctx.state.put_pending_session(ctx.group_id, ctx.phone, BIZ, None,
                                  ctx.now,
                                  ttl_minutes=ctx.config.pending_ttl_minutes)
    return ask_group_name_text(ctx.config.pending_ttl_minutes)


def _create_group(ctx: HandlerContext, group_name: str) -> str:
    """建群 + 五开关全关 + 记录绑定管理员（两轮第 2 轮专用）。"""
    ctx.state.create_group(ctx.group_id, group_name)
    # 绑定只负责「接入」：⛔ 不替管理员决定开哪些业务 —— 五开关显式全关
    # （缺键即 False 的 fail-closed 口径；后台手动建群仍默认全开，§4.7）
    ctx.state.upsert_group_config(
        ctx.group_id,
        biz_switches={k: False
                      for k in ctx.state.BIZ_SWITCH_KEYS},
        bind_admins=[ctx.phone])
    ctx.state.record_security_event(
        "group_bound", ctx.group_id, ctx.phone, "bound via chat command")
    return bound_text(ctx.group_id)


def handle_pending_text_if_any(ctx: HandlerContext) -> Optional[str]:
    """两轮绑定第 2 轮：纯文本 = 群名称 -> 建群。无等待态返回 None。

    由 Web 层在**群授权检查之前**调用（待绑群此刻仍无授权，§3.2）。

    分支（全部 fail-closed）：
    - 无等待态 -> None（交回主流程，如 unknown -> 帮助文案）
    - 等待态已过期 -> 明确超时提示（2026-10-17 用户指令六；⛔ 不静默）
    - 「取消」-> 清等待态 + 告知已取消
    - 文本命中命令 -> ⛔ 不当群名（明确提示，避免静默吞掉用户命令）
    - 空文本 -> 保留等待态，让用户重发
    - 超长群名 -> 保留等待态，让用户重发
    - 等待期已被后台绑定 -> 清等待态 + 告知已绑定
    """
    row = ctx.state.get_pending_session(ctx.group_id, ctx.phone, BIZ, ctx.now)
    text = (ctx.text or "").strip()
    if row is None:
        # ⭐ 2026-10-17 用户指令六：区分「过期」与「从未存在」——
        # 过期要给明确超时话术；⛔ 命令类消息仍走正常流程（未绑定群 = 静默），
        # ⛔ 不用超时话术吞命令；群已被（后台）绑定时也不必再提示重发「绑定」。
        expired = ctx.state.get_expired_pending_session(
            ctx.group_id, ctx.phone, BIZ, ctx.now)
        if (expired is not None
                and commands.parse(text).result != commands.RESULT_DISPATCH
                and not ctx.state.group_active(ctx.group_id)):
            return BIND_EXPIRED_TEXT.format(
                ttl=ctx.config.pending_ttl_minutes)
        return None

    if text == CANCEL_TEXT:
        ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
        return CANCELED_TEXT

    # ⛔ 命令类消息不作群名（否则用户想在两轮中插一句「打卡」会被吞掉）
    if commands.parse(text).result == commands.RESULT_DISPATCH:
        return PENDING_BUSY_TEXT

    if not text:
        # 保留等待态：让用户重新输入（⛔ 不静默失效，用户能明白发生了什么）
        return EMPTY_GROUP_NAME_TEXT
    if len(text) > MAX_GROUP_NAME_LEN:
        return TOO_LONG_GROUP_NAME_TEXT

    # ⭐ 2026-10-20：第二轮不再二次校验发送人手机号（全员可绑定）。
    # 等待期被后台一键绑定：⛔ 不重复建，告知已绑定
    if ctx.state.get_group_config(ctx.group_id) is not None:
        ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
        return ALREADY_BOUND_TEXT.format(gid=ctx.group_id)

    reply = _create_group(ctx, text)
    ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
    return reply
