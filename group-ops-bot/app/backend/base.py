# -*- coding: utf-8 -*-
"""机器人侧 -> MCP Server 的端口抽象（§4.8 第 2 步 / §4.11.0 两层职责边界）。

纪律（与 §4.11.0 对齐）：
- 机器人 ⛔ 不直连 MySQL；全部业务读写经本端口
- 本端口 ⛔ 不做业务资格判断（群开关 / 名单 / 字段池有效性归机器人侧）
- 调用失败一律抛 BackendError，由处理器归 C 类（§2.5.1）；
  ⛔ 适配器层不自动重试（§2.5.4：C 不自动重试、不新建请求 —— 重放由
  request_id 幂等与 C 类恢复流程负责，⛔ 不靠盲目重发）
- 方法名 / 参数与 §4.11.1 工具契约一一对应；改契约必须同步本文件与文档
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class BackendError(RuntimeError):
    """MCP 调用失败（连接 / 协议 / JSON-RPC 错误 / 响应超时）。

    出现即表示「结果未知」—— 调用方按 C 类处理（§2.5.1）：
    挂恢复任务 + op.query_by_request 查证，⛔ 不当成功也不当失败。
    """


class Backend(ABC):
    """MCP Server 工具面（§4.11.1 中机器人需要调用的部分）。"""

    # ---------- 人员目录 ----------

    @abstractmethod
    def user_upsert(self, *, phone: str, name: Optional[str],
                    platform_user_id: Optional[str],
                    department: Optional[str] = None,
                    source: str = "learned") -> dict:
        """按手机号 upsert（幂等）。返回 {"user_id": int, "created": bool}。"""

    @abstractmethod
    def user_get(self, *, phone: str) -> dict:
        """按手机号查人员（只读）。返回 {"user": dict|None}。"""

    # ---------- 打卡 ----------

    @abstractmethod
    def checkin_submit(self, *, user_id: int, group_id: str, period_key: str,
                       session: str, image_ref: str,
                       image_meta: Optional[dict], request_id: str,
                       status: str = "NORMAL",
                       entry_source: str = "user") -> dict:
        """checkin.submit。返回 {status, record_id, duplicate, message}。

        status: NORMAL/LATE/ADMIN_EDIT（机器人侧按截止与来源计算，MCP 只存）；
        entry_source: user/admin（后台补录追溯，§用户指令第二阶段）。
        """

    @abstractmethod
    def checkin_query_status(self, *, user_id: int, group_id: str,
                             period_key: str, session: str) -> dict:
        """checkin.query_status（只读，提醒判定用）。"""

    @abstractmethod
    def checkin_query_records(self, *, group_id: str,
                              period_key: str) -> dict:
        """checkin.query_records（只读，统计导出用）。"""

    # ---------- 总结 ----------

    @abstractmethod
    def summary_save(self, *, user_id: int, group_id: str, period_key: str,
                     period_type: str, content: str, request_id: str,
                     entry_source: str = "user") -> dict:
        """summary.save。覆盖语义：同范围键非同一请求 -> UPDATE（§2.5.3）。

        entry_source: user/admin（后台补录追溯）。
        """

    @abstractmethod
    def summary_query_status(self, *, user_id: int, group_id: str,
                             period_key: str) -> dict:
        """summary.query_status（只读）。"""

    @abstractmethod
    def summary_query_records(self, *, group_id: str,
                              period_key: str) -> dict:
        """summary.query_records（只读）。"""

    # ---------- 报数 ----------

    @abstractmethod
    def report_submit(self, *, user_id: int, group_id: str, period_key: str,
                      role: Optional[str], items: list,
                      request_id: str) -> dict:
        """report.submit。items: [{field_id, field_name, quantity, unit?}]。"""

    @abstractmethod
    def report_query_status(self, *, user_id: int, group_id: str,
                            period_key: str) -> dict:
        """report.query_status（只读）。"""

    @abstractmethod
    def report_query_records(self, *, group_id: str,
                             period_key: str) -> dict:
        """report.query_records（只读）。"""

    # ---------- 运维查证 ----------

    @abstractmethod
    def op_query_by_request(self, *, request_id: str) -> dict:
        """op.query_by_request —— C 类恢复查证的唯一入口（§4.11.3 第 6 条）。"""

    def close(self) -> None:  # pragma: no cover - 默认无需清理
        """释放资源（默认无操作）。"""
