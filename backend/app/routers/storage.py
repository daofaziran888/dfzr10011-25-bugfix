"""采集存储分层接口：维护存储策略，并承接看板同步过来的导出快照与归档待处理清单。

- /api/storage/snapshots：存储分层按看板刷新结果生成的导出快照，保留完整数据范围；
- /api/storage/archive-tasks：修复看板刷新归档待处理清单，记录归档与刷新的到达顺序。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.storage import storage_service

router = APIRouter(prefix="/api/storage", tags=["采集存储分层"])

service = storage_service

LIST_FIELDS = ["策略编号", "存储层级", "文件格式", "压缩算法", "保留天数", "迁移规则", "清理方式", "策略状态"]
STATUSES = ["草稿", "已生效", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、已生效、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤采集存储分层列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/snapshots", response_model=PageResult[dict])
def list_snapshots(
    panel_id: int | None = Query(default=None, description="按看板面板 id 过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """导出区清单：每个看板刷新成功快照都带完整数据范围、版本与生成时间。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_snapshots(panel_id=panel_id, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/snapshots/{snapshot_id}", response_model=dict)
def get_snapshot(snapshot_id: int) -> dict[str, Any]:
    """读取单个导出快照明细；不存在时给出可读的错误说明。"""
    snapshot = service.find_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"导出快照 {snapshot_id} 不存在")
    return snapshot


@router.get("/archive-tasks", response_model=PageResult[dict])
def list_archive_tasks(
    panel_id: int | None = Query(default=None, description="按看板面板 id 过滤"),
    status: str | None = Query(default=None, description="待处理、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """修复看板刷新归档待处理清单：记录归档时保留的快照与最后刷新批次。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_archive_tasks(panel_id=panel_id, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/archive-tasks/{task_id}/complete", response_model=ActionResult)
def complete_archive_task(task_id: int) -> ActionResult:
    """将归档待办标记为已完成；归档处置本身在面板归档时已同步落库。"""
    task = service.complete_archive_task(task_id)
    return ActionResult(ok=True, message=f"归档待办 {task['任务编号']} 已完成", entry=task)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集存储分层清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "storage", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条存储策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"存储策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条存储策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="存储策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条存储策略执行启用策略、暂停策略、关闭策略；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
