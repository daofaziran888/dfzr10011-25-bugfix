"""全局采集看板接口：面板维护、字段保存、刷新批次与归档动作。

身份入参（HTTP 头不支持中文值，因此走查询参数/请求体）：
- GET 接口用 query 参数 team、operator；
- POST/PUT 接口在 values 里带「团队」「操作人」，也兼容 ASCII 的 X-Team/X-Operator 头；
- 缺省身份为采集A组的值班管理员，保证旧页面不传参时行为不变。
路由层不做业务判断，权限、并发与归档冲突全部在 services 内处理。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dashboard import dashboard_service
from app.services.dashboard_refresh import refresh_service

router = APIRouter(prefix="/api/dashboard", tags=["全局采集看板"])

LIST_FIELDS = ["面板编号", "面板名称", "显示指标", "刷新间隔", "告警配置", "数据范围", "创建人", "归属团队", "共享范围", "面板状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已归档"]

DEFAULT_TEAM = "采集A组"
DEFAULT_OPERATOR = "值班管理员"


def resolve_team(query_team: str | None, values: dict[str, Any] | None, x_team: str | None) -> str:
    return str(query_team or (values or {}).get("团队") or x_team or DEFAULT_TEAM).strip()


def resolve_operator(values: dict[str, Any] | None, x_operator: str | None) -> str:
    return str((values or {}).get("操作人") or x_operator or DEFAULT_OPERATOR).strip()


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按面板编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已归档"),
    team: str | None = Query(default=None, description="操作者所属团队，决定可见范围"),
    page: int = 1,
    size: int = 20,
    x_team: str | None = Header(default=None, alias="X-Team"),
) -> PageResult[dict]:
    """按团队范围过滤看板面板：本团队面板给全量，跨团队共享面板只给摘要。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    actor_team = resolve_team(team, None, x_team)
    items, total = dashboard_service.list_entries(
        team=actor_team, keyword=keyword, status=status, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    team: str | None = Query(default=None, description="操作者所属团队"),
    x_team: str | None = Header(default=None, alias="X-Team"),
) -> dict[str, Any]:
    """导出全局采集看板清单：只导出当前团队可见范围，跨团队共享面板仅含摘要。"""
    actor_team = resolve_team(team, None, x_team)
    items, total = dashboard_service.list_entries(team=actor_team, page=1, size=10000)
    return {"module": "dashboard", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(
    entry_id: int,
    team: str | None = Query(default=None, description="操作者所属团队"),
    x_team: str | None = Header(default=None, alias="X-Team"),
) -> dict[str, Any]:
    """读取单条看板面板明细（含最近成功快照）；跨团队访问共享面板只返回摘要。"""
    return dashboard_service.get_entry(entry_id, team=resolve_team(team, None, x_team))


@router.post("", response_model=ActionResult)
def create_entry(
    payload: EntryPayload,
    x_team: str | None = Header(default=None, alias="X-Team"),
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """登记一条看板面板，缺字段时说明原因而不是静默丢弃。"""
    actor_team = resolve_team(None, payload.values, x_team)
    entry, missing = dashboard_service.create_entry(
        payload.values, operator=resolve_operator(payload.values, x_operator), team=actor_team
    )
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="看板面板已登记", entry=entry)


@router.put("/{entry_id}/fields", response_model=ActionResult)
def update_fields(
    entry_id: int,
    payload: EntryPayload,
    x_team: str | None = Header(default=None, alias="X-Team"),
) -> ActionResult:
    """保存面板字段：只改面板声明，历史刷新批次与导出快照不被覆盖。"""
    actor_team = resolve_team(None, payload.values, x_team)
    entry = dashboard_service.update_fields(entry_id, payload.values, team=actor_team)
    return ActionResult(ok=True, message="看板面板字段已保存，历史刷新结果保持原范围", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    x_team: str | None = Header(default=None, alias="X-Team"),
) -> ActionResult:
    """对单条看板面板执行启用、暂停、归档；归档会同步存储模块的归档待处理清单。"""
    action = str(payload.values.get("action") or "").strip()
    actor_team = resolve_team(None, payload.values, x_team)
    entry, message = dashboard_service.run_action(entry_id, action, team=actor_team)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/refresh-batches", response_model=PageResult[dict])
def list_refresh_batches(
    entry_id: int,
    status: str | None = Query(default=None, description="刷新中、成功、刷新失败、已拒绝"),
    team: str | None = Query(default=None, description="操作者所属团队"),
    page: int = 1,
    size: int = 20,
    x_team: str | None = Header(default=None, alias="X-Team"),
) -> PageResult[dict]:
    """查看面板的刷新批次记录；跨团队成员（含共享只读）无权读取刷新明细。"""
    actor_team = resolve_team(team, None, x_team)
    dashboard_service.require_owner_view(entry_id, actor_team)
    items, total = refresh_service.list_batches(
        team=actor_team, panel_id=entry_id, status=status, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/{entry_id}/refresh-batches", response_model=ActionResult)
def start_refresh(
    entry_id: int,
    payload: EntryPayload,
    x_team: str | None = Header(default=None, alias="X-Team"),
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """申请一次看板刷新：同面板并发只允许一个批次落库，归档面板的申请登记为拒绝。"""
    actor_team = resolve_team(None, payload.values, x_team)
    batch = refresh_service.start_refresh(
        entry_id,
        operator=resolve_operator(payload.values, x_operator),
        team=actor_team,
        data_range=str(payload.values.get("数据范围") or "").strip() or None,
        remark=payload.remark,
    )
    return ActionResult(ok=True, message=f"刷新批次 {batch['批次编号']} 已落库，正在执行", entry=batch)


@router.post("/refresh-batches/{batch_id}/complete", response_model=ActionResult)
def complete_refresh(
    batch_id: int,
    payload: EntryPayload,
    x_team: str | None = Header(default=None, alias="X-Team"),
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """刷新完成回执：成功才生成导出快照并更新最近快照，失败不覆盖旧快照。"""
    actor_team = resolve_team(None, payload.values, x_team)
    success = bool(payload.values.get("成功", True))
    batch = refresh_service.complete_refresh(
        batch_id,
        operator=resolve_operator(payload.values, x_operator),
        team=actor_team,
        success=success,
        data_range=str(payload.values.get("数据范围") or "").strip() or None,
        metrics=payload.values.get("指标快照") if isinstance(payload.values.get("指标快照"), dict) else None,
        fail_reason=str(payload.values.get("失败原因") or "").strip() or None,
    )
    message = "刷新成功，导出快照已同步到存储分层" if success else "刷新失败已登记，旧快照保持不变"
    return ActionResult(ok=True, message=message, entry=batch)
