"""全局采集看板业务规则：状态流转、字段校验、共享权限与归档编排都收在这里。

权限口径：
- 面板归属团队内的成员可见完整字段并可执行刷新、改字段、归档等动作；
- 跨团队成员只能看到「跨团队共享」面板，且只读摘要，看不到数据范围、告警配置等明细；
- 团队内（含共享面板）只有归属团队能改动，共享不等于可写。

数据口径：
- 列表/详情只呈现面板声明的字段与最近一次成功刷新快照，刷新结果的范围以刷新批次
  绑定的不可变副本为准（见 dashboard_refresh），面板改字段不会污染历史刷新结果；
- 归档编排区分两种到达顺序：归档先行走「保留旧快照 + 在途批次拒绝」，
  刷新先行走「新快照保留 + 归档记录注明最后刷新」。
"""
from __future__ import annotations

from typing import Any

from app.services.errors import PermissionDenied, ServiceError
from app.services.storage import storage_service
from app.store import store

MODULE = "dashboard"
REQUIRED_FIELDS = ["面板编号", "面板名称", "显示指标", "数据范围"]
EDITABLE_FIELDS = ["面板名称", "显示指标", "刷新间隔", "告警配置", "数据范围", "共享范围"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已归档"]
# 动作 -> (要求的来源状态, 流转后的目标状态)，来源不对就拒绝，目标必须在状态序列里。
ACTION_RULES = {
    "启用面板": ("草稿", "已启用"),
    "暂停面板": ("已启用", "已暂停"),
    "归档面板": ("已暂停", "已归档"),
}
NEGATIVE_ACTIONS = []
SHARE_SCOPES = ["团队内", "跨团队共享"]
ARCHIVED_STATUS = "已归档"

# 跨团队只读摘要允许透出的字段；数据范围等明细不在其中。
SUMMARY_FIELDS = [
    "id",
    "面板编号",
    "面板名称",
    "显示指标",
    "status",
    "归属团队",
    "共享范围",
    "最近快照版本",
    "最近刷新时间",
    "摘要访问",
]


class DashboardService:
    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------
    def is_owner(self, panel: dict[str, Any], team: str) -> bool:
        return str(panel.get("归属团队") or "") == str(team or "")

    def is_shared(self, panel: dict[str, Any]) -> bool:
        return str(panel.get("共享范围") or "") == "跨团队共享"

    def can_view(self, panel: dict[str, Any], team: str) -> bool:
        return self.is_owner(panel, team) or self.is_shared(panel)

    def require_owner(self, panel: dict[str, Any], team: str) -> None:
        if not self.is_owner(panel, team):
            raise PermissionDenied("共享面板只读，只有归属团队可以改动")

    def require_owner_view(self, panel_id: int, team: str) -> dict[str, Any]:
        """读取刷新批次等明细入口：归属团队放行，跨团队共享访问直接 403。"""
        panel = self.require_panel(panel_id)
        self.require_owner(panel, team)
        return panel

    def require_panel(self, panel_id: int) -> dict[str, Any]:
        panel = store.find(MODULE, panel_id)
        if panel is None:
            raise ServiceError(f"看板面板 {panel_id} 不存在或已归档", status_code=404)
        return panel

    # ------------------------------------------------------------------
    # 查询（团队范围）
    # ------------------------------------------------------------------
    def visible_panels(self, team: str) -> list[dict[str, Any]]:
        return [row for row in store.rows(MODULE) if self.can_view(row, team)]

    def list_entries(
        self,
        *,
        team: str,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = self.visible_panels(team)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("面板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [self.view_entry(row, team) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int, *, team: str) -> dict[str, Any]:
        panel = self.require_panel(entry_id)
        if not self.can_view(panel, team):
            raise PermissionDenied("该面板未对当前团队共享，无权查看")
        return self.view_entry(panel, team)

    def view_entry(self, panel: dict[str, Any], team: str) -> dict[str, Any]:
        """组装面板视图：本团队看全量+最近成功快照，跨团队只给摘要。"""
        if not self.is_owner(panel, team):
            summary = {field: panel.get(field) for field in SUMMARY_FIELDS if field in panel}
            summary["摘要访问"] = True
            return summary

        view = dict(panel)
        snapshot = storage_service.latest_snapshot(int(panel["id"]))
        view["最近快照"] = snapshot
        view["摘要访问"] = False
        return view

    # ------------------------------------------------------------------
    # 登记与编辑
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any], *, operator: str, team: str) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        share_scope = str(values.get("共享范围") or "团队内").strip()
        if share_scope not in SHARE_SCOPES:
            share_scope = "团队内"
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in [
            "面板编号", "面板名称", "显示指标", "刷新间隔", "告警配置", "数据范围",
        ]})
        entry["创建人"] = operator
        entry["归属团队"] = team
        entry["共享范围"] = share_scope
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["最近成功批次编号"] = ""
        entry["最近快照id"] = None
        entry["最近快照版本"] = ""
        entry["最近刷新时间"] = ""
        rows.append(entry)
        return entry, []

    def update_fields(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        team: str,
    ) -> dict[str, Any]:
        """保存面板字段：只改面板声明本身，历史刷新批次与快照保持原样。"""
        panel = self.require_panel(entry_id)
        self.require_owner(panel, team)
        if panel.get("status") == ARCHIVED_STATUS:
            raise ServiceError("面板已归档，字段不允许再修改")
        updates = {field: values[field] for field in EDITABLE_FIELDS if field in values}
        if "数据范围" in updates and not str(updates["数据范围"] or "").strip():
            raise ServiceError("数据范围不允许清空，刷新结果必须绑定完整数据范围")
        if "共享范围" in updates and updates["共享范围"] not in SHARE_SCOPES:
            raise ServiceError(f"共享范围只支持：{'、'.join(SHARE_SCOPES)}")
        panel.update(updates)
        return panel

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str, *, team: str) -> tuple[dict[str, Any] | None, str]:
        panel = self.require_panel(entry_id)
        self.require_owner(panel, team)
        if action not in ACTION_RULES:
            raise ServiceError(f"动作「{action}」不属于全局采集看板可执行范围")
        source_status, target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            raise ServiceError(f"目标状态「{target}」不在允许的状态序列里")
        if panel.get("status") == ARCHIVED_STATUS:
            raise ServiceError("面板已归档，不允许再执行状态动作")
        if panel.get("status") != source_status:
            raise ServiceError(
                f"面板当前状态为「{panel.get('status')}」，动作「{action}」要求来源状态为「{source_status}」"
            )

        if action == "归档面板":
            return self._archive_panel(panel)

        panel["status"] = target
        panel["pending"] = target != STATUS_ORDER[-1]
        panel["abnormal"] = action in NEGATIVE_ACTIONS
        return panel, f"看板面板已{action}"

    def _archive_panel(self, panel: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """归档编排：同步存储模块的待处理清单，并按刷新/归档到达顺序处置快照。"""
        # 延迟导入，避免与 dashboard_refresh 的模块级依赖形成环。
        from app.services.dashboard_refresh import refresh_service

        panel_id = int(panel["id"])
        last_batch = str(panel.get("最近成功批次编号") or "")
        note = "刷新先行后立即归档，归档记录注明最后刷新" if last_batch else "归档先行并保留旧快照"

        # 归档先行：在途刷新批次全部拒绝，归档后不会产生新快照。
        refresh_service.reject_active_batches(panel_id)

        task = storage_service.create_archive_task(
            panel=panel,
            operator=str(panel.get("创建人") or ""),
            team=str(panel.get("归属团队") or ""),
            note=note,
        )
        storage_service.complete_archive_task(int(task["id"]), note=note)

        panel["status"] = ARCHIVED_STATUS
        panel["pending"] = False
        panel["归档任务id"] = task["id"]
        panel["归档备注"] = note
        return panel, f"看板面板已归档，存储模块已生成归档待办（{task['任务编号']}）"


dashboard_service = DashboardService()
