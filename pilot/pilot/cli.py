"""命令行入口：管理端与离线设备端。

管理端（联机服务同机使用）：
  python -m pilot.cli init-db
  python -m pilot.cli create-batch 水世界 20 --seed 1005
  python -m pilot.cli assign Bxxx D1 --size 20
  python -m pilot.cli batches / batch Bxxx
  python -m pilot.cli return Bxxx Sxxx
  python -m pilot.cli close Bxxx
  python -m pilot.cli disputes / resolve 3 "保留首次派号"
  python -m pilot.cli analyze Bxxx / approve Bxxx / distribute Bxxx
  python -m pilot.cli serve --port 18304

设备端（可完全离线，设备目录独立）：
  python -m pilot.cli device-init D1 ./device_d1 --package packages/Sxxx.json
  python -m pilot.cli issue D1 ./device_d1 V1001
  python -m pilot.cli complete D1 ./device_d1 <code> V1001 [--arm-used A]
  python -m pilot.cli upload D1 ./device_d1         # 有服务时回传
  python -m pilot.cli device-status D1 ./device_d1
  python -m pilot.cli device-return D1 ./device_d1  # 清点未用号
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import pilot_home
from .device import DeviceClient
from .services import PilotError
from .storage import Database


def _db() -> Database:
    return Database(pilot_home() / "pilot.sqlite")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# 延迟导入，避免设备端命令依赖服务层之外的环境
def _svc():
    from . import services
    return services


def _http(method: str, path: str, payload: dict | None,
          base: str) -> dict:
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                 method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = json.loads(exc.read().decode("utf-8")).get("detail", exc)
        raise PilotError(f"HTTP {exc.code}: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot", description="随机导览试点工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db")
    p = sub.add_parser("export-key")
    p.add_argument("target", help="导出路径，如 ./device_d1/package.key")
    sub.add_parser("batches")
    p = sub.add_parser("batch"); p.add_argument("batch_id")
    p = sub.add_parser("create-batch")
    p.add_argument("zone"); p.add_argument("qty_per_arm", type=int)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--actor", default="admin")
    p = sub.add_parser("assign")
    p.add_argument("batch_id"); p.add_argument("device_id")
    p.add_argument("--size", type=int, default=None)
    p = sub.add_parser("return")
    p.add_argument("batch_id"); p.add_argument("segment_id")
    p = sub.add_parser("close"); p.add_argument("batch_id")
    p = sub.add_parser("disputes"); p.add_argument("--open", action="store_true")
    p = sub.add_parser("resolve"); p.add_argument("dispute_id", type=int)
    p.add_argument("resolution")
    p = sub.add_parser("analyze"); p.add_argument("batch_id")
    p = sub.add_parser("approve"); p.add_argument("batch_id")
    p.add_argument("--note", default="")
    p = sub.add_parser("distribute"); p.add_argument("batch_id")
    sub.add_parser("audit")
    p = sub.add_parser("serve"); p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18304)

    p = sub.add_parser("device-init")
    p.add_argument("device_id"); p.add_argument("device_home")
    p.add_argument("--package", required=True)
    p.add_argument("--key-file", default=None)
    p = sub.add_parser("issue")
    p.add_argument("device_id"); p.add_argument("device_home")
    p.add_argument("visit_id")
    p.add_argument("--key-file", default=None)
    p = sub.add_parser("complete")
    p.add_argument("device_id"); p.add_argument("device_home")
    p.add_argument("code"); p.add_argument("visit_id")
    p.add_argument("--arm-used", choices=["A", "B"], default=None)
    p.add_argument("--key-file", default=None)
    p = sub.add_parser("upload")
    p.add_argument("device_id"); p.add_argument("device_home")
    p.add_argument("--base", default="http://127.0.0.1:18304")
    p.add_argument("--key-file", default=None)
    p = sub.add_parser("device-status")
    p.add_argument("device_id"); p.add_argument("device_home")
    p.add_argument("--key-file", default=None)
    p = sub.add_parser("device-return")
    p.add_argument("device_id"); p.add_argument("device_home")
    p.add_argument("--key-file", default=None)

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except PilotError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    s = _svc()
    if args.cmd == "init-db":
        _db(); _print({"ok": True, "home": str(pilot_home())})
    elif args.cmd == "export-key":
        from .config import export_key
        path = export_key(args.target)
        _print({"exported": str(path),
                "hint": "请通过可信通道预置到设备目录，勿公开分发"})
    elif args.cmd == "create-batch":
        _print(s.create_batch(_db(), args.zone, args.qty_per_arm,
                              args.seed, args.actor))
    elif args.cmd == "batches":
        _print({"batches": s.list_batches(_db())})
    elif args.cmd == "batch":
        _print(s.get_batch_detail(_db(), args.batch_id))
    elif args.cmd == "assign":
        _print(s.allocate_segment(_db(), args.batch_id, args.device_id,
                                  args.size))
    elif args.cmd == "return":
        _print(s.return_segment(_db(), args.batch_id, args.segment_id))
    elif args.cmd == "close":
        _print(s.close_batch(_db(), args.batch_id))
    elif args.cmd == "disputes":
        _print({"disputes": s.list_disputes(_db(), args.open)})
    elif args.cmd == "resolve":
        _print(s.resolve_dispute(_db(), args.dispute_id, args.resolution))
    elif args.cmd == "analyze":
        _print(s.create_analysis(_db(), args.batch_id))
    elif args.cmd == "approve":
        _print(s.approve_analysis(_db(), args.batch_id, "admin", args.note))
    elif args.cmd == "distribute":
        _print(s.mark_distributed(_db(), args.batch_id))
    elif args.cmd == "audit":
        _print({"audit": s.audit_trail(_db())})
    elif args.cmd == "serve":
        from .server import serve
        serve(args.host, args.port)
    elif args.cmd == "device-init":
        package = json.loads(Path(args.package).read_text(encoding="utf-8"))
        _print(DeviceClient(args.device_id, args.device_home,
                            args.key_file).load_package(package))
    elif args.cmd == "issue":
        _print(DeviceClient(args.device_id, args.device_home,
                            args.key_file).issue(args.visit_id))
    elif args.cmd == "complete":
        _print(DeviceClient(args.device_id, args.device_home,
                            args.key_file).complete(
            args.code, args.visit_id, args.arm_used))
    elif args.cmd == "device-status":
        _print(DeviceClient(args.device_id, args.device_home,
                            args.key_file).status())
    elif args.cmd == "device-return":
        client = DeviceClient(args.device_id, args.device_home, args.key_file)
        _print({"unused_codes": client.unused_codes(),
                "status": client.status()})
    elif args.cmd == "upload":
        client = DeviceClient(args.device_id, args.device_home, args.key_file)
        payload = client.build_upload()
        if payload is None:
            _print({"nothing_to_upload": True})
            return 0
        last_seq = payload.pop("_last_seq")
        result = _http("POST", "/api/uploads", payload, args.base)
        client.mark_uploaded(last_seq)  # 服务端受理后才推进游标
        _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
