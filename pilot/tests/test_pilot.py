"""试点模块端到端验收测试。

运行：cd pilot && python -m unittest discover -s tests -v
覆盖：等量随机号包、号段不转拨、离线派号幂等、回传重试幂等、
跨设备重复领号/同号冲突隔离、交回才能关场、关场与回传并发不漏人、
ITT 统计（换方案按原分配、未完成保留分母）、旧批次照常核验。
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

os.environ["PILOT_HOME"] = tempfile.mkdtemp(prefix="pilot-test-")

from pilot.cli import _http  # noqa: E402
from pilot.device import DeviceClient  # noqa: E402
from pilot.server import serve  # noqa: E402
from pilot.services import (  # noqa: E402
    PilotError, allocate_segment, close_batch, create_analysis, create_batch,
    get_batch_detail, list_disputes, merge_upload, resolve_dispute,
    return_segment, verify_package,
)
from pilot.storage import Database  # noqa: E402
from pilot.config import pilot_home  # noqa: E402


class PilotTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pilot-case-")
        self.db = Database(Path(self.tmp) / "server.sqlite")
        self.dev_home = Path(self.tmp) / "devices"
        self.dev_home.mkdir()
        self._serial = 0

    def make_devices(self, batch_id, n_devices=2, size=10):
        # 每台设备一个独立本地库与唯一编号：一台设备一次试点只持一个号段
        self._serial += 1
        serial = self._serial
        packages = {}
        from pilot.config import local_key_path
        key_bytes = local_key_path().read_bytes()
        for i in range(1, n_devices + 1):
            device_id = f"D{serial}_{i}"
            pkg = allocate_segment(self.db, batch_id, device_id, size=size)
            self.assertTrue(verify_package(pkg))
            packages[device_id] = pkg
            dev_dir = self.dev_home / device_id
            dev_dir.mkdir(parents=True, exist_ok=True)
            # 模拟可信通道预置的共享验签密钥
            (dev_dir / "package.key").write_bytes(key_bytes)
            DeviceClient(device_id, dev_dir).load_package(pkg)
        return packages

    def client(self, device_id):
        return DeviceClient(device_id, self.dev_home / device_id)

    def sync(self, device_id):
        client = self.client(device_id)
        payload = client.build_upload()
        if payload is None:
            return None
        last_seq = payload.pop("_last_seq")
        result = merge_upload(self.db, payload)
        client.mark_uploaded(last_seq)
        return result

    def close_everything(self, batch_id, packages):
        for pkg in packages.values():
            return_segment(self.db, batch_id, pkg["segment_id"])
        return close_batch(self.db, batch_id)


class TestBatchAndSegments(PilotTestCase):
    def test_equal_arms_and_randomized(self):
        b1 = create_batch(self.db, "水世界", 20, seed=1005)
        b2 = create_batch(self.db, "机械坊", 20, seed=1005)
        self.assertEqual(b1["qty_per_arm"], 20)
        detail = get_batch_detail(self.db, b1["id"])
        counts = {(r["arm"], r["state"]): r["n"] for r in detail["pool_by_arm_state"]}
        self.assertEqual(counts[("A", "unused")], 20)
        self.assertEqual(counts[("B", "unused")], 20)
        # 同种子不同批次号码不同（含展区前缀），且排列随机而非 A 连号
        d2 = get_batch_detail(self.db, b2["id"])
        c1 = [r["code"] for r in self.db.connect().execute(
            "SELECT code FROM number_pool WHERE batch_id=? ORDER BY id", (b1["id"],))]
        c2 = [r["code"] for r in self.db.connect().execute(
            "SELECT code FROM number_pool WHERE batch_id=? ORDER BY id", (b2["id"],))]
        self.assertNotEqual(c1, c2)
        arms = [r["arm"] for r in self.db.connect().execute(
            "SELECT arm FROM number_pool WHERE batch_id=? ORDER BY id", (b1["id"],))]
        self.assertNotEqual(arms[:20], ["A"] * 20)

    def test_segment_not_reassigned(self):
        b = create_batch(self.db, "水世界", 10, seed=7)
        allocate_segment(self.db, b["id"], "D1", size=8)
        with self.assertRaises(PilotError):  # 同设备重复领段
            allocate_segment(self.db, b["id"], "D1", size=2)
        allocate_segment(self.db, b["id"], "D2", size=12)  # 只剩 12
        with self.assertRaises(PilotError):  # 池空
            allocate_segment(self.db, b["id"], "D3", size=1)
        # 已锁定行不会被再次分配
        free = self.db.connect().execute(
            "SELECT COUNT(*) AS n FROM number_pool WHERE batch_id=? AND segment_id IS NULL",
            (b["id"],)).fetchone()["n"]
        self.assertEqual(free, 0)

    def test_tampered_package_rejected(self):
        b = create_batch(self.db, "水世界", 4, seed=3)
        devices = self.make_devices(b["id"], n_devices=1, size=4)
        dev = next(iter(devices))
        pkg = devices[dev]
        pkg["numbers"][0]["arm"] = "B" if pkg["numbers"][0]["arm"] == "A" else "A"
        self.assertFalse(verify_package(pkg))
        with self.assertRaises(PilotError):
            self.client(dev).load_package(pkg)


class TestOfflineIssueAndUpload(PilotTestCase):
    def test_issue_idempotent_and_no_dup_after_restart(self):
        b = create_batch(self.db, "水世界", 5, seed=11)
        dev = next(iter(self.make_devices(b["id"], n_devices=1, size=5)))
        c = self.client(dev)
        first = c.issue("V001")
        again = c.issue("V001")  # 同次到访重试
        self.assertEqual(first["code"], again["code"])
        self.assertTrue(again["idempotent_replay"])
        second = c.issue("V002")
        self.assertNotEqual(first["code"], second["code"])
        # 模拟中断：新建对象重新打开同一设备库，状态不丢、不重号
        reopened = self.client(dev)
        third = reopened.issue("V003")
        self.assertEqual(reopened.status()["next_pos"], 3)
        self.assertNotIn(third["code"], (first["code"], second["code"]))
        result = self.sync(dev)
        self.assertEqual(result["accepted"], 3)

    def test_upload_retry_is_idempotent(self):
        b = create_batch(self.db, "水世界", 5, seed=12)
        devices = self.make_devices(b["id"], n_devices=1, size=5)
        dev = next(iter(devices))
        c = self.client(dev)
        c.issue("V001"); c.issue("V002")
        payload = c.build_upload()
        last_seq = payload.pop("_last_seq")
        r1 = merge_upload(self.db, payload)
        # 同样的 upload_id 原样重放（网络重试）
        r2 = merge_upload(self.db, payload)
        self.assertTrue(r2.get("replayed"))
        self.assertEqual(r1["accepted"], r2["accepted"])
        c.mark_uploaded(last_seq)
        # 游标已推进，再次 build 无载荷
        self.assertIsNone(c.build_upload())
        rows = self.db.connect().execute(
            "SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        self.assertEqual(rows, 2)
        uploads = self.db.connect().execute(
            "SELECT COUNT(*) AS n FROM uploads").fetchone()["n"]
        self.assertEqual(uploads, 1)

    def test_cross_device_duplicate_visit_quarantined(self):
        b = create_batch(self.db, "水世界", 10, seed=13)
        pkgs = self.make_devices(b["id"], n_devices=2, size=10)
        d1, d2 = list(pkgs)
        c1, c2 = self.client(d1), self.client(d2)
        r1 = c1.issue("V009")
        self.sync(d1)
        # 同一次到访在另一台设备又领号
        c2.issue("V009")
        result = self.sync(d2)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["disputes"][0]["kind"], "duplicate_issue")
        disputes = list_disputes(self.db, only_open=True)
        self.assertEqual(len(disputes), 1)
        # 隔离事件不改写原号绑定
        pool = self.db.connect().execute(
            "SELECT * FROM number_pool WHERE code=?", (r1["code"],)).fetchone()
        self.assertEqual(pool["visit_id"], "V009")
        self.assertEqual(pool["state"], "issued")
        # 未解决前不能出分析
        self.close_everything(b["id"], pkgs)
        with self.assertRaises(PilotError):
            create_analysis(self.db, b["id"])
        resolve_dispute(self.db, disputes[0]["id"], f"保留 {d1} 首次派号")
        analysis = create_analysis(self.db, b["id"])
        self.assertNotIn("idempotent_replay", analysis)

    def test_content_conflict_on_foreign_code(self):
        b = create_batch(self.db, "水世界", 10, seed=14)
        pkgs = self.make_devices(b["id"], n_devices=2, size=10)
        d1, d2 = list(pkgs)
        c1 = self.client(d1)
        c1.issue("V100")
        self.sync(d1)
        # 伪造 d2 上报 d1 号段里的号码
        foreign = self.db.connect().execute(
            "SELECT code FROM number_pool WHERE segment_id=? LIMIT 1",
            (pkgs[d1]["segment_id"],)).fetchone()["code"]
        bad = {
            "upload_id": f"{d2}-UP-bad",
            "batch_id": b["id"],
            "segment_id": pkgs[d2]["segment_id"],
            "device_id": d2,
            "events": [{
                "event_id": f"{d2}-EV000001", "code": foreign,
                "visit_id": "V200", "arm_used": None,
                "event_type": "issue", "occurred_at": "2026-11-16T09:00:00+00:00",
            }],
        }
        result = merge_upload(self.db, bad)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["disputes"][0]["kind"], "content_conflict")

    def test_complete_requires_binding_and_arm_switch_recorded(self):
        b = create_batch(self.db, "水世界", 6, seed=15)
        pkgs = self.make_devices(b["id"], n_devices=1, size=6)
        dev = next(iter(pkgs))
        c = self.client(dev)
        issued = c.issue("V300")
        with self.assertRaises(PilotError):  # 到访编号不一致
            c.complete(issued["code"], "V999")
        # 访客实际换了方案完成：记录 arm_used，统计仍按原分配
        c.complete(issued["code"], "V300", arm_used="B" if issued["arm"] == "A" else "A")
        c.issue("V301")  # 只领号未完成 → 保留分母
        self.sync(dev)
        incomplete_arm = self.db.connect().execute(
            "SELECT arm FROM number_pool WHERE visit_id='V301'").fetchone()["arm"]
        # 直接按号段交回
        seg_id = pkgs[dev]["segment_id"]
        return_segment(self.db, b["id"], seg_id)
        close_batch(self.db, b["id"])
        analysis = create_analysis(self.db, b["id"])
        snap = analysis["snapshot"]
        self.assertEqual(sum(snap["denominators"].values()), 2)  # 未完成保留分母
        self.assertEqual(len(snap["arm_switchers"]), 1)
        # 完成数计入号码原分配方案（即使访客实际换了方案）
        assigned_arm = issued["arm"]
        self.assertEqual(snap["tally"][assigned_arm]["completed"], 1)
        self.assertEqual(snap["tally"][incomplete_arm]["issued"], 1)


class TestClosingAndConcurrency(PilotTestCase):
    def test_cannot_close_before_all_returned(self):
        b = create_batch(self.db, "水世界", 10, seed=16)
        pkgs = self.make_devices(b["id"], n_devices=2, size=10)
        d1, d2 = list(pkgs)
        with self.assertRaises(PilotError):
            close_batch(self.db, b["id"])
        return_segment(self.db, b["id"], pkgs[d1]["segment_id"])
        with self.assertRaises(PilotError):  # 还有 d2
            close_batch(self.db, b["id"])
        close = self.close_everything(b["id"], pkgs)
        self.assertEqual(close["status"], "closed")
        # 关场后拒绝回传与再分配
        with self.assertRaises(PilotError):
            allocate_segment(self.db, b["id"], "D3-extra")
        with self.assertRaises(PilotError):
            merge_upload(self.db, {
                "upload_id": "x", "batch_id": b["id"],
                "segment_id": pkgs[d2]["segment_id"],
                "device_id": d2, "events": []})

    def test_return_reclaims_only_unused(self):
        b = create_batch(self.db, "水世界", 6, seed=17)
        pkgs = self.make_devices(b["id"], n_devices=1, size=6)
        dev = next(iter(pkgs))
        c = self.client(dev)
        c.issue("V1"); c.issue("V2")
        self.sync(dev)
        seg_id = pkgs[dev]["segment_id"]
        summary = return_segment(self.db, b["id"], seg_id)
        self.assertEqual(summary["counts"].get("reclaimed"), 4)
        self.assertEqual(summary["counts"].get("issued"), 2)
        # 重复交回幂等
        again = return_segment(self.db, b["id"], seg_id)
        self.assertEqual(again["id"], seg_id)

    def test_concurrent_upload_and_close_nobody_lost(self):
        b = create_batch(self.db, "水世界", 40, seed=18)
        devices = list(self.make_devices(b["id"], n_devices=4, size=20))
        errors, barrier = [], threading.Barrier(5)
        synced_segments: list[str] = []
        sync_lock = threading.Lock()

        def device_work(dev):
            c = self.client(dev)
            last_code = None
            for i in range(12):
                out = c.issue(f"{dev}-V{i}")
                last_code = out["code"]
                if i % 3 == 0:
                    c.complete(last_code, f"{dev}-V{i}")
            barrier.wait()  # 四台设备同时回传，制造合并并发
            try:
                self.sync(dev)
                seg_id = self.db.connect().execute(
                    "SELECT id FROM segments WHERE device_id=?",
                    (dev,)).fetchone()["id"]
                with sync_lock:
                    synced_segments.append(seg_id)
            except PilotError as exc:
                errors.append(str(exc))

        def close_work():
            barrier.wait()
            # 只交回已完成回传的号段；关场在号段未齐时失败并重试，
            # 验证关场与回传并发下不漏一个已派号码
            for _ in range(200):
                try:
                    with sync_lock:
                        ready = list(synced_segments)
                    for seg_id in ready:
                        seg = self.db.connect().execute(
                            "SELECT status FROM segments WHERE id=?",
                            (seg_id,)).fetchone()
                        if seg and seg["status"] == "assigned":
                            return_segment(self.db, b["id"], seg_id)
                    close_batch(self.db, b["id"])
                    return
                except PilotError:
                    threading.Event().wait(0.01)
            errors.append("关场重试未成功")

        threads = [threading.Thread(target=device_work, args=(d,))
                   for d in devices]
        threads.append(threading.Thread(target=close_work))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        detail = get_batch_detail(self.db, b["id"])
        self.assertEqual(detail["status"], "closed")
        # 48 次派号一个不少（关场与回传并发不漏人）
        issued = sum(r["n"] for r in detail["pool_by_arm_state"]
                     if r["state"] in ("issued", "completed"))
        self.assertEqual(issued, 48)


class TestAnalysisAndOldBatches(PilotTestCase):
    def _run_closed_batch(self, seed):
        b = create_batch(self.db, "水世界", 30, seed=seed)
        pkgs = self.make_devices(b["id"], n_devices=2, size=30)
        d1, d2 = list(pkgs)
        c1, c2 = self.client(d1), self.client(d2)
        for i in range(10):
            out = c1.issue(f"V{i}")
            if i % 2 == 0:
                c1.complete(out["code"], f"V{i}")
        for i in range(8):
            c2.issue(f"W{i}")  # d2 全部未完成
        self.sync(d1); self.sync(d2)
        self.close_everything(b["id"], pkgs)
        return b

    def test_analysis_idempotent_and_old_batch_verifiable(self):
        old = self._run_closed_batch(21)
        a1 = create_analysis(self.db, old["id"])
        a2 = create_analysis(self.db, old["id"])  # 重试不重复建分析
        self.assertEqual(a1["id"], a2["id"])
        self.assertTrue(a2.get("idempotent_replay"))
        self.assertIn("chi2", a1["snapshot"]["stats"])
        self.assertEqual(a1["snapshot"]["stats"]["df"], 1)
        # 旧批次快照保持不变，可照常核验；新批次独立生成
        new = self._run_closed_batch(22)
        a3 = create_analysis(self.db, new["id"])
        self.assertNotEqual(a1["id"], a3["id"])
        old_again = get_batch_detail(self.db, old["id"])["analysis"]
        self.assertEqual(old_again["snapshot"]["generated_at"],
                         a1["snapshot"]["generated_at"])

    def test_analysis_needs_closed_batch(self):
        b = create_batch(self.db, "水世界", 4, seed=23)
        with self.assertRaises(PilotError):
            create_analysis(self.db, b["id"])


class TestHttpApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pilot-http-")
        os.environ["PILOT_HOME"] = cls.tmp
        from http.server import ThreadingHTTPServer
        from pilot.server import make_handler
        cls.db = Database(Path(cls.tmp) / "server.sqlite")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.db))
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_full_flow_over_http(self):
        batch = _http("POST", "/api/batches",
                      {"zone": "穹顶厅", "qty_per_arm": 5, "seed": 99}, self.base)
        self.assertEqual(batch["qty_per_arm"], 5)
        pkg = _http("POST", f"/api/batches/{batch['id']}/segments",
                    {"device_id": "D9"}, self.base)
        self.assertIn("signature", pkg)
        listing = _http("GET", "/api/batches", None, self.base)
        self.assertEqual(len(listing["batches"]), 1)
        # 错误参数返回 400 而不是 500
        with self.assertRaises(PilotError):
            _http("POST", "/api/batches", {"zone": "", "qty_per_arm": 0}, self.base)
        # 静态页面可访问
        with urllib.request.urlopen(f"{self.base}/index.html") as resp:
            self.assertIn("随机导览", resp.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
