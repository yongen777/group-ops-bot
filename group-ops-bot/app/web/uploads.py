# -*- coding: utf-8 -*-
"""H5 图片上传通道（§4.11.4 D4 定夺：方案 A 机器人中转 + 服务器磁盘，2026-10-16）。

链路（与主文档 §4.11.4 候选方案 A 一致）：
    H5(multipart) -> 机器人 :18002 -> 本模块落**服务器磁盘** ->
    返回服务端签发的 ``image_ref`` -> ``checkin.submit(image_ref)``。

硬规则：
- ⛔ MCP Server / 存储凭据不出服务端；H5 只与机器人交互（同源）
- ⛔ 类型靠**魔数嗅探**判定，⛔ 不信客户端声明的 content-type / 扩展名
- ⛔ 大小上限 ``MAX_UPLOAD_BYTES``；超限 / 非图片一律拒绝（fail-closed）
- ✅ 凭证行（state.h5_upload）与令牌绑定：提交时校验 token_id 一致才认，
  防拿他人 image_ref 冒领上传通道
- ✅ Q11 口径：结果未确认、回执未落稳前⛔ 不提前删除（ consumed 标记 +
  保留期限清理都在提交成功之后，见 cleanup_old_uploads）
- ⛔ 本体不长期留机器人侧：保留期限 ``RETAIN_HOURS`` 过后由调度器
  best-effort 清理（失败只记日志，⛔ 不影响主流程）
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("group-ops.uploads")

# 上传文件大小上限（10MB）：手机拍照 + 适度压缩后的常见量级；
# ⛔ 超限一律拒绝，⛔ 不静默截断（截断图 = 坏图，比拒绝更糟）。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# 允许的图片类型（嗅探判定 -> 规范扩展名）。
# ⛔ 不放行 GIF / SVG：动图与矢量图在本业务无意义且扩大攻击面。
_MAGIC_TABLE = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"RIFF", "image/webp", "webp"),   # RIFF....WEBP，见 _sniff_mime
)

# 上传文件保留期限（小时）：超过即清理（Q11：只清已不被任何在途流程引用的）。
RETAIN_HOURS = 48

# 上传引用长度（服务端签发；⛔ 不用客户端提供的任何标识）。
_REF_LEN = 22


def generate_ref() -> str:
    """服务端签发的上传引用（高强度随机，URL-safe）。"""
    return secrets.token_urlsafe(_REF_LEN)


def _sniff_mime(content: bytes) -> Optional[str]:
    """按文件头魔数判定类型；不识别返回 None（⛔ 不猜）。"""
    if len(content) < 12:
        return None
    for magic, mime, _ext in _MAGIC_TABLE:
        if not content.startswith(magic):
            continue
        if mime == "image/webp" and content[8:12] != b"WEBP":
            continue  # RIFF 容器还可能是 AVI/WAV，必须看第 9-12 字节
        return mime
    return None


def _ext_for(mime: str) -> str:
    for _magic, m, ext in _MAGIC_TABLE:
        if m == mime:
            return ext
    return "bin"


def sniff_image(content: bytes) -> Optional[str]:
    """公开嗅探口（测试 / 端点共用）：合法图片返回规范 mime，否则 None。"""
    return _sniff_mime(content)


def uploads_dir(data_dir) -> Path:
    """上传根目录（服务唯一可写数据目录下的子目录，§2.9 目录纪律）。"""
    return Path(data_dir) / "uploads"


def rel_path_for(group_id: str, period_key: str, ref: str, mime: str) -> str:
    """存储相对路径（相对 data_dir；⛔ 不含绝对路径，防路径穿越外泄）。"""
    return "uploads/%s/%s/%s.%s" % (group_id, period_key, ref, _ext_for(mime))


def save_upload(*, data_dir, group_id: str, period_key: str, ref: str,
                file_name: str, content: bytes) -> dict:
    """嗅探 + 落盘。返回 {"rel_path", "mime", "size"}；不合法抛 ValueError。

    ⛔ 调用方负责把 ValueError 转成对用户的明确拒绝话术（fail-closed）。
    """
    if not content:
        raise ValueError("文件内容为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("图片超过 %dMB 上限" % (MAX_UPLOAD_BYTES // 1048576))
    mime = _sniff_mime(content)
    if mime is None:
        raise ValueError("仅支持 JPG / PNG / WebP 图片")
    rel = rel_path_for(group_id, period_key, ref, mime)
    target = Path(data_dir) / rel
    # 防御：rel_path 由本函数构造，仍二次校验不越出数据目录
    base = Path(data_dir).resolve()
    resolved = target.resolve()
    if base != resolved and base not in resolved.parents:
        raise ValueError("存储路径不合法")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {"rel_path": rel, "mime": mime, "size": len(content),
            "file_name": (file_name or "")[:128]}


def upload_abs_path(data_dir, rel_path: str) -> Optional[Path]:
    """rel_path -> 绝对路径；越界 / 不存在返回 None（⛔ 不做任何猜测拼接）。"""
    if not rel_path or rel_path.startswith("/") or ".." in rel_path:
        return None
    base = Path(data_dir).resolve()
    target = (base / rel_path).resolve()
    if base != target and base not in target.parents:
        return None
    return target if target.is_file() else None


def cleanup_old_uploads(*, data_dir, state, now: Optional[datetime] = None,
                        retain_hours: int = RETAIN_HOURS) -> dict:
    """清理超过保留期限的上传（best-effort；⛔ 失败只记日志不上抛）。

    与 Q11 的关系：保留期限（48h）远大于令牌 30 分钟有效期与 C 类恢复
    窗口，到点才清 —— 结果未确认的回执不会因清理丢失。
    """
    from .. import period as period_mod
    now = now or period_mod.now()
    cutoff = (now - timedelta(hours=retain_hours)).isoformat(
        timespec="seconds")
    result = {"deleted_rows": 0, "deleted_files": 0, "errors": 0}
    try:
        rows = state.list_uploads_before(cutoff)
    except Exception:
        LOG.exception("list_uploads_before failed")
        return result
    for row in rows:
        try:
            path = upload_abs_path(data_dir, row["rel_path"])
            if path is not None:
                path.unlink()
                result["deleted_files"] += 1
        except Exception:
            result["errors"] += 1
            LOG.warning("delete upload file failed: ref=%s", row["ref"])
        try:
            state.delete_upload(row["ref"])
            result["deleted_rows"] += 1
        except Exception:
            result["errors"] += 1
            LOG.warning("delete upload row failed: ref=%s", row["ref"])
    return result
