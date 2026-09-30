"""储量估算业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.reserve_batch import P_NEW
from app.store import store

MODULE = "reserve"
REQUIRED_FIELDS = ["块段编号", "矿体名称", "面积"]
OPTIONAL_FIELDS = ["厚度", "品位", "矿石体重", "资源类别", "块段状态", "项目属性"]
STATUS_ORDER = ["待估算", "已估算", "待评审", "已认定"]
ACTION_RULES = {"完成估算": "已估算", "提交评审": "待评审", "认定结果": "已认定"}
NEGATIVE_ACTIONS = []


class ReserveService:
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
            rows = [
                row for row in rows
                if keyword in str(row.get("块段编号", "")) or keyword in str(row.get("矿体名称", ""))
            ]
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
        for field in OPTIONAL_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry.setdefault("项目属性", P_NEW)
        entry.setdefault("资源类别", "推断的")
        # 新项目一律以当前模型为准；历史项目由批量播种或显式导入产生
        entry.setdefault("pinned_formula_id", _current_formula_id())
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"矿体块段 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于储量估算可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"矿体块段已{action}"


def _current_formula_id() -> str:
    from app.services.reserve_batch import batch_service

    try:
        return batch_service.current_formula_id()
    except Exception:  # 极端情况下公式表尚未播种，回落到内置当前版本号
        return "FV-2024"
