# -*- coding: utf-8 -*-
"""D46（2026-10-26 用户报障）：H5 提交成功后必须锁成终态。

问题
----
报数 H5 提交完成后「界面还可以交互」：旧实现只在成功回调里
`submitBtn.disabled = true`，但 `refreshSubmitState()` 每次改动都会重算
`disabled = !valid || !verified` —— 用户点一下「验证身份」或改个数量，
按钮就又亮了，页面看起来像「没退出」，还能再提一次。
打卡页同构：提交成功后「拍照/选图」仍可点，上传成功回调会把 submitBtn 重新点亮。

为什么用 Node 跑
----------------
这是**状态机被重算覆盖**的问题，静态 grep 只能证明「代码里有 disabled = true」，
证明不了「点了别的按钮之后它还是 true」。所以用最小 DOM 替身把页面
<script> 真跑一遍，按用户顺序操作到提交之后再继续交互。

探针本身做过红验证：还原成旧的成功分支后，4 项断言失败（含
「提交后重新验证身份不能点亮提交按钮」），恢复后全绿。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "tools" / "h5_lock_check.js"
TEMPLATES = ROOT.parent / "group-ops-bot" / "app" / "h5templates"

# 本机可能装了多个 node，按可用性依次尝试（Windows 上默认 PATH 里的可能是旧版）
NODE_CANDIDATES = [
    shutil.which("node"),
    r"C:\Users\lenovo\.workbuddy\binaries\node\versions\22.22.2-2\node.exe",
    "/usr/bin/node",
    "/usr/local/bin/node",
]


def _node() -> str:
    for cand in NODE_CANDIDATES:
        if not cand:
            continue
        try:
            out = subprocess.run([cand, "--version"], capture_output=True,
                                 text=True, timeout=15)
        except Exception:
            continue
        if out.returncode == 0 and out.stdout.strip().startswith("v"):
            return cand
    pytest.skip("本机没有可用的 node，跳过 H5 行为探针")


def test_h5_submit_locks_page():
    node = _node()
    assert SCRIPT.is_file(), "缺少探针脚本 tools/h5_lock_check.js"
    assert TEMPLATES.is_dir(), "缺少模板目录 app/h5templates"

    proc = subprocess.run([node, str(SCRIPT), str(TEMPLATES)],
                          capture_output=True, text=True, timeout=120)
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        "H5 提交后未锁成终态（探针输出见下）:\n" + out)
    assert "全部通过" in out, out
    # 两个页面都必须被测到（⛔ 不能只测报数就过）
    assert "[报数 report.html]" in out
    assert "[打卡 checkin.html]" in out
