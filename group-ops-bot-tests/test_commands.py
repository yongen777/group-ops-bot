# -*- coding: utf-8 -*-
"""命令解析测试（⭐ 2026-10-16 精确匹配口径：整串完全相等才触发；
2026-10-17 用户指令一：「绑定: 群名」一步到位路径移除，绑定只认两字）。

§4.1 完成标准：五个命令的精确匹配 + 变体归 help + 新帮助文案全部有测试。
"""

from app import commands
from app.commands import (
    CMD_BIND, CMD_CHECKIN, CMD_EXPORT, CMD_REPORT, CMD_SUMMARY,
    RESULT_DISPATCH, RESULT_UNKNOWN,
    parse,
)


class TestNormalize:
    def test_strip_at_mention(self):
        assert commands.normalize_text("@张三 打卡") == "打卡"
        assert commands.normalize_text("@所有人 打卡") == "打卡"
        assert commands.normalize_text("@13800138000 打卡") == "打卡"

    def test_fullwidth_and_space(self):
        assert commands.normalize_text("　打卡　") == "打卡"
        assert commands.normalize_text("  打卡  ") == "打卡"

    def test_strip_punctuation_after_at_mention(self):
        # 2026-10-16 线上实测：用户写「@general-bot:我完成开发」，
        # 旧正则在 @ 名后截止，冒号残进正文（MySQL 实证「:我完成开发」）。
        assert commands.normalize_text("@general-bot:我完成开发") == "我完成开发"
        assert commands.normalize_text("@general-bot：我完成开发") == "我完成开发"
        assert commands.normalize_text("@bot，打卡") == "打卡"
        assert commands.normalize_text("@bot。 打卡") == "打卡"
        # ⛔ 只清 @ 提及后面：非 @ 位置的标点原样保留（含已废弃的
        # 「绑定: 群名」写法里的冒号 —— 它现在是普通文本，不是命令）
        assert commands.normalize_text("绑定: 群名") == "绑定: 群名"
        assert commands.normalize_text("总结: 今日完成X") == "总结: 今日完成X"


class TestExactMatch:
    """⭐ 五命令整串精确匹配（用户指令三.3）。"""

    def test_five_commands_dispatch(self):
        for cmd in (CMD_CHECKIN, CMD_SUMMARY, CMD_REPORT, CMD_EXPORT,
                    CMD_BIND):
            result = parse(cmd)
            assert result.result == RESULT_DISPATCH, cmd
            assert result.command == cmd

    def test_with_mention_and_spaces_still_exact(self):
        # @ 提及与首尾空白归一化后仍应精确命中
        result = parse("@general-bot  打卡 ")
        assert result.result == RESULT_DISPATCH
        assert result.command == CMD_CHECKIN

    def test_extra_characters_are_unknown(self):
        # ⛔ 多一个字 / 少一个字 / 差一点 —— 一律无关文本 -> 帮助文案
        for text in ("打卡了", "打卡abc", "打", "打卡卡", "帮我打卡",
                     "打卡 上午", "打卡一下", "打卡。"):
            result = parse(text)
            assert result.result == RESULT_UNKNOWN, text
            assert commands.reply_text(result) == commands.HELP_TEXT, text

    def test_command_embedded_in_sentence_is_unknown(self):
        for text in ("今天你打卡了吗", "请 打卡", "我不打卡", "总结今天完成X",
                     "报数 3 个", "导出统计", "汇总", "统计", "绑定群"):
            assert parse(text).result == RESULT_UNKNOWN, text

    def test_summary_params_removed(self):
        # ⛔ 「总结 日报」「总结 周报」不再作为参数化命令（用户指令三.3）
        for text in ("总结 日报", "总结 周报", "总结 日报：修完上传"):
            assert parse(text).result == RESULT_UNKNOWN, text

    def test_export_single_command_only(self):
        # ⛔ 旧四导出命令（导出打卡/导出日报/导出周报/导出报数）已废：
        # 「导出打卡」不等于「导出」-> 无关文本
        for text in ("导出打卡", "导出日报", "导出周报", "导出报数"):
            assert parse(text).result == RESULT_UNKNOWN, text

    def test_stats_command_removed(self):
        # 「统计」已由「导出」取代（用户指令二的标准文案只有五个命令）
        assert parse("统计").result == RESULT_UNKNOWN


