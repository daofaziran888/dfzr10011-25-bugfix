"""看板刷新/归档/权限修复的端到端校验。

覆盖需求口径：
1. 刷新结果绑定完整数据范围，面板改字段不污染历史刷新结果；
2. 归档后不能生成新快照，两种到达顺序（归档先行 / 刷新先行）各自的表现；
3. 并发刷新同一面板只有一个批次落库；失败刷新不覆盖旧快照；
4. 跨团队越权只读摘要、不可改动，非共享面板不可见；
5. 刷新结果同步到存储模块：导出快照保留范围，归档待处理清单同步留痕。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.seed import SEED_ROWS
from app.store import store

client = TestClient(app)

TEAM_A = "采集A组"
TEAM_B = "采集B组"
OUTSIDER = "采集团队C"
OPERATOR_A = "值班管理员"


def reset_store() -> None:
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}


def get(path: str, team: str = TEAM_A) -> object:
    sep = "&" if "?" in path else "?"
    return client.get(f"{path}{sep}team={team}")


def post(path: str, values: dict | None = None, team: str = TEAM_A, operator: str = OPERATOR_A) -> object:
    payload = {"values": {"团队": team, "操作人": operator, **(values or {})}}
    return client.post(path, json=payload)


def put(path: str, values: dict | None = None, team: str = TEAM_A) -> object:
    payload = {"values": {"团队": team, **(values or {})}}
    return client.put(path, json=payload)


def test_seed_snapshot_retains_full_range_and_panel_view_shows_latest() -> None:
    reset_store()
    resp = get("/api/dashboard/1")
    assert resp.status_code == 200
    body = resp.json()
    # 团队范围看到面板声明数据 + 最近成功快照，且快照保留完整数据范围。
    assert body["数据范围"] == "采集A组-近24小时"
    assert body["最近快照"]["数据范围"] == "采集A组-近24小时"
    assert body["最近快照"]["快照版本"] == "v1"


def test_saving_panel_fields_does_not_mutate_history_refresh_range() -> None:
    reset_store()
    # 面板字段保存并重新校验刷新范围，历史批次绑定的仍是申请时的范围。
    resp = put("/api/dashboard/1/fields", {"数据范围": "采集A组-近12小时", "显示指标": "新指标口径"})
    assert resp.status_code == 200
    batches = get("/api/dashboard/1/refresh-batches").json()
    history = next(row for row in batches["items"] if row["批次编号"] == "REF-DASH-0001-0001")
    assert history["数据范围"] == "采集A组-近24小时"
    assert history["面板声明"]["数据范围"] == "采集A组-近24小时"
    assert history["面板声明"]["显示指标"] == "采集任务总量、节点在线率"
    # 导出区旧快照同样保持原范围，不被面板字段保存覆盖。
    snapshot = client.get("/api/storage/snapshots").json()["items"][0]
    assert snapshot["数据范围"] == "采集A组-近24小时"


def test_refresh_must_bind_full_range_and_success_creates_storage_snapshot() -> None:
    reset_store()
    resp = post("/api/dashboard/2/refresh-batches")
    assert resp.status_code == 200
    batch = resp.json()["entry"]
    assert batch["批次状态"] == "刷新中"
    assert batch["数据范围"] == "采集A组-近7天"
    # 面板声明在批次落库时被复制，后续不可变。
    assert batch["面板声明"]["显示指标"] == "归并成功率、平均延迟"

    done = post(
        f"/api/dashboard/refresh-batches/{batch['id']}/complete",
        {"成功": True, "指标快照": {"归并成功率": "99.1%"}},
    )
    assert done.status_code == 200
    snapshot_id = done.json()["entry"]["快照id"]

    # 存储分层按刷新结果生成导出快照，保留数据范围/版本/时间。
    snap = client.get(f"/api/storage/snapshots/{snapshot_id}").json()
    assert snap["数据范围"] == "采集A组-近7天"
    assert snap["快照版本"] == "v1"
    assert snap["生成时间"]
    panel = get("/api/dashboard/2").json()
    assert panel["最近快照版本"] == "v1"
    assert panel["最近成功批次编号"] == batch["批次编号"]


def test_completion_rejects_range_drift() -> None:
    reset_store()
    batch = post("/api/dashboard/2/refresh-batches").json()["entry"]
    resp = post(
        f"/api/dashboard/refresh-batches/{batch['id']}/complete",
        {"成功": True, "数据范围": "被篡改的其它范围"},
    )
    assert resp.status_code == 400
    assert "范围不一致" in resp.json()["message"]


def test_concurrent_refresh_only_one_batch_lands() -> None:
    reset_store()
    first = post("/api/dashboard/2/refresh-batches")
    assert first.status_code == 200
    second = post("/api/dashboard/2/refresh-batches")
    assert second.status_code == 409
    assert "并发刷新只允许一个批次" in second.json()["message"]
    # 首个批次结束后可再次申请。
    bid = first.json()["entry"]["id"]
    post(f"/api/dashboard/refresh-batches/{bid}/complete", {"成功": True})
    third = post("/api/dashboard/2/refresh-batches")
    assert third.status_code == 200


def test_failed_refresh_keeps_old_snapshot() -> None:
    reset_store()
    before = client.get("/api/storage/snapshots", params={"panel_id": 1}).json()["items"]
    assert len(before) == 1
    batch = post("/api/dashboard/1/refresh-batches").json()["entry"]
    resp = post(
        f"/api/dashboard/refresh-batches/{batch['id']}/complete",
        {"成功": False, "失败原因": "采集源超时"},
    )
    assert resp.status_code == 200
    assert resp.json()["entry"]["批次状态"] == "刷新失败"
    after = client.get("/api/storage/snapshots", params={"panel_id": 1}).json()["items"]
    assert len(after) == 1  # 失败不生成新快照
    assert after[0]["id"] == before[0]["id"]  # 旧快照未被覆盖
    panel = get("/api/dashboard/1").json()
    assert panel["最近快照id"] == before[0]["id"]


def test_archive_first_keeps_old_snapshot_and_rejects_later_refresh() -> None:
    reset_store()
    # 面板3已暂停，可直接归档；它没有成功快照。
    post("/api/dashboard/3/actions", {"action": "归档面板"}, team=TEAM_B)
    # 归档后申请刷新 -> 拒绝留痕，无新快照。
    resp = post("/api/dashboard/3/refresh-batches", team=TEAM_B)
    assert resp.status_code == 409
    assert "归档" in resp.json()["message"]
    batches = get("/api/dashboard/3/refresh-batches", team=TEAM_B).json()["items"]
    rejected = next(row for row in batches if row["批次状态"] == "已拒绝")
    assert rejected["归档后拒绝"] is True
    assert client.get("/api/storage/snapshots", params={"panel_id": 3}).json()["total"] == 0

    # 归档待处理清单已同步到存储模块。
    tasks = client.get("/api/storage/archive-tasks", params={"panel_id": 3}).json()["items"]
    assert len(tasks) == 1 and tasks[0]["任务状态"] == "已完成"
    assert tasks[0]["备注"] == "归档先行并保留旧快照"


def test_archive_first_with_inflight_batch_rejects_receipt_old_snapshot_stays() -> None:
    reset_store()
    # 面板1已有旧快照 v1；按状态序列启用、暂停后再归档，期间制造一个在途批次。
    post("/api/dashboard/1/actions", {"action": "启用面板"})
    post("/api/dashboard/1/actions", {"action": "暂停面板"})
    inflight = post("/api/dashboard/1/refresh-batches").json()["entry"]
    post("/api/dashboard/1/actions", {"action": "归档面板"})
    # 归档先行保留旧快照，导出区仍显示旧快照。
    snaps = client.get("/api/storage/snapshots", params={"panel_id": 1}).json()["items"]
    assert [s["快照版本"] for s in snaps] == ["v1"]
    # 在途批次的完成回执后到 -> 拒绝，不落新快照。
    receipt = post(f"/api/dashboard/refresh-batches/{inflight['id']}/complete", {"成功": True})
    assert receipt.status_code == 409
    assert "回执被拒绝" in receipt.json()["message"]
    assert client.get("/api/storage/snapshots", params={"panel_id": 1}).json()["total"] == 1


def test_refresh_first_then_archive_keeps_new_snapshot_and_notes_last_refresh() -> None:
    reset_store()
    post("/api/dashboard/2/actions", {"action": "暂停面板"})
    batch = post("/api/dashboard/2/refresh-batches").json()["entry"]
    post(f"/api/dashboard/refresh-batches/{batch['id']}/complete",
         {"成功": True, "指标快照": {"k": "v"}})
    # 刷新先行后立即归档：导出区保留新快照时间和版本。
    post("/api/dashboard/2/actions", {"action": "归档面板"})
    snaps = client.get("/api/storage/snapshots", params={"panel_id": 2}).json()["items"]
    assert len(snaps) == 1 and snaps[0]["快照版本"] == "v1"
    assert snaps[0]["来源状态"] == "刷新成功"
    assert snaps[0]["保留状态"] == "归档保留"
    tasks = client.get("/api/storage/archive-tasks", params={"panel_id": 2}).json()["items"]
    assert tasks[0]["最后刷新批次"] == batch["批次编号"]
    assert tasks[0]["保留快照版本"] == "v1"
    assert tasks[0]["备注"] == "刷新先行后立即归档，归档记录注明最后刷新"
    # 归档后再到的刷新申请不能生成新快照。
    again = post("/api/dashboard/2/refresh-batches")
    assert again.status_code == 409
    assert client.get("/api/storage/snapshots", params={"panel_id": 2}).json()["total"] == 1


def test_cross_team_sees_shared_summary_only_and_cannot_change() -> None:
    reset_store()
    listing = get("/api/dashboard", team=OUTSIDER).json()
    # 非共享的面板1、面板3不可见，只看到共享面板2的摘要。
    assert listing["total"] == 1
    summary = listing["items"][0]
    assert summary["面板编号"] == "DASH-0002"
    assert summary["摘要访问"] is True
    assert "数据范围" not in summary and "告警配置" not in summary

    detail = get("/api/dashboard/2", team=OUTSIDER).json()
    assert detail["摘要访问"] is True
    assert "数据范围" not in detail

    # 越权改动全部 403：字段保存、状态动作、发起刷新。
    assert put("/api/dashboard/2/fields", {"显示指标": "越权修改"}, team=OUTSIDER).status_code == 403
    assert post("/api/dashboard/2/actions", {"action": "暂停面板"}, team=OUTSIDER).status_code == 403
    assert post("/api/dashboard/2/refresh-batches", team=OUTSIDER).status_code == 403
    # 非共享面板连摘要都看不到。
    assert get("/api/dashboard/1", team=OUTSIDER).status_code == 403


def test_owner_team_can_operate_and_team_b_is_isolated() -> None:
    reset_store()
    # B 组看到自己团队的面板3（全量）与跨团队共享的面板2（摘要）。
    b_list = get("/api/dashboard", team=TEAM_B).json()
    by_code = {row["面板编号"]: row for row in b_list["items"]}
    assert set(by_code) == {"DASH-0002", "DASH-0003"}
    assert by_code["DASH-0002"]["摘要访问"] is True
    assert by_code["DASH-0003"]["摘要访问"] is False
    # A 组对 B 组面板的改动被拒。
    assert post("/api/dashboard/3/actions", {"action": "归档面板"}, team=TEAM_A).status_code == 403
    # B 组对共享给自己的 A 组面板同样不能改动。
    assert post("/api/dashboard/2/actions", {"action": "暂停面板"}, team=TEAM_B).status_code == 403
    # 归属团队可以正常改动自己的面板。
    ok = post("/api/dashboard/2/actions", {"action": "暂停面板"})
    assert ok.status_code == 200


def test_archived_panel_fields_are_locked() -> None:
    reset_store()
    post("/api/dashboard/3/actions", {"action": "归档面板"}, team=TEAM_B)
    resp = put("/api/dashboard/3/fields", {"刷新间隔": "1分钟"}, team=TEAM_B)
    assert resp.status_code == 400
    assert "归档" in resp.json()["message"]


def test_refresh_batches_visible_only_to_owner_team() -> None:
    reset_store()
    # 外部团队即使知道面板 id，也读不到共享面板的刷新批次明细。
    assert get("/api/dashboard/2/refresh-batches", team=OUTSIDER).status_code == 403


def test_missing_data_range_blocks_refresh() -> None:
    reset_store()
    # 模拟没有声明数据范围的面板，刷新必须被拦下，保证刷新结果一定绑定完整数据范围。
    store.find("dashboard", 1)["数据范围"] = ""
    resp = post("/api/dashboard/1/refresh-batches")
    assert resp.status_code == 400
    assert "数据范围" in resp.json()["message"]
