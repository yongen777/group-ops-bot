# -*- coding: utf-8 -*-
"""上传凭证的保留策略（⭐ 2026-10-20 用户口径：业务数据全部留存）。

背景：原先 ``RETAIN_HOURS=48``，调度器每轮把 48h 前的打卡凭证**文件和
h5_upload 行一起删掉**。后果是后台回看历史记录时凭证图 404 —— 与
「数据全部保存」的预期直接冲突。

新口径：
- 默认 **0 = 永久保留**，清理任务空转（⛔ 不删文件、⛔ 不删行）
- 只有显式设置 ``GOPS_UPLOAD_RETAIN_HOURS``（>0）才清理
- 非法值 fail-safe 到「不删数据」（宁可占盘，⛔ 不误删业务证据）
"""

from __future__ import annotations

from app.web import uploads


def test_default_is_keep_forever(monkeypatch):
    monkeypatch.delenv(uploads.RETAIN_ENV, raising=False)
    assert uploads.resolve_retain_hours() == 0
    assert uploads.RETAIN_HOURS == 0


def test_env_can_enable_cleanup(monkeypatch):
    monkeypatch.setenv(uploads.RETAIN_ENV, "72")
    assert uploads.resolve_retain_hours() == 72


def test_env_zero_and_garbage_mean_keep(monkeypatch):
    monkeypatch.setenv(uploads.RETAIN_ENV, "0")
    assert uploads.resolve_retain_hours() == 0
    monkeypatch.setenv(uploads.RETAIN_ENV, "abc")
    assert uploads.resolve_retain_hours() == 0


def test_cleanup_noop_when_retention_disabled(monkeypatch):
    """默认配置下清理任务必须空转（state 传 None 也不会被访问）。"""
    monkeypatch.delenv(uploads.RETAIN_ENV, raising=False)
    result = uploads.cleanup_old_uploads(data_dir="/nonexistent", state=None)
    assert result["deleted_rows"] == 0
    assert result["deleted_files"] == 0
    assert result["skipped"] == "retain_disabled"
