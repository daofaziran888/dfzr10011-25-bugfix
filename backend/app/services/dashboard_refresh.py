"""看板刷新批次业务规则：刷新范围绑定、并发互斥、失败保护与归档冲突都收在这里。

关键约束（对应修复口径）：
- 刷新批次落库时复制面板当时声明的字段与完整数据范围，之后面板再怎么改字段，
  历史刷新结果都不会被覆盖，看板面板与详情始终能对上；
- 同一面板同一时刻只允许一个「刷新中」批次，并发申请直接拒绝；
- 刷新失败只登记失败批次，绝不生成或覆盖快照，导出区继续保留旧快照；
- 面板归档后到达的刷新回执/申请一律登记为「已拒绝」，归档后不会产生新快照；
- 成功刷新通过存储服务生成导出快照，快照里保留完整数据范围、版本与时间。
"""
from __future__ import annotations

import threading
from typing import Any

from app.services.dashboard import dashboard_service
from app.services.errors import ConflictState, PermissionDenied, ServiceError
from app.services.storage import storage_service
from app.store import store

MODULE = "dashboard_refresh"
ACTIVE_STATUS = "刷新中"
DECLARATION_FIELDS = ["面板名称", "显示指标", "刷新间隔", "数据范围"]


class DashboardRefreshService:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._panel_locks: dict[int, threading.Lock] = {}

    def _lock_for(self, panel_id: int) -> threading.Lock:
        with self._guard:
            lock = self._panel_locks.get(panel_id)
            if lock is None:
                lock = threading.Lock()
                self._panel_locks[panel_id] = lock
            return lock

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_batches(
        self,
        *,
        team: str,
        panel_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        visible = {int(row["id"]) for row in dashboard_service.visible_panels(team)}
        rows = [row for row in store.rows(MODULE) if int(row.get("面板id", 0)) in visible]
        if panel_id is not None:
            rows = [row for row in rows if int(row.get("面板id", 0)) == panel_id]
        if status:
            rows = [row for row in rows if row.get("批次状态") == status]
        rows = sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_batch(self, batch_id: int, *, team: str) -> dict[str, Any]:
        batch = store.find(MODULE, batch_id)
        if batch is None:
            raise ServiceError(f"刷新批次 {batch_id} 不存在", status_code=404)
        panel = dashboard_service.require_panel(int(batch["面板id"]))
        if not dashboard_service.can_view(panel, team):
            raise PermissionDenied("跨团队只能查看共享面板摘要，无权读取刷新批次明细")
        return batch

    # ------------------------------------------------------------------
    # 刷新申请
    # ------------------------------------------------------------------
    def start_refresh(
        self,
        panel_id: int,
        *,
        operator: str,
        team: str,
        data_range: str | None = None,
        remark: str | None = None,
    ) -> dict[str, Any]:
        panel = dashboard_service.require_panel(panel_id)
        dashboard_service.require_owner(panel, team)
        effective_range = str(data_range or panel.get("数据范围") or "").strip()
        if not effective_range:
            raise ServiceError("面板未声明数据范围，刷新结果无法绑定完整数据范围")

        lock = self._lock_for(panel_id)
        with lock:
            if self._active_batch(panel_id) is not None:
                raise ConflictState("该面板已有刷新批次在执行，并发刷新只允许一个批次落库")
            if panel.get("status") == "已归档":
                # 归档后到达的刷新申请：登记拒绝记录，不产生快照。
                return self._record_archived_rejection(panel, operator, effective_range, remark)
            return self._create_batch(panel, operator, effective_range, remark)

    # ------------------------------------------------------------------
    # 刷新完成回执
    # ------------------------------------------------------------------
    def complete_refresh(
        self,
        batch_id: int,
        *,
        operator: str,
        team: str,
        success: bool,
        data_range: str | None = None,
        metrics: dict[str, Any] | None = None,
        fail_reason: str | None = None,
    ) -> dict[str, Any]:
        batch = store.find(MODULE, batch_id)
        if batch is None:
            raise ServiceError(f"刷新批次 {batch_id} 不存在", status_code=404)
        panel = dashboard_service.require_panel(int(batch["面板id"]))
        dashboard_service.require_owner(panel, team)

        bound_range = str(batch.get("数据范围") or "").strip()
        effective_range = str(data_range or bound_range).strip()
        if not effective_range:
            raise ServiceError("刷新回执缺少完整数据范围，无法落库")
        if data_range and effective_range != bound_range:
            raise ServiceError("回执数据范围与批次绑定范围不一致，禁止改写刷新结果的范围")

        lock = self._lock_for(int(panel["id"]))
        with lock:
            if panel.get("status") == "已归档":
                # 归档先行、回执后到（含被归档动作拒绝的在途批次）：拒绝该批次，
                # 导出区继续显示归档保留的旧快照。
                if batch.get("批次状态") == ACTIVE_STATUS:
                    batch["批次状态"] = "已拒绝"
                    batch["归档后拒绝"] = True
                    batch["完成时间"] = _now()
                    batch["失败原因"] = "面板已归档，刷新完成回执被拒绝"
                    batch["备注"] = "归档先行并保留旧快照，本次刷新不落新快照"
                raise ConflictState(f"面板已归档，批次 {batch['批次编号']} 的刷新回执被拒绝")

            if batch.get("批次状态") != ACTIVE_STATUS:
                raise ConflictState(f"批次已结束（{batch.get('批次状态')}），不能重复回执")

            batch["完成时间"] = _now()
            if not success:
                # 失败刷新只登记原因，旧快照保持不动。
                batch["批次状态"] = "刷新失败"
                batch["失败原因"] = str(fail_reason or "刷新执行失败")
                return batch

            version = self._next_version(int(panel["id"]))
            batch["快照版本"] = version
            snapshot = storage_service.create_refresh_snapshot(
                panel=panel, batch=batch, metrics=metrics or {}
            )
            batch["快照id"] = snapshot["id"]
            batch["批次状态"] = "成功"

            panel["最近成功批次编号"] = batch["批次编号"]
            panel["最近快照id"] = snapshot["id"]
            panel["最近快照版本"] = version
            panel["最近刷新时间"] = snapshot["生成时间"]
            return batch

    def reject_active_batches(self, panel_id: int) -> list[dict[str, Any]]:
        """面板归档时调用：在途批次全部标记为归档后拒绝，不允许再落快照。"""
        lock = self._lock_for(panel_id)
        with lock:
            rejected: list[dict[str, Any]] = []
            for batch in store.rows(MODULE):
                if int(batch.get("面板id", 0)) == panel_id and batch.get("批次状态") == ACTIVE_STATUS:
                    batch["批次状态"] = "已拒绝"
                    batch["归档后拒绝"] = True
                    batch["完成时间"] = _now()
                    batch["失败原因"] = "面板已归档，在途刷新回执被拒绝"
                    batch["备注"] = "归档先行并保留旧快照，在途批次不落新快照"
                    rejected.append(batch)
            return rejected

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _active_batch(self, panel_id: int) -> dict[str, Any] | None:
        for batch in store.rows(MODULE):
            if int(batch.get("面板id", 0)) == panel_id and batch.get("批次状态") == ACTIVE_STATUS:
                return batch
        return None

    def _next_version(self, panel_id: int) -> str:
        snapshots, _ = storage_service.list_snapshots(panel_id=panel_id, page=1, size=10000)
        return f"v{len(snapshots) + 1}"

    def _create_batch(
        self,
        panel: dict[str, Any],
        operator: str,
        data_range: str,
        remark: str | None,
    ) -> dict[str, Any]:
        rows = store.rows(MODULE)
        batch_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        seq = sum(1 for row in rows if int(row.get("面板id", 0)) == int(panel["id"])) + 1
        declaration = {field: panel.get(field, "") for field in DECLARATION_FIELDS}
        batch = {
            "id": batch_id,
            "批次编号": f"REF-{panel.get('面板编号')}-{seq:04d}",
            "面板id": int(panel["id"]),
            "面板编号": panel.get("面板编号", ""),
            "归属团队": panel.get("归属团队", ""),
            "操作人": operator,
            "批次状态": ACTIVE_STATUS,
            "数据范围": data_range,
            "面板声明": declaration,
            "快照版本": "",
            "快照id": None,
            "失败原因": "",
            "归档后拒绝": False,
            "备注": remark or "",
            "申请时间": _now(),
            "完成时间": "",
        }
        rows.append(batch)
        return batch

    def _record_archived_rejection(
        self,
        panel: dict[str, Any],
        operator: str,
        data_range: str,
        remark: str | None,
    ) -> dict[str, Any]:
        batch = self._create_batch(panel, operator, data_range, remark)
        batch["批次状态"] = "已拒绝"
        batch["归档后拒绝"] = True
        batch["失败原因"] = "面板已归档，归档后不能生成新快照"
        batch["完成时间"] = _now()
        raise ConflictState(f"面板已归档，刷新申请被拒绝（批次 {batch['批次编号']} 已留痕）")


refresh_service = DashboardRefreshService()


def _now() -> str:
    from datetime import datetime

    return datetime.now().replace(microsecond=0).isoformat()
