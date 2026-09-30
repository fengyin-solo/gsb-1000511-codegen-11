"""储量估算「计算批次轨道」接口。

对外暴露分步链路（选择块段 -> 套用公式 -> 复核签发）、重算续算，以及
台账 / 估算表 / 储量图清单 / 受影响报告四个共享同一批次号的入口。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.reserve_batch import (
    RECOMPUTE_STAGES,
    STEP_FLOW,
    BatchConflict,
    BatchRejected,
    batch_service,
)

router = APIRouter(prefix="/api/reserve-batches", tags=["储量估算-计算批次轨道"])


class CreateBatchPayload(BaseModel):
    segment_ids: list[int] = Field(default_factory=list)
    formula_id: str | None = None
    operator: str = "当班估算员"
    remark: str | None = None


class AdvancePayload(BaseModel):
    operator: str = "当班估算员"


class RecomputePayload(BaseModel):
    formula_id: str
    operator: str = "当班估算员"
    client_lock: str | None = None
    # 1=迁移快照阶段故障 2=公式重算阶段故障 3=整批回写阶段故障（演示回滚/续算）
    drill_stage: int | None = None


class ResumePayload(BaseModel):
    operator: str = "当班估算员"
    client_lock: str | None = None


def _reject(exc: BatchRejected) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ------------------------------------------------------------ 首屏与静态资源

@router.get("/track")
def track_overview() -> dict[str, Any]:
    """首屏：每个块段的公式版本、当前批次与受影响报告，外加批次汇总。"""
    batch_service.bootstrap()
    return batch_service.track_overview()


@router.get("/formulas")
def list_formulas() -> dict[str, Any]:
    """公式版本清单：历史版本保留，当前模型用于新项目。"""
    batch_service.bootstrap()
    return {"steps": STEP_FLOW, "recompute_stages": RECOMPUTE_STAGES, "items": batch_service.list_formulas()}


@router.get("/estimates")
def list_estimates(batch_no: str | None = None) -> dict[str, Any]:
    """估算表入口：所有结果都带批次号，可按同一批次收敛。"""
    batch_service.bootstrap()
    return {"items": batch_service.estimates(batch_no)}


@router.get("/maps")
def list_maps(batch_no: str | None = None) -> dict[str, Any]:
    """储量图清单入口：与台账、估算表共享同一批次号。"""
    batch_service.bootstrap()
    return {"items": batch_service.maps(batch_no)}


@router.get("/reports")
def list_reports(batch_no: str | None = None) -> dict[str, Any]:
    """受影响报告入口：报告随批次更新，引用同一批次号。"""
    batch_service.bootstrap()
    return {"items": batch_service.reports(batch_no)}


@router.get("/locks")
def list_locks() -> dict[str, Any]:
    """幂等任务锁视图：同一版本 + 同一输入指纹只会确认落库一次。"""
    batch_service.bootstrap()
    from app.store import store

    return {"items": store.rows("reserve_task_lock")}


# ------------------------------------------------------------ 批次集合

@router.get("")
def list_batches() -> dict[str, Any]:
    batch_service.bootstrap()
    return {"items": batch_service._batch_list_payload(batch_service.list_batches())}


@router.post("", status_code=201)
def create_batch(payload: CreateBatchPayload) -> dict[str, Any]:
    """创建批次并完成「选择块段」：历史块段锁 FV-2018，新块段默认当前模型。"""
    batch_service.bootstrap()
    try:
        return {"ok": True, "batch": batch_service.create_batch(
            segment_ids=payload.segment_ids,
            operator=payload.operator,
            formula_id=payload.formula_id,
            remark=payload.remark,
        )}
    except BatchRejected as exc:
        raise _reject(exc) from exc


# ------------------------------------------------------------ 单批次（放在静态路径之后）

@router.get("/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    batch_service.bootstrap()
    try:
        return batch_service.batch_detail(batch_id)
    except BatchRejected as exc:
        raise _reject(exc) from exc


@router.post("/{batch_id}/advance")
def advance_batch(batch_id: int, payload: AdvancePayload) -> dict[str, Any]:
    """链路推进一格：套用公式 -> 复核签发；签发即整批回写四类数据。"""
    batch_service.bootstrap()
    try:
        return {"ok": True, "batch": batch_service.advance_step(batch_id, payload.operator)}
    except BatchRejected as exc:
        raise _reject(exc) from exc
    except BatchConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{batch_id}/recompute")
def recompute_batch(batch_id: int, payload: RecomputePayload) -> dict[str, Any]:
    """对已确认批次重算：迁移快照 -> 版本重算 -> 整批回写。

    并发重算同一批次只允许一个通过（409）；同版本同输入命中幂等锁则直接回放。
    """
    batch_service.bootstrap()
    if payload.drill_stage is not None and payload.drill_stage not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="故障注入点只能是 1、2、3")
    try:
        detail = batch_service.recompute(
            batch_id=batch_id,
            operator=payload.operator,
            formula_id=payload.formula_id,
            client_lock=payload.client_lock,
            drill_stage=payload.drill_stage,
        )
        return {"ok": True, "batch": detail}
    except BatchConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BatchRejected as exc:
        raise _reject(exc) from exc


@router.post("/{batch_id}/resume")
def resume_batch(batch_id: int, payload: ResumePayload) -> dict[str, Any]:
    """复位后从检查点继续：跳过已完成的重算阶段，直接续跑后续阶段。"""
    batch_service.bootstrap()
    try:
        return {"ok": True, "batch": batch_service.resume(batch_id, payload.operator, payload.client_lock)}
    except BatchConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BatchRejected as exc:
        raise _reject(exc) from exc


@router.post("/{batch_id}/reset")
def reset_batch(batch_id: int) -> dict[str, Any]:
    """人工复位回滚态批次：清除回滚标记，停在最后一个检查点等待续算。"""
    batch_service.bootstrap()
    try:
        return {"ok": True, "batch": batch_service.reset(batch_id)}
    except BatchRejected as exc:
        raise _reject(exc) from exc