class TestBindNameFormRemoved:
    """⭐ 2026-10-17 用户指令一：「绑定: 群名」一步到位路径**已移除**。

    绑定第一轮只识别精确「绑定」二字；群名一律等第二轮纯文本。
    「绑定: 任意内容」整串不等于任何命令 -> 无关文本 -> 帮助文案。
    """

    def test_bind_colon_forms_are_unknown(self):
        for text in ("绑定: 量子密信群", "绑定：量子密信群", "绑定: 打卡",
                     "绑定: A群 打卡", "绑定:", "绑定：", "绑定: " + "长" * 65,
                     "@机器人 绑定: 巡检A组"):
            result = parse(text)
            assert result.result == RESULT_UNKNOWN, text
            assert result.command is None, text
            assert commands.reply_text(result) == commands.HELP_TEXT, text

    def test_exact_bind_still_dispatches(self):
        # ⛔ 只剥掉「绑定: 群名」，精确「绑定」不受影响
        result = parse("@general-bot  绑定 ")
        assert (result.result, result.command) == (RESULT_DISPATCH, CMD_BIND)

    def test_no_bind_name_constant_remains(self):
        # 真源护栏：命令层不得再保留「绑定:」命令常量（防回退）
        assert not hasattr(commands, "CMD_BIND_NAME")


class TestUnknownAndHelp:
    def test_empty_text(self):
        assert parse("").result == RESULT_UNKNOWN
        assert parse("   ").result == RESULT_UNKNOWN

    def test_only_at_mention(self):
        assert parse("@general-bot").result == RESULT_UNKNOWN

    def test_unknown_command(self):
        assert parse("你好").result == RESULT_UNKNOWN
        assert parse("帮助").result == RESULT_UNKNOWN

    def test_help_text_is_standard_2026_10_16(self):
        """⭐ 帮助文案整体替换为用户给定的标准格式（⛔ 逐行锁定，防回退）。

        2026-10-19 用户指令：新增「实名」一行（共 6 项命令）。
        """
        expected = (
            "未识别该指令，可用命令如下：\n"
            "「打卡」每日按群配置的时间段打卡\n"
            "「总结」提交当日总结\n"
            "「报数」提交当日工作报数\n"
            "「实名」录入个人姓名与手机号至本群\n"
            "「导出」一键导出全部业务的统计表（仅后台授权的管理员可用）\n"
            "「绑定」将本群接入机器人（仅后台授权的管理员可用）")
        assert commands.HELP_TEXT == expected
        assert commands.reply_text(parse("你好")) == expected

    def test_help_text_lists_all_six_commands(self):
        for word in ("「打卡」", "「总结」", "「报数」", "「实名」",
                     "「导出」", "「绑定」"):
            assert word in commands.HELP_TEXT


class TestUsageText:
    def test_all_commands_have_usage(self):
        for cmd in (CMD_CHECKIN, CMD_SUMMARY, CMD_REPORT, CMD_EXPORT,
                    CMD_BIND):
            assert cmd in commands.USAGE_TEXT
            assert commands.USAGE_TEXT[cmd]

    def test_usage_mentions_exact_two_char_send(self):
        # 用法话术要教用户「单独发送两个字」
        for cmd in (CMD_CHECKIN, CMD_SUMMARY, CMD_REPORT, CMD_EXPORT,
                    CMD_BIND):
            assert "单独发送" in commands.USAGE_TEXT[cmd], cmd


class TestDeterminism:
    def test_same_input_same_result(self):
        for text in ("打卡", "打卡了", "总结", "导出", "绑定: X", "你好"):
            first = parse(text)
            for _ in range(3):
                again = parse(text)
                assert (again.result, again.command) == (first.result,
                                                        first.command), text

    def test_two_commands_in_one_message_is_unknown(self):
        # ⛔ 精确匹配下「多命令冲突」不再存在：整串不等于任何命令 -> 无关文本
        for text in ("打卡 总结", "总结 打卡 报数", "导出 打卡"):
            result = parse(text)
            assert result.result == RESULT_UNKNOWN, text
            assert commands.reply_text(result) == commands.HELP_TEXT, text