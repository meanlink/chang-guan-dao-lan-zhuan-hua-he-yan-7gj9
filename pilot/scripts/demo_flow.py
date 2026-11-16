"""一键演示：两台离线设备完整试点流程（确定性，可重复运行）。

运行：python scripts/demo_flow.py
会在系统临时目录下创建独立数据，不影响已有数据。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 先定位独立数据目录，再导入 pilot（config 在导入时读取 PILOT_HOME）
_work = Path(tempfile.mkdtemp(prefix="pilot-demo-"))
os.environ["PILOT_HOME"] = str(_work / "server")

from pilot.config import local_key_path  # noqa: E402
from pilot.device import DeviceClient  # noqa: E402
from pilot.services import (  # noqa: E402
    allocate_segment, close_batch, create_analysis, approve_analysis,
    mark_distributed, create_batch, get_batch_detail, list_disputes,
    merge_upload, resolve_dispute, return_segment,
)
from pilot.storage import Database  # noqa: E402


def main() -> None:
    work = _work
    db = Database(work / "server" / "pilot.sqlite")
    print(f"数据目录：{work}")

    batch = create_batch(db, "水世界", qty_per_arm=10, seed=1005)
    print(f"1. 批次 {batch['id']}：A/B 各 10 个随机号，共 20 个")

    packages = {}
    for device_id, size in (("D1", 10), ("D2", 10)):
        pkg = allocate_segment(db, batch["id"], device_id, size=size)
        packages[device_id] = pkg
        dev_dir = work / device_id
        dev_dir.mkdir(parents=True)
        (dev_dir / "package.key").write_bytes(local_key_path().read_bytes())
        DeviceClient(device_id, dev_dir).load_package(pkg)
    print("2. 两个签名号段已分配 D1/D2，密钥已预置")

    d1, d2 = DeviceClient("D1", work / "D1"), DeviceClient("D2", work / "D2")
    issued = {}
    for i, visit in enumerate(("V1001", "V1002", "V1003", "V1004"), start=1):
        out = d1.issue(visit)
        issued[visit] = out
        if i <= 3:  # 前三位完成，其中 V1003 实际换了方案
            switched = "B" if out["arm"] == "A" else "A"
            d1.complete(out["code"], visit,
                        arm_used=switched if visit == "V1003" else None)
    d2.issue("V1001")  # 与 D1 同次到访 → 跨设备重复领号
    d2.issue("V2001")

    for client in (d1, d2):
        payload = client.build_upload()
        last_seq = payload.pop("_last_seq")
        result = merge_upload(db, payload)
        client.mark_uploaded(last_seq)
        print(f"3. {client.device_id} 回传：受理 {result['accepted']}，"
              f"隔离 {result['quarantined']}")

    open_disputes = list_disputes(db, only_open=True)
    print(f"4. 未决争议 {len(open_disputes)} 条，未解决前拒绝出分析")
    for d in open_disputes:
        resolve_dispute(db, d["id"], "保留 D1 首次派号，D2 重复号作废")

    for pkg in packages.values():
        return_segment(db, batch["id"], pkg["segment_id"])
    close_batch(db, batch["id"])
    print("5. 两台设备交回未用号，批次已关场")

    analysis = create_analysis(db, batch["id"])
    snap = analysis["snapshot"]
    approve_analysis(db, batch["id"], "admin", "演示批准")
    mark_distributed(db, batch["id"])
    print("6. ITT 分析已批准并分发：")
    print("   分母（含未完成）：", snap["denominators"])
    print("   列联表：", snap["contingency_table"]["observed"])
    print("   换方案披露：", snap["arm_switchers"])
    print("   统计：", snap["stats"] or snap["stats_error"])

    detail = get_batch_detail(db, batch["id"])
    assert detail["status"] == "closed"
    # D1 派出 4 个 + D2 有效 1 个（另 1 个同次到访重复领号被隔离），共 5 个在册
    assert sum(r["n"] for r in detail["pool_by_arm_state"]
               if r["state"] in ("issued", "completed")) == 5
    print("7. 关场后核对：5 个有效已派号码全部在册，争议号不污染分母，不漏人。演示完成。")


if __name__ == "__main__":
    main()
