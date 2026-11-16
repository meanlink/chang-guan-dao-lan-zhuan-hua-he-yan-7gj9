"""标准库 HTTP 服务：无需第三方依赖，断网环境直接启动。

API：
  POST /api/batches                 建批次（等量 A/B 号包）
  GET  /api/batches                 批次总览（发号/回收/争议/关场）
  GET  /api/batches/{id}            批次详情
  POST /api/batches/{id}/segments   分配号段给设备，返回签名号包
  POST /api/uploads                 回传合并（幂等、冲突隔离）
  POST /api/segments/{sid}/return   设备交回未用号
  POST /api/batches/{id}/close      关场
  GET  /api/disputes                争议列表
  POST /api/disputes/{id}/resolve   处理争议
  POST /api/batches/{id}/analysis   生成分析（幂等）
  POST /api/batches/{id}/approve    批准分析
  POST /api/batches/{id}/distribute 标记分发
  GET  /api/audit                   审计轨迹
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path

from .config import pilot_home
from .services import (
    PilotError, allocate_segment, approve_analysis, audit_trail,
    close_batch, create_analysis, create_batch, get_batch_detail,
    list_batches, list_disputes, mark_distributed, merge_upload,
    resolve_dispute, return_segment,
)
from .storage import Database

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def make_handler(db: Database):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PilotOffline/1.0"

        def log_message(self, fmt, *args):  # 简洁日志
            print("[http]", self.address_string(), fmt % args)

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise PilotError("请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise PilotError("请求体必须是 JSON 对象")
            return data

        def _static(self, path: str) -> bool:
            if path in ("", "/"):
                path = "/index.html"
            target = (WEB_DIR / path.lstrip("/")).resolve()
            if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
                return False
            ctype = "text/html; charset=utf-8" if target.suffix == ".html" else \
                    "text/javascript; charset=utf-8" if target.suffix == ".js" else \
                    "text/css; charset=utf-8" if target.suffix == ".css" else \
                    "application/octet-stream"
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        def do_GET(self):
            try:
                route = urlparse(self.path).path
                if route == "/health":
                    return self._send(200, {"status": "ok"})
                if route.startswith("/api/"):
                    return self._api_get(route)
                if self._static(route):
                    return
                self._send(404, {"detail": "未找到"})
            except PilotError as exc:
                self._send(400, {"detail": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"detail": f"服务异常：{exc}"})

        def do_POST(self):
            try:
                self._api_post(urlparse(self.path).path, self._body())
            except PilotError as exc:
                self._send(400, {"detail": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"detail": f"服务异常：{exc}"})

        def _api_get(self, route: str) -> None:
            if route == "/api/batches":
                return self._send(200, {"batches": list_batches(db)})
            if route.startswith("/api/batches/") and route.count("/") == 3:
                return self._send(200, get_batch_detail(db, route.split("/")[3]))
            if route == "/api/disputes":
                only_open = urlparse(self.path).query == "open=1"
                return self._send(200, {"disputes": list_disputes(db, only_open)})
            if route == "/api/audit":
                return self._send(200, {"audit": audit_trail(db)})
            self._send(404, {"detail": "未知接口"})

        def _api_post(self, route: str, body: dict) -> None:
            parts = [p for p in route.split("/") if p]
            if route == "/api/batches":
                batch = create_batch(db, body.get("zone", ""),
                                     int(body.get("qty_per_arm", 0)),
                                     body.get("seed"),
                                     body.get("actor", "admin"))
                return self._send(201, batch)
            if len(parts) == 4 and parts[:2] == ["api", "batches"]:
                batch_id = parts[2]
                action = parts[3]
                if action == "segments":
                    pkg = allocate_segment(db, batch_id,
                                           body.get("device_id", ""),
                                           body.get("size"),
                                           body.get("actor", "admin"))
                    return self._send(201, pkg)
                if action == "close":
                    return self._send(200, close_batch(
                        db, batch_id, body.get("actor", "admin")))
                if action == "analysis":
                    return self._send(201, create_analysis(
                        db, batch_id, body.get("actor", "analyst")))
                if action == "approve":
                    return self._send(200, approve_analysis(
                        db, batch_id, body.get("approver", "admin"),
                        body.get("note", "")))
                if action == "distribute":
                    return self._send(200, mark_distributed(
                        db, batch_id, body.get("actor", "admin")))
            if route == "/api/uploads":
                return self._send(200, merge_upload(db, body))
            if len(parts) == 4 and parts[:2] == ["api", "segments"] \
                    and parts[3] == "return":
                batch_id = body.get("batch_id", "")
                return self._send(200, return_segment(
                    db, batch_id, parts[2], body.get("actor")))
            if len(parts) == 4 and parts[:2] == ["api", "disputes"] \
                    and parts[3] == "resolve":
                return self._send(200, resolve_dispute(
                    db, int(parts[2]), body.get("resolution", ""),
                    body.get("actor", "admin")))
            self._send(404, {"detail": "未知接口"})

    return Handler


def serve(host: str = "127.0.0.1", port: int = 18304) -> None:
    db = Database(pilot_home() / "pilot.sqlite")
    httpd = ThreadingHTTPServer((host, port), make_handler(db))
    print(f"随机导览试点服务已启动：http://{host}:{port} （数据目录 {pilot_home()}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
