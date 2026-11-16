"""试点模块配置：数据目录与号包签名密钥。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def pilot_home() -> Path:
    root = Path(os.environ.get("PILOT_HOME", Path.cwd() / "pilot_data"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def signing_key() -> bytes:
    """号包 HMAC 密钥，首启生成并落盘，仅本机演示用途。"""
    path = pilot_home() / "package.key"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


def local_key_path() -> Path:
    signing_key()  # 确保密钥已生成
    return pilot_home() / "package.key"


def export_key(target: str | Path) -> Path:
    """导出共享验签密钥，用于离线设备预置（随 U 盘等可信通道分发）。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(signing_key())
    target.chmod(0o600)
    return target
