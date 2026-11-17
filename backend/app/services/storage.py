"""采集存储分层业务规则：状态流转、字段校验与筛选口径都收在这里。

除策略本身的状态流转外，本模块还承接全局采集看板同步过来的两类数据：
1. 看板刷新结果生成的导出快照（storage_snapshot）——按刷新结果落快照，保留完整数据范围；
2. 看板面板归档待处理清单（dashboard_archive_task）——归档请求先在这里挂账，再驱动快照处置。
快照的生成、保留与归档绑定只允许在本服务内改动，其它服务通过这里的方法进入存储模块。
"""
from __future__ import annotations

from typing import Any

from app.services.errors import ServiceError
from app.store import store

MODULE = "storage"
SNAPSHOT_MODULE = "storage_snapshot"
ARCHIVE_MODULE = "dashboard_archive_task"
REQUIRED_FIELDS = ["策略编号", "存储层级", "文件格式"]
STATUS_ORDER = ["草稿", "已生效", "已暂停", "已关闭"]
ACTION_RULES = {"启用策略": "草稿", "暂停策略": "已生效", "关闭策略": "已暂停"}
NEGATIVE_ACTIONS = []

# 看板导出快照默认落到热数据层，格式沿用平台导出约定。
DEFAULT_SNAPSHOT_LAYER = "热数据层"
DEFAULT_SNAPSHOT_FORMAT = "Parquet"


class StorageService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"存储策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集存储分层可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"存储策略已{action}"

    # ------------------------------------------------------------------
    # 看板刷新归档待处理清单
    # ------------------------------------------------------------------
    def list_archive_tasks(
        self,
        *,
        panel_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(ARCHIVE_MODULE)
        if panel_id is not None:
            rows = [row for row in rows if int(row.get("面板id", 0)) == panel_id]
        if status:
            rows = [row for row in rows if row.get("任务状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def find_archive_task(self, task_id: int) -> dict[str, Any] | None:
        return store.find(ARCHIVE_MODULE, task_id)

    def create_archive_task(
        self,
        *,
        panel: dict[str, Any],
        operator: str,
        team: str,
        note: str = "",
    ) -> dict[str, Any]:
        """面板归档时在存储模块挂一笔待处理账，并绑定当前最新导出快照。

        快照由存储模块按面板 id 自行查最新对象，避免调用方持有过期引用，
        保证「刷新先行后立即归档」时绑定的是新快照而不是旧对象。
        """
        rows = store.rows(ARCHIVE_MODULE)
        task_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        kept_snapshot = self.latest_snapshot(int(panel["id"]))
        task = {
            "id": task_id,
            "任务编号": f"ARC-{panel.get('面板编号')}-{task_id:04d}",
            "面板id": int(panel["id"]),
            "面板编号": panel.get("面板编号", ""),
            "归属团队": panel.get("归属团队", team),
            "操作人": operator,
            "任务状态": "待处理",
            "数据范围": panel.get("数据范围", ""),
            "保留快照id": kept_snapshot.get("id") if kept_snapshot else None,
            "保留快照版本": kept_snapshot.get("快照版本") if kept_snapshot else "",
            "最后刷新批次": panel.get("最近成功批次编号", ""),
            "备注": note,
            "创建时间": _now(),
        }
        rows.append(task)
        if kept_snapshot is not None:
            # 只追加保留标记，不改写快照的来源状态，保证快照出处可追溯。
            kept_snapshot["归档任务id"] = task_id
            kept_snapshot["保留状态"] = "归档保留"
        return task

    def complete_archive_task(self, task_id: int, *, note: str | None = None) -> dict[str, Any]:
        task = self.find_archive_task(task_id)
        if task is None:
            raise ServiceError(f"归档任务 {task_id} 不存在", status_code=404)
        task["任务状态"] = "已完成"
        if note:
            task["备注"] = note
        task["完成时间"] = _now()
        return task

    # ------------------------------------------------------------------
    # 看板导出快照
    # ------------------------------------------------------------------
    def list_snapshots(
        self,
        *,
        panel_id: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(SNAPSHOT_MODULE)
        if panel_id is not None:
            rows = [row for row in rows if int(row.get("面板id", 0)) == panel_id]
        rows = sorted(rows, key=lambda row: int(row.get("id", 0)))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def find_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        return store.find(SNAPSHOT_MODULE, snapshot_id)

    def latest_snapshot(self, panel_id: int) -> dict[str, Any] | None:
        """导出区对一个面板只呈现最近一次成功刷新生成的快照。"""
        rows = [
            row
            for row in store.rows(SNAPSHOT_MODULE)
            if int(row.get("面板id", 0)) == panel_id
        ]
        return max(rows, key=lambda row: int(row.get("id", 0)), default=None)

    def create_refresh_snapshot(
        self,
        *,
        panel: dict[str, Any],
        batch: dict[str, Any],
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        """按看板刷新结果生成导出快照；数据范围随刷新批次完整落盘，不接受空范围。"""
        data_range = str(batch.get("数据范围") or "").strip()
        if not data_range:
            raise ServiceError("刷新结果未绑定完整数据范围，无法生成导出快照")
        rows = store.rows(SNAPSHOT_MODULE)
        snapshot_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        snapshot = {
            "id": snapshot_id,
            "快照编号": f"SNAP-{panel.get('面板编号')}-{snapshot_id:04d}",
            "面板id": int(panel["id"]),
            "面板编号": panel.get("面板编号", ""),
            "批次id": int(batch["id"]),
            "批次编号": batch.get("批次编号", ""),
            "快照版本": batch.get("快照版本", ""),
            "数据范围": data_range,
            "指标快照": metrics,
            "存储层级": DEFAULT_SNAPSHOT_LAYER,
            "文件格式": DEFAULT_SNAPSHOT_FORMAT,
            "来源状态": "刷新成功",
            "保留状态": "导出可见",
            "归档任务id": None,
            "生成时间": _now(),
        }
        rows.append(snapshot)
        return snapshot


def _now() -> str:
    from datetime import datetime

    return datetime.now().replace(microsecond=0).isoformat()


storage_service = StorageService()
