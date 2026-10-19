# -*- coding: utf-8 -*-
"""MySQL 连接池（MCP Server 侧；机器人⛔ 不直连 MySQL，§3.1）。

设计：
- 线程安全（FastMCP 工具可能在多线程被调用）：LifoQueue + 锁
- 坏连接自愈：借出前 ping，断了自动重连一次；仍失败则抛错（⛔ 不静默降级）
- 密码只从环境变量进入内存（config.load_mysql_config），⛔ 不日志、不落盘
- 单副本部署下的保守池大小（§2.9 单副本）
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from queue import LifoQueue
from typing import Optional

import pymysql
import pymysql.err

from app.config import MysqlConfig, load_mysql_config


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
        try:
            conn = self._idle.get_nowait()
        except Exception:
            with self._lock:
                if self._created < self._size:
                    self._created += 1
                    return self._connect()
            conn = self._idle.get(timeout=10)  # 达到上限则等待归还
        try:
            conn.ping(reconnect=True)
        except pymysql.err.MySQLError:
            try:
                conn.close()
            except Exception:
                pass
            conn = self._connect()
        return conn

    def _release(self, conn: pymysql.connections.Connection) -> None:
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
