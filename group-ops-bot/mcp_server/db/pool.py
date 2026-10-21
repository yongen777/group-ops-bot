# -*- coding: utf-8 -*-
"""MySQL 连接池（MCP Server 侧；机器人⛔ 不直连 MySQL，§3.1）。

设计：
- 线程安全（FastMCP 工具可能在多线程被调用）：LifoQueue + 锁
- 坏连接自愈：借出前 ping，断了自动重连一次；仍失败则抛错（⛔ 不静默降级）
- 密码只从环境变量进入内存（config.load_mysql_config），⛔ 不日志、不落盘
- 单副本部署下的保守池大小（§2.9 单副本）
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from queue import Empty, LifoQueue
from typing import Optional

import pymysql
import pymysql.err

from app.config import MysqlConfig, load_mysql_config

LOG = logging.getLogger("group-ops.mcp.pool")


class PoolError(RuntimeError):
    """连接池不可用（fail-closed：不静默降级为无库模式）。"""


class MysqlPool:
    """极简连接池：惰性建连、借出 ping、归还复检。"""

    def __init__(self, config: Optional[MysqlConfig] = None, size: int = 5) -> None:
        self._config = config or load_mysql_config()
        self._size = size
        self._idle: LifoQueue = LifoQueue()
        self._lock = threading.Lock()
        self._created = 0
        # 已借出（含正在 ping）的连接数：与 _created 一起维持
        # 「_created = 空闲 + 借出」不变式，才分得清「真的都在忙」和「计数漂移」
        self._borrowed = 0
        self._closed = False

    def _connect(self) -> pymysql.connections.Connection:
        try:
            return pymysql.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password,
                database=self._config.database,
                charset=self._config.charset,
                autocommit=False,
                connect_timeout=10,
                read_timeout=30,
                write_timeout=30,
            )
        except pymysql.err.MySQLError as exc:
            raise PoolError("mysql connect failed: %s" % exc) from exc

    def _acquire(self) -> pymysql.connections.Connection:
        with self._lock:
            if self._closed:
                raise PoolError("pool is closed")
            # ⭐ 计数漂移自愈（2026-10-20）：空闲队列为空、无人借出、但计数
            # 已到上限 —— 这个组合不可能是「真的都在忙」，只能是历史泄漏
            # （建连失败 / 借出异常未归还）。直接清零重来，⛔ 不回退无库模式。
            if (self._created >= self._size and self._borrowed == 0
                    and self._idle.empty()):
                LOG.warning(
                    "mysql pool counter drifted (created=%s, borrowed=0, idle=0)"
                    " -> resetting to 0", self._created)
                self._created = 0
        conn = None
        try:
            conn = self._idle.get_nowait()
            with self._lock:
                self._borrowed += 1
        except Empty:
            with self._lock:
                if self._created < self._size:
                    self._created += 1
                    try:
                        conn = self._connect()
                    except BaseException:
                        # ⭐ 建连失败必须归还计数。旧代码 `_created += 1;
                        # return self._connect()` 一旦抛错就永久多记一个
                        # （MySQL 抖动 5 次 = 计数打满且空闲队列为空）→ 之后
                        # 每次调用卡满 10 秒并抛 `queue.Empty`（str 为空串），
                        # **MySQL 恢复后也永不自愈**，只能重启进程。
                        # 2026-10-19 18:00 线上 314 次调度失败 + 后台全量
                        # 取不到数据即此因。
                        self._created -= 1
                        raise
                    self._borrowed += 1
            if conn is None:
                conn = self._idle.get(timeout=10)  # 达到上限则等待归还
                with self._lock:
                    self._borrowed += 1
        try:
            conn.ping(reconnect=True)
        except pymysql.err.MySQLError:
            try:
                conn.close()
            except Exception:
                pass
            # ⭐ D31 修复 2（2026-10-21）：重连失败必须归还 `_created` 与
            # `_borrowed`。此处已在上文 `_borrowed += 1`（借出计数），若
            # `_connect()` 再抛错而直接冒泡，异常就发生在 `connection()` 的
            # try/finally **之前** —— 两个计数永久泄漏，第 70 行的漂移自愈
            # （要求 `_borrowed == 0`）永远不触发，5 次后连接池死锁到重启。
            try:
                conn = self._connect()
            except BaseException:
                with self._lock:
                    self._created -= 1
                    self._borrowed -= 1
                raise
        return conn

    def _release(self, conn: pymysql.connections.Connection) -> None:
        with self._lock:
            self._borrowed -= 1
        if self._closed:
            try:
                conn.close()
            except Exception:
                pass
            return
        try:
            conn.rollback()  # 归还前确保无悬挂事务
        except Exception:
            pass
        self._idle.put(conn)

    @contextmanager
    def connection(self):
        conn = self._acquire()
        ok = False
        try:
            yield conn
            ok = True
        finally:
            if ok:
                self._release(conn)
            else:
                # 异常路径：连接状态未知，关闭重建，⛔ 不复用可疑连接
                try:
                    conn.close()
                except Exception:
                    pass
                with self._lock:
                    self._created -= 1
                    self._borrowed -= 1

    def close(self) -> None:
        with self._lock:
            self._closed = True
        while True:
            try:
                conn = self._idle.get_nowait()
                conn.close()
            except Exception:
                break

    def healthcheck(self) -> bool:
        try:
            with self.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchall()
            return True
        except Exception:
            return False
