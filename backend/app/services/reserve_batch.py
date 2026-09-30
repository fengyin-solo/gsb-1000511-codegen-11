"""储量估算「计算批次轨道」业务规则。

把一次储量估算拆成可追踪的分步链路：
    选择块段 -> 套用公式 -> 复核签发
签发（或历史项目重算）成功后，结果同时回写到：
    - reserve          矿体块段台账
    - reserve_estimate 估算表
    - reserve_map      储量图清单
    - reserve_report   受影响报告（记录批次引用）

关键约束：
1. 多个入口（台账 / 估算表 / 图清单 / 报告）都只通过批次号 batch_no 引用同一次结果；
2. 历史项目按当时公式版本固化（pinned_formula_id），新项目一律用当前模型 current_formula_id；
3. 重算先迁移面积/品位快照，再用目标版本计算，最后一次性提交；
4. 幂等任务锁保证「同批次 + 同公式版本 + 同输入指纹」只落库一次；
5. 并发重算同一批次只放行一个，其余返回 409；
6. 中途失败整批回滚到上一确认版本，状态回到「已回滚/待续算」，复位后从检查点继续。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

RESERVE = "reserve"
FORMULA_TABLE = "reserve_formula"
BATCH_TABLE = "reserve_batch"
LINK_TABLE = "reserve_batch_segment"
ESTIMATE_TABLE = "reserve_estimate"
MAP_TABLE = "reserve_map"
REPORT_TABLE = "reserve_report"
LOCK_TABLE = "reserve_task_lock"

# 分步链路：首屏轨道按这三个阶段渲染
STEP_FLOW = ["选择块段", "套用公式", "复核签发"]

# 批次状态
ST_DRAFT = "草稿"
ST_IN_PROGRESS = "进行中"
ST_CONFIRMED = "已确认"
ST_ROLLED_BACK = "已回滚"

# 块段项目属性
P_HISTORICAL = "历史项目"
P_NEW = "新建项目"

# 链路步骤状态
S_DONE = "done"
S_ACTIVE = "active"
S_TODO = "todo"
S_FAILED = "failed"

# 重算的三个内部阶段（检查点 = 最后完成的阶段序号，从 -1 开始）
RECOMPUTE_STAGES = ["迁移面积品位快照", "按公式版本重算", "整批回写签发"]

# 可选的故障注入点（用于演示回滚与检查点续算）
DRILL_POINTS = {1: "snapshot", 2: "calculate", 3: "commit"}


class BatchConflict(RuntimeError):
    """并发重算同一批次时，未抢到锁的一方抛出。"""


class BatchRejected(RuntimeError):
    """业务规则不满足（阶段不对、版本不适用、数据缺失等）。"""


FORMULA_SEED = [
    {
        "id": "FV-2018",
        "name": "地质块段法（2018 评审版）",
        "expression": "矿石量 = 面积 × 厚度 × 体重；金属量 = 矿石量 × 品位",
        "version": "2018",
        "released_at": "2018-06-01",
        "status": "历史版本",
        "factor": 1.0,
        "note": "历史项目继续按当时公式版本保留，不自动升级。",
    },
    {
        "id": "FV-2024",
        "name": "地质块段法（2024 当前模型）",
        "expression": "矿石量 = 面积 × 厚度 × 体重 × 校正系数；金属量 = 矿石量 × 品位",
        "version": "2024",
        "released_at": "2024-03-15",
        "status": "当前版本",
        "factor": 1.03,
        "note": "新项目以当前模型为准；校正系数 1.03 用于岩体边界修正。",
    },
]

REPORT_SEED = [
    # 每条报告通过 矿体名称 与块段关联；签发/重算后挂到同一批次号上
    {"id": 1, "报告编号": "GEOL-R-101", "报告名称": "一号金矿体北段储量核实报告", "矿体名称": "一号金矿体", "受影响状态": "待更新", "批次号": None},
    {"id": 2, "报告编号": "GEOL-R-102", "报告名称": "一号金矿体南段年度资源报告", "矿体名称": "一号金矿体", "受影响状态": "待更新", "批次号": None},
    {"id": 3, "报告编号": "GEOL-R-201", "报告名称": "二号铜矿体详查地质报告", "矿体名称": "二号铜矿体", "受影响状态": "待更新", "批次号": None},
    {"id": 4, "报告编号": "GEOL-R-202", "报告名称": "二号铜矿体资源量估算说明", "矿体名称": "二号铜矿体", "受影响状态": "待更新", "批次号": None},
    {"id": 5, "报告编号": "GEOL-R-301", "报告名称": "三号铁矿体普查报告", "矿体名称": "三号铁矿体", "受影响状态": "待更新", "批次号": None},
    {"id": 6, "报告编号": "GEOL-R-302", "报告名称": "三号铁矿体阶段性储量通报", "矿体名称": "三号铁矿体", "受影响状态": "待更新", "批次号": None},
    {"id": 7, "报告编号": "GEOL-R-401", "报告名称": "四号银矿体勘探报告", "矿体名称": "四号银矿体", "受影响状态": "待更新", "批次号": None},
    {"id": 8, "报告编号": "GEOL-R-402", "报告名称": "四号银矿体资源储量年报", "矿体名称": "四号银矿体", "受影响状态": "待更新", "批次号": None},
    {"id": 9, "报告编号": "GEOL-R-501", "报告名称": "五号铅锌矿体核实报告", "矿体名称": "五号铅锌矿体", "受影响状态": "待更新", "批次号": None},
    {"id": 10, "报告编号": "GEOL-R-502", "报告名称": "五号铅锌矿体闭坑估算专题", "矿体名称": "五号铅锌矿体", "受影响状态": "待更新", "批次号": None},
]

# 服务启动时幂等播种一次（依赖 store 表是否已存在）
_BOOTSTRAP_LOCK = threading.Lock()
_bootstrapped = False


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _round2(value: float) -> float:
    return round(value + 1e-9, 2)


class ReserveBatchService:
    """计算批次轨道的全部读写逻辑。"""

    # 同一批次同一时刻只允许一个重算线程
    _batch_locks: dict[int, threading.Lock] = {}
    _locks_guard = threading.Lock()

    # ------------------------------------------------------------------ 初始化

    def bootstrap(self) -> None:
        """播种公式版本、报告清单，并把已认定的历史块段补一条历史签发批次。

        幂等：以批次表是否已有数据为开关，重启服务不会重复播种。
        """
        global _bootstrapped
        with _BOOTSTRAP_LOCK:
            if _bootstrapped:
                return

            formulas = store.rows(FORMULA_TABLE)
            if not formulas:
                formulas.extend(dict(item) for item in FORMULA_SEED)

            reports = store.rows(REPORT_TABLE)
            if not reports:
                reports.extend(dict(item) for item in REPORT_SEED)

            self._migrate_legacy_segments()
            self._seed_historical_batches()
            _bootstrapped = True

    def _batch_lock(self, batch_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._batch_locks.get(batch_id)
            if lock is None:
                lock = threading.Lock()
                self._batch_locks[batch_id] = lock
            return lock

    def _migrate_legacy_segments(self) -> None:
        """给旧版示例块段补齐批次轨道需要的字段，不覆盖已有值。"""
        for row in store.rows(RESERVE):
            row.setdefault("项目属性", P_NEW)
            row.setdefault("pinned_formula_id", self.current_formula_id())
            for field in ("矿石量", "金属量", "签发批次", "公式版本", "签发代数"):
                row.setdefault(field, None)

    def _seed_historical_batches(self) -> None:
        """历史已认定块段：按 2018 版补一条「当时确认」的批次，保证历史可追溯。"""
        batches = store.rows(BATCH_TABLE)
        if batches:
            return
        confirmed = [
            row for row in store.rows(RESERVE)
            if row.get("项目属性") == P_HISTORICAL and row.get("status") == "已认定"
        ]
        if not confirmed:
            return
        links = store.rows(LINK_TABLE)
        estimates = store.rows(ESTIMATE_TABLE)
        maps = store.rows(MAP_TABLE)

        # 按矿体归并历史批次，便于演示「一个矿体一条历史轨道」
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in confirmed:
            grouped.setdefault(str(row.get("矿体名称")), []).append(row)

        seq = 0
        for ore_name, segments in grouped.items():
            seq += 1
            batch_no = f"RB-2018-{seq:03d}"
            batch = {
                "id": seq,
                "batch_no": batch_no,
                "项目属性": P_HISTORICAL,
                "formula_id": "FV-2018",
                "generation": 1,
                "status": ST_CONFIRMED,
                "checkpoint": 2,
                "lock_owner": None,
                "created_at": "2018-12-20 10:00:00",
                "confirmed_at": "2018-12-20 10:00:00",
                "rolled_back_at": None,
                "rollback_reason": None,
                "remark": "历史项目按 2018 版公式补录的确认批次，重算时保留该版本。",
            }
            batches.append(batch)
            for row in segments:
                snapshot = self._snapshot_of(row)
                result = self._evaluate("FV-2018", snapshot)
                links.append({
                    "id": len(links) + 1,
                    "batch_id": seq,
                    "generation": 1,
                    "segment_id": int(row["id"]),
                    "块段编号": row.get("块段编号"),
                    "矿体名称": ore_name,
                    "项目属性": P_HISTORICAL,
                    "formula_id": "FV-2018",
                    "snapshot": snapshot,
                    "result": result,
                    "step_index": 2,
                    "step_status": S_DONE,
                })
                estimates.append({
                    "id": len(estimates) + 1,
                    "batch_no": batch_no,
                    "generation": 1,
                    "segment_id": int(row["id"]),
                    "块段编号": row.get("块段编号"),
                    "矿体名称": ore_name,
                    "formula_id": "FV-2018",
                    **{k: result[k] for k in ("面积", "厚度", "品位", "矿石体重", "校正系数", "矿石量", "金属量")},
                    "签发时间": batch["confirmed_at"],
                    "is_latest": True,
                })
                maps.append({
                    "id": len(maps) + 1,
                    "batch_no": batch_no,
                    "generation": 1,
                    "segment_id": int(row["id"]),
                    "图号": f"MAP-{row.get('块段编号', '')}",
                    "图名": f"{ore_name}储量估算图",
                    "矿体名称": ore_name,
                    "formula_id": "FV-2018",
                    "矿石量": result["矿石量"],
                    "金属量": result["金属量"],
                    "签发时间": batch["confirmed_at"],
                    "is_latest": True,
                })
                row["公式版本"] = "FV-2018"
                row["签发批次"] = batch_no
                row["签发代数"] = 1
                row["矿石量"] = result["矿石量"]
                row["金属量"] = result["金属量"]
            self._touch_reports(ore_name, batch_no, batch["confirmed_at"])

    # ------------------------------------------------------------------ 公式

    def list_formulas(self) -> list[dict[str, Any]]:
        return store.rows(FORMULA_TABLE)

    def current_formula_id(self) -> str:
        for item in store.rows(FORMULA_TABLE):
            if item.get("status") == "当前版本":
                return str(item["id"])
        return "FV-2024"

    def get_formula(self, formula_id: str) -> dict[str, Any] | None:
        for item in store.rows(FORMULA_TABLE):
            if item["id"] == formula_id:
                return item
        return None

    def _evaluate(self, formula_id: str, snap: dict[str, Any]) -> dict[str, Any]:
        formula = self.get_formula(formula_id)
        if formula is None:
            raise BatchRejected(f"公式版本 {formula_id} 不存在")
        area = _to_float(snap.get("面积"))
        thickness = _to_float(snap.get("厚度"))
        grade = _to_float(snap.get("品位"))
        density = _to_float(snap.get("矿石体重"))
        missing = [
            name for name, value in (("面积", area), ("厚度", thickness), ("品位", grade), ("矿石体重", density))
            if value is None
        ]
        if missing:
            raise BatchRejected(f"块段 {snap.get('块段编号')} 缺少可计算的数值：{'、'.join(missing)}")
        factor = float(formula.get("factor", 1.0))
        ore = _round2(area * thickness * density * factor)  # 体积 × 体重 × 校正系数
        metal = _round2(ore * grade / 100.0)  # 品位按百分数录入
        return {
            "面积": area,
            "厚度": thickness,
            "品位": grade,
            "矿石体重": density,
            "校正系数": factor,
            "矿石量": ore,
            "金属量": metal,
        }

    # ------------------------------------------------------------------ 批次查询

    def list_batches(self) -> list[dict[str, Any]]:
        return store.rows(BATCH_TABLE)

    def _find_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_TABLE, batch_id)

    def _require_batch(self, batch_id: int) -> dict[str, Any]:
        batch = self._find_batch(batch_id)
        if batch is None:
            raise BatchRejected(f"计算批次 {batch_id} 不存在")
        return batch

    def _links_of(self, batch_id: int, generation: int | None = None) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(LINK_TABLE) if int(row["batch_id"]) == batch_id]
        if generation is not None:
            rows = [row for row in rows if int(row["generation"]) == generation]
        return rows

    def track_overview(self) -> dict[str, Any]:
        """首屏：每个块段的公式版本、当前批次与受影响报告。"""
        segments = []
        for row in store.rows(RESERVE):
            ore_name = str(row.get("矿体名称") or "")
            segments.append({
                "segment_id": int(row["id"]),
                "块段编号": row.get("块段编号"),
                "矿体名称": ore_name,
                "项目属性": row.get("项目属性"),
                "状态": row.get("status"),
                "公式版本": self._formula_label(row.get("公式版本") or row.get("pinned_formula_id")),
                "签发批次": row.get("签发批次"),
                "签发代数": row.get("签发代数"),
                "矿石量": row.get("矿石量"),
                "金属量": row.get("金属量"),
                "受影响报告": [
                    {"报告编号": r["报告编号"], "报告名称": r["报告名称"], "批次号": r.get("批次号"), "受影响状态": r.get("受影响状态")}
                    for r in store.rows(REPORT_TABLE) if r.get("矿体名称") == ore_name
                ],
            })
        batches = store.rows(BATCH_TABLE)
        summary = {
            "批次总数": len(batches),
            "已确认": sum(1 for b in batches if b["status"] == ST_CONFIRMED),
            "进行中": sum(1 for b in batches if b["status"] in (ST_DRAFT, ST_IN_PROGRESS)),
            "已回滚待续算": sum(1 for b in batches if b["status"] == ST_ROLLED_BACK),
            "当前公式": self.current_formula_id(),
        }
        return {"summary": summary, "segments": segments, "batches": self._batch_list_payload(batches)}

    def _formula_label(self, formula_id: Any) -> str:
        formula = self.get_formula(str(formula_id)) if formula_id else None
        if not formula:
            return "—"
        return f"{formula['id']} {formula['name']}"

    def _batch_list_payload(self, batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for batch in batches:
            links = self._links_of(int(batch["id"]), int(batch.get("generation", 1)))
            result.append({
                "id": batch["id"],
                "batch_no": batch["batch_no"],
                "项目属性": batch.get("项目属性"),
                "formula_id": batch.get("formula_id"),
                "formula_name": self._formula_label(batch.get("formula_id")),
                "generation": batch.get("generation"),
                "status": batch.get("status"),
                "checkpoint": batch.get("checkpoint"),
                "lock_owner": batch.get("lock_owner"),
                "created_at": batch.get("created_at"),
                "confirmed_at": batch.get("confirmed_at"),
                "rolled_back_at": batch.get("rolled_back_at"),
                "rollback_reason": batch.get("rollback_reason"),
                "块段数": len(links),
                "块段": [{"块段编号": l.get("块段编号"), "矿体名称": l.get("矿体名称"), "step_status": l.get("step_status")} for l in links],
            })
        return result

    def batch_detail(self, batch_id: int) -> dict[str, Any]:
        batch = self._require_batch(batch_id)
        payload = self._batch_list_payload([batch])[0]
        links = self._links_of(batch_id)
        # 链路步骤：三个阶段各自的完成情况
        steps = []
        for index, name in enumerate(STEP_FLOW):
            if int(batch.get("checkpoint", -1)) >= index:
                state = S_DONE
            elif batch["status"] == ST_ROLLED_BACK and int(batch.get("checkpoint", -1)) + 1 == index:
                state = S_FAILED
            elif int(batch.get("checkpoint", -1)) + 1 == index and batch["status"] == ST_IN_PROGRESS:
                state = S_ACTIVE
            else:
                state = S_TODO
            steps.append({"index": index, "name": name, "state": state})

        def link_view(link: dict[str, Any]) -> dict[str, Any]:
            return {
                "generation": link["generation"],
                "segment_id": link["segment_id"],
                "块段编号": link.get("块段编号"),
                "矿体名称": link.get("矿体名称"),
                "项目属性": link.get("项目属性"),
                "formula_id": link.get("formula_id"),
                "step_index": link.get("step_index"),
                "step_status": link.get("step_status"),
                "snapshot": link.get("snapshot"),
                "result": link.get("result"),
            }

        latest_gen = int(batch.get("generation", 1))
        affected_reports = [
            r for r in store.rows(REPORT_TABLE)
            if r.get("批次号") == batch["batch_no"]
        ]
        payload.update({
            "steps": steps,
            "segments": [link_view(l) for l in links if int(l["generation"]) == latest_gen],
            "history": [link_view(l) for l in links if int(l["generation"]) != latest_gen],
            "affected_reports": affected_reports,
            "estimates": [r for r in store.rows(ESTIMATE_TABLE) if r.get("batch_no") == batch["batch_no"] and int(r.get("generation", 1)) == latest_gen],
            "maps": [r for r in store.rows(MAP_TABLE) if r.get("batch_no") == batch["batch_no"] and int(r.get("generation", 1)) == latest_gen],
        })
        return payload

    # ------------------------------------------------------------------ 创建批次

    def create_batch(self, segment_ids: list[int], operator: str, formula_id: str | None = None,
                     remark: str | None = None) -> dict[str, Any]:
        if not segment_ids:
            raise BatchRejected("请至少选择一个矿体块段")
        unique_ids = list(dict.fromkeys(int(sid) for sid in segment_ids))
        chosen: list[dict[str, Any]] = []
        for sid in unique_ids:
            row = store.find(RESERVE, sid)
            if row is None:
                raise BatchRejected(f"矿体块段 {sid} 不存在，无法加入批次")
            chosen.append(row)

        # 历史项目锁死 2018 版；新项目默认当前模型，也允许显式选当前/历史版本
        current_id = self.current_formula_id()
        kinds = {str(row.get("项目属性")) for row in chosen}
        if P_HISTORICAL in kinds and formula_id and formula_id != "FV-2018":
            raise BatchRejected("所选块段含历史项目，历史项目只能按当时公式版本 FV-2018 保留")
        target_formula = formula_id or (current_id if kinds == {P_NEW} else "FV-2018")
        if self.get_formula(target_formula) is None:
            raise BatchRejected(f"公式版本 {target_formula} 不存在")

        rows = store.rows(BATCH_TABLE)
        batch_id = max((int(b["id"]) for b in rows), default=0) + 1
        batch_no = f"RB-{_now()[:4]}-{batch_id:03d}"
        batch = {
            "id": batch_id,
            "batch_no": batch_no,
            "项目属性": P_HISTORICAL if kinds == {P_HISTORICAL} else (P_NEW if kinds == {P_NEW} else "混合批次"),
            "formula_id": target_formula,
            "generation": 1,
            "status": ST_DRAFT,
            "checkpoint": -1,
            "lock_owner": None,
            "created_at": _now(),
            "confirmed_at": None,
            "rolled_back_at": None,
            "rollback_reason": None,
            "remark": remark,
            "operator": operator,
        }
        rows.append(batch)

        links = store.rows(LINK_TABLE)
        for row in chosen:
            # 每个块段保留各自适用的公式版本（历史块段 2018，新块段当前模型）
            seg_formula = "FV-2018" if row.get("项目属性") == P_HISTORICAL else target_formula
            links.append({
                "id": max((int(l["id"]) for l in links), default=0) + 1,
                "batch_id": batch_id,
                "generation": 1,
                "segment_id": int(row["id"]),
                "块段编号": row.get("块段编号"),
                "矿体名称": row.get("矿体名称"),
                "项目属性": row.get("项目属性"),
                "formula_id": seg_formula,
                "snapshot": None,
                "result": None,
                "step_index": -1,
                "step_status": S_TODO,
            })
        return self.batch_detail(batch_id)

    # ------------------------------------------------------------------ 分步推进

    def advance_step(self, batch_id: int, operator: str) -> dict[str, Any]:
        """按 选择块段 -> 套用公式 -> 复核签发 顺序推进一个阶段。"""
        batch = self._require_batch(batch_id)
        if batch["status"] == ST_CONFIRMED:
            raise BatchRejected("批次已确认签发，如需更新请发起重算")
        if batch["status"] == ST_ROLLED_BACK:
            raise BatchRejected("批次已回滚，请先复位并从检查点续算")

        links = self._links_of(batch_id, int(batch["generation"]))
        if not links:
            raise BatchRejected("批次里没有块段，无法推进")
        next_index = int(batch.get("checkpoint", -1)) + 1
        if next_index >= len(STEP_FLOW):
            raise BatchRejected("链路已经走完")
        batch["status"] = ST_IN_PROGRESS

        if next_index == 0:
            # 选择块段：固化选定关系（建批次时已选定，这里只做确认与台账占用标记）
            for link in links:
                link["step_index"] = 0
                link["step_status"] = S_DONE
        elif next_index == 1:
            # 套用公式：迁移面积/品位快照并按各自公式版本试算，任一失败本阶段不落库
            staged = []
            for link in links:
                row = store.find(RESERVE, int(link["segment_id"]))
                snapshot = self._snapshot_of(row)
                result = self._evaluate(str(link["formula_id"]), snapshot)
                staged.append((link, snapshot, result))
            for link, snapshot, result in staged:
                link["snapshot"] = snapshot
                link["result"] = result
                link["step_index"] = 1
                link["step_status"] = S_DONE
        elif next_index == 2:
            # 复核签发：整批回写，任一失败则整批回滚到上一确认版本
            return self._commit_generation(batch, links, operator, drill_point=None)

        batch["checkpoint"] = next_index
        if next_index == len(STEP_FLOW) - 2:
            batch["status"] = ST_IN_PROGRESS
        return self.batch_detail(batch_id)

    def _snapshot_of(self, row: dict[str, Any]) -> dict[str, Any]:
        return {field: row.get(field) for field in ("块段编号", "矿体名称", "面积", "厚度", "品位", "矿石体重", "资源类别")}

    # ------------------------------------------------------------------ 签发 / 回写

    def _commit_generation(self, batch: dict[str, Any], links: list[dict[str, Any]],
                           operator: str, drill_point: str | None) -> dict[str, Any]:
        """整批提交：先保存上一确认版本的回写快照，提交失败时按快照回滚。"""
        batch_id = int(batch["id"])
        generation = int(batch["generation"])

        if drill_point == "commit":
            self._rollback(batch, links, "故障注入：复核签发阶段写入中断，已回滚到上一确认版本")
            raise BatchRejected("复核签发失败：整批回滚到上一确认版本，可复位后从检查点续算")

        # 上一确认版本快照（台账/估算表/图清单/报告各保存受影响行）
        previous: dict[str, Any] = {"reserve": [], "estimate": [], "map": [], "report": []}
        affected_ore = {str(l.get("矿体名称")) for l in links}
        segment_ids = {int(l["segment_id"]) for l in links}
        prev_batch_no = batch["batch_no"]
        for row in store.rows(RESERVE):
            if int(row["id"]) in segment_ids:
                previous["reserve"].append(dict(row))
        for table_name, key in ((ESTIMATE_TABLE, "estimate"), (MAP_TABLE, "map")):
            for row in store.rows(table_name):
                if row.get("batch_no") == prev_batch_no and int(row.get("generation", 1)) < generation:
                    previous[key].append((int(row["id"]), row.get("is_latest"), dict(row)))
        for row in store.rows(REPORT_TABLE):
            if row.get("矿体名称") in affected_ore:
                previous["report"].append((int(row["id"]), dict(row)))

        try:
            confirmed_at = _now()
            for link in links:
                result = link.get("result")
                snapshot = link.get("snapshot")
                if result is None or snapshot is None:
                    raise BatchRejected(f"块段 {link.get('块段编号')} 尚未完成公式试算")
                self._write_estimate_and_map(batch, link, confirmed_at)
                self._write_ledger(batch, link, confirmed_at, operator)
            for ore_name in affected_ore:
                self._touch_reports(ore_name, batch["batch_no"], confirmed_at)
        except BatchRejected:
            self._restore_previous(previous, batch["batch_no"], generation)
            self._rollback(batch, links, "复核签发阶段写入失败，已回滚到上一确认版本")
            raise

        batch["status"] = ST_CONFIRMED
        batch["checkpoint"] = 2
        batch["confirmed_at"] = confirmed_at
        batch["rolled_back_at"] = None
        batch["rollback_reason"] = None
        batch["lock_owner"] = None
        for link in links:
            link["step_index"] = 2
            link["step_status"] = S_DONE
            link.pop("_pending_generation", None)
        self._record_idempotent_lock(batch, links, generation)
        return self.batch_detail(batch_id)

    def _record_idempotent_lock(self, batch: dict[str, Any], links: list[dict[str, Any]], generation: int) -> None:
        """签发成功后登记幂等锁：同批次 + 同版本 + 同输入指纹只落库一次。"""
        locks = store.rows(LOCK_TABLE)
        for link in links:
            fingerprint = self._fingerprint([link])
            exists = any(
                int(lk["batch_id"]) == int(batch["id"])
                and lk["formula_id"] == link["formula_id"]
                and lk["fingerprint"] == fingerprint
                for lk in locks
            )
            if exists:
                continue
            locks.append({
                "id": max((int(lk["id"]) for lk in locks), default=0) + 1,
                "batch_id": int(batch["id"]),
                "batch_no": batch["batch_no"],
                "segment_id": int(link["segment_id"]),
                "formula_id": link["formula_id"],
                "fingerprint": fingerprint,
                "generation": generation,
                "status": "confirmed",
                "confirmed_at": _now(),
            })

    def _write_ledger(self, batch: dict[str, Any], link: dict[str, Any], confirmed_at: str, operator: str) -> None:
        row = store.find(RESERVE, int(link["segment_id"]))
        if row is None:
            raise BatchRejected(f"块段 {link.get('块段编号')} 已被删除，整批拒绝签发")
        result = link["result"]
        row.update({
            "面积": result["面积"],
            "厚度": result["厚度"],
            "品位": result["品位"],
            "矿石体重": result["矿石体重"],
            "矿石量": result["矿石量"],
            "金属量": result["金属量"],
            "公式版本": link["formula_id"],
            "签发批次": batch["batch_no"],
            "签发代数": int(batch["generation"]),
            "签发时间": confirmed_at,
            "签发人": operator,
            "status": "已认定",
            "pending": False,
            "abnormal": False,
        })

    def _write_estimate_and_map(self, batch: dict[str, Any], link: dict[str, Any], confirmed_at: str) -> None:
        result = link["result"]
        generation = int(batch["generation"])
        estimates = store.rows(ESTIMATE_TABLE)
        for row in estimates:
            if int(row.get("segment_id", -1)) == int(link["segment_id"]):
                row["is_latest"] = False
        estimates.append({
            "id": max((int(r["id"]) for r in estimates), default=0) + 1,
            "batch_no": batch["batch_no"],
            "generation": generation,
            "segment_id": int(link["segment_id"]),
            "块段编号": link.get("块段编号"),
            "矿体名称": link.get("矿体名称"),
            "formula_id": link["formula_id"],
            "面积": result["面积"],
            "厚度": result["厚度"],
            "品位": result["品位"],
            "矿石体重": result["矿石体重"],
            "校正系数": result["校正系数"],
            "矿石量": result["矿石量"],
            "金属量": result["金属量"],
            "签发时间": confirmed_at,
            "is_latest": True,
        })
        maps = store.rows(MAP_TABLE)
        for row in maps:
            if int(row.get("segment_id", -1)) == int(link["segment_id"]):
                row["is_latest"] = False
        maps.append({
            "id": max((int(r["id"]) for r in maps), default=0) + 1,
            "batch_no": batch["batch_no"],
            "generation": generation,
            "segment_id": int(link["segment_id"]),
            "图号": f"MAP-{link.get('块段编号', '')}",
            "图名": f"{link.get('矿体名称')}储量估算图",
            "矿体名称": link.get("矿体名称"),
            "formula_id": link["formula_id"],
            "矿石量": result["矿石量"],
            "金属量": result["金属量"],
            "签发时间": confirmed_at,
            "is_latest": True,
        })

    def _touch_reports(self, ore_name: str, batch_no: str, confirmed_at: str) -> None:
        """受影响报告统一引用同一个批次号——多个入口共享同一批次。"""
        for row in store.rows(REPORT_TABLE):
            if row.get("矿体名称") == ore_name:
                row["批次号"] = batch_no
                row["受影响状态"] = f"已随批次 {batch_no} 更新"
                row["更新时间"] = confirmed_at

    def _restore_previous(self, previous: dict[str, Any], batch_no: str, generation: int) -> None:
        """按保存的上一确认版本快照还原台账/估算表/图清单/报告。"""
        for saved in previous["reserve"]:
            row = store.find(RESERVE, int(saved["id"]))
            if row is not None:
                row.clear()
                row.update(saved)
        # 只撤下本次失败代次刚写入的估算表/图清单行，其他批次的历史行不受影响
        for table_name, key in ((ESTIMATE_TABLE, "estimate"), (MAP_TABLE, "map")):
            table = store.rows(table_name)
            table[:] = [
                row for row in table
                if not (row.get("batch_no") == batch_no and int(row.get("generation", 1)) == generation)
            ]
            for _row_id, _was_latest, saved in previous[key]:
                target = next((r for r in table if int(r["id"]) == int(saved["id"])), None)
                if target is not None:
                    target.clear()
                    target.update(saved)
        for _row_id, saved in previous["report"]:
            target = next((r for r in store.rows(REPORT_TABLE) if int(r["id"]) == int(saved["id"])), None)
            if target is not None:
                target.clear()
                target.update(saved)

    def _rollback(self, batch: dict[str, Any], links: list[dict[str, Any]], reason: str) -> None:
        batch["status"] = ST_ROLLED_BACK
        batch["rolled_back_at"] = _now()
        batch["rollback_reason"] = reason
        # 检查点停在「复核签发」前，保留前两阶段成果，复位后从该检查点继续
        batch["checkpoint"] = 1
        for link in links:
            if int(link.get("step_index", -1)) >= 2:
                link["step_index"] = 1
                link["step_status"] = S_TODO

    # ------------------------------------------------------------------ 重算

    def _fingerprint(self, links: list[dict[str, Any]]) -> str:
        parts = []
        for link in sorted(links, key=lambda item: int(item["segment_id"])):
            row = store.find(RESERVE, int(link["segment_id"]))
            snap = self._snapshot_of(row)
            parts.append("|".join(str(snap.get(k)) for k in ("块段编号", "面积", "厚度", "品位", "矿石体重")))
        return f"{len(links)}#{ '||'.join(parts)}"

    def _idempotent_hit(self, batch_id: int, formula_id: str, links: list[dict[str, Any]]) -> dict[str, Any] | None:
        """只有批次内每个块段都能用「目标版本 + 当前输入指纹」匹配到已确认锁，才算命中。"""
        locks = store.rows(LOCK_TABLE)
        matched_generations: set[int] = set()
        for link in links:
            # 历史块段只可能按 FV-2018 命中；新块段按请求的目标版本命中
            seg_formula = "FV-2018" if link.get("项目属性") == P_HISTORICAL else formula_id
            fingerprint = self._fingerprint([link])
            hit = next(
                (lk for lk in locks
                 if int(lk["batch_id"]) == batch_id
                 and lk["formula_id"] == seg_formula
                 and lk["fingerprint"] == fingerprint
                 and lk.get("status") == "confirmed"),
                None,
            )
            if hit is None:
                return None
            matched_generations.add(int(hit["generation"]))
        return {"generation": max(matched_generations)} if matched_generations else None

    def recompute(self, batch_id: int, operator: str, formula_id: str,
                  client_lock: str | None = None, drill_stage: int | None = None) -> dict[str, Any]:
        """对已确认批次发起重算。抢到批次锁的线程执行，其余并发请求 409。"""
        batch = self._require_batch(batch_id)
        if batch["status"] != ST_CONFIRMED:
            raise BatchRejected("只有已确认的批次才能发起重算")
        formula = self.get_formula(formula_id)
        if formula is None:
            raise BatchRejected(f"公式版本 {formula_id} 不存在")
        prev_links = self._links_of(batch_id, int(batch["generation"]))
        historical = any(l.get("项目属性") == P_HISTORICAL for l in prev_links)
        if historical and formula_id != "FV-2018":
            raise BatchRejected("批次含历史项目，历史项目继续按 FV-2018 保留，不能套用其他版本")

        lock = self._batch_lock(batch_id)
        if not lock.acquire(blocking=False):
            raise BatchConflict(f"批次 {batch['batch_no']} 正在重算，并发请求只允许一个通过")
        batch["lock_owner"] = client_lock or f"worker-{operator or 'system'}"
        try:
            hit = self._idempotent_hit(batch_id, formula_id, prev_links)
            if hit is not None:
                # 同一版本 + 同一输入只落库一次，直接回放既有确认结果
                return self.batch_detail(batch_id) | {"idempotent": True, "replayed_generation": hit["generation"]}

            new_generation = int(batch["generation"]) + 1
            staged = self._stage_generation(batch, new_generation, formula_id)
            return self._run_recompute_stages(batch, staged, operator, drill_stage)
        finally:
            if batch.get("lock_owner"):
                batch["lock_owner"] = None
            try:
                lock.release()
            except RuntimeError:
                pass

    def _run_recompute_stages(self, batch: dict[str, Any], staged: list[dict[str, Any]],
                              operator: str, drill_stage: int | None) -> dict[str, Any]:
        """依次执行「迁移快照 -> 版本重算 -> 整批回写」；checkpoint 之前的阶段直接跳过。"""
        start = int(batch.get("checkpoint", -1)) + 1
        batch["status"] = ST_IN_PROGRESS

        if start <= 0:
            # 阶段 1：迁移相关面积和品位快照
            for link in staged:
                row = store.find(RESERVE, int(link["segment_id"]))
                link["snapshot"] = self._snapshot_of(row)
                link["step_index"] = 0
                link["step_status"] = S_DONE
            batch["checkpoint"] = 0
            if drill_stage == 1:
                self._fail_recompute(batch, staged, "故障注入：快照迁移中断，待复位后从检查点继续")
                raise BatchRejected("重算在「迁移面积品位快照」阶段中断，已回滚，可复位后从检查点继续")

        if start <= 1:
            # 阶段 2：按目标公式版本重算（历史块段仍按 FV-2018）
            for link in staged:
                if link.get("snapshot") is None:
                    row = store.find(RESERVE, int(link["segment_id"]))
                    link["snapshot"] = self._snapshot_of(row)
                link["formula_id"] = "FV-2018" if link.get("项目属性") == P_HISTORICAL else batch["formula_id"]
                link["result"] = self._evaluate(str(link["formula_id"]), link["snapshot"])
                link["step_index"] = 1
                link["step_status"] = S_DONE
            batch["checkpoint"] = 1
            if drill_stage == 2:
                self._fail_recompute(batch, staged, "故障注入：公式重算中断，待复位后从检查点继续")
                raise BatchRejected("重算在「按公式版本重算」阶段中断，已回滚，可复位后从检查点继续")

        # 阶段 3：整批回写签发；失败则回滚到上一确认版本，检查点停在「已重算、未提交」
        return self._commit_generation(
            batch, staged, operator, drill_point="commit" if drill_stage == 3 else None
        )

    def _stage_generation(self, batch: dict[str, Any], generation: int, formula_id: str) -> list[dict[str, Any]]:
        """基于上一确认代次复制出新代次的链路行（草稿，未成功前不计入正式结果）。"""
        links_table = store.rows(LINK_TABLE)
        previous = self._links_of(int(batch["id"]), int(batch["generation"]))
        staged: list[dict[str, Any]] = []
        for prev in previous:
            link = {
                "id": max((int(l["id"]) for l in links_table), default=0) + 1,
                "batch_id": int(batch["id"]),
                "generation": generation,
                "segment_id": int(prev["segment_id"]),
                "块段编号": prev.get("块段编号"),
                "矿体名称": prev.get("矿体名称"),
                "项目属性": prev.get("项目属性"),
                "formula_id": formula_id,
                "snapshot": None,
                "result": None,
                "step_index": -1,
                "step_status": S_TODO,
                "_pending_generation": True,
            }
            links_table.append(link)
            staged.append(link)
        # 记住本次重算的目标版本：检查点续算时 stage 2 要按它重算
        batch["formula_id"] = formula_id
        batch["generation"] = generation
        batch["status"] = ST_IN_PROGRESS
        batch["checkpoint"] = -1
        batch["confirmed_at"] = None
        return staged

    def _fail_recompute(self, batch: dict[str, Any], staged: list[dict[str, Any]], reason: str) -> None:
        """阶段 1/2 失败：台账尚未被触碰，保留新代次草稿与检查点，整批标记为已回滚。"""
        batch["status"] = ST_ROLLED_BACK
        batch["rolled_back_at"] = _now()
        batch["rollback_reason"] = reason
        for link in staged:
            link["step_status"] = S_FAILED if int(link.get("step_index", -1)) == batch["checkpoint"] else (
                S_DONE if int(link.get("step_index", -1)) <= int(batch["checkpoint"]) else S_TODO
            )

    def resume(self, batch_id: int, operator: str, client_lock: str | None = None) -> dict[str, Any]:
        """复位后从检查点继续：按 checkpoint 跳过重算已完成的阶段，直接续跑后续阶段。"""
        batch = self._require_batch(batch_id)
        if batch["status"] == ST_CONFIRMED:
            raise BatchRejected("批次已确认，无需从检查点续算")
        if batch["status"] == ST_DRAFT:
            raise BatchRejected("批次尚未发起重算，请先走分步链路")
        lock = self._batch_lock(batch_id)
        if not lock.acquire(blocking=False):
            raise BatchConflict(f"批次 {batch['batch_no']} 正在处理中，请稍后再续算")
        batch["lock_owner"] = client_lock or f"worker-{operator or 'system'}"
        try:
            staged = self._links_of(batch_id, int(batch["generation"]))
            # 只有「上一轮重算未提交成功」的草稿代次才允许从检查点继续
            pending = [link for link in staged if link.get("_pending_generation")]
            if not pending:
                raise BatchRejected("检查点之后没有待续算的重算结果，请先发起重算")
            batch["rollback_reason"] = None
            batch["rolled_back_at"] = None
            return self._run_recompute_stages(batch, staged, operator, drill_stage=None)
        finally:
            if batch.get("lock_owner"):
                batch["lock_owner"] = None
            try:
                lock.release()
            except RuntimeError:
                pass

    def reset(self, batch_id: int) -> dict[str, Any]:
        """人工复位：清除回滚标记，链路停在最后一个检查点，等待「从检查点继续」。"""
        batch = self._require_batch(batch_id)
        if batch["status"] != ST_ROLLED_BACK:
            raise BatchRejected("只有已回滚的批次需要复位")
        batch["status"] = ST_IN_PROGRESS
        checkpoint = int(batch.get("checkpoint", 1))
        batch["checkpoint"] = checkpoint
        batch["rollback_reason"] = None
        batch["rolled_back_at"] = None
        links = self._links_of(batch_id, int(batch["generation"]))
        for link in links:
            done_index = int(link.get("step_index", -1))
            link["step_status"] = S_DONE if done_index <= checkpoint and done_index >= 0 else S_TODO
        return self.batch_detail(batch_id)

    # ------------------------------------------------------------------ 多入口视图

    def estimates(self, batch_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(ESTIMATE_TABLE)
        if batch_no:
            rows = [r for r in rows if r.get("batch_no") == batch_no]
        return rows

    def maps(self, batch_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(MAP_TABLE)
        if batch_no:
            rows = [r for r in rows if r.get("batch_no") == batch_no]
        return rows

    def reports(self, batch_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(REPORT_TABLE)
        if batch_no:
            rows = [r for r in rows if r.get("批次号") == batch_no]
        return rows


batch_service = ReserveBatchService()
