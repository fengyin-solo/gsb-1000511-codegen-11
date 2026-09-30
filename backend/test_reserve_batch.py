"""计算批次轨道的端到端校验：通过 HTTP 接口覆盖全部业务约束。"""
from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
PASS = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAILURES.append(f"{name} {detail}")
        print(f"  ✗ {name} {detail}")


def get(path: str):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


def post(path: str, **body):
    r = client.post(path, json=body)
    return r


print("== 1. 首屏：公式版本 / 批次 / 受影响报告 ==")
track = get("/api/reserve-batches/track")
seg = {s["segment_id"]: s for s in track["segments"]}
check("历史块段锁 FV-2018", seg[1]["公式版本"].startswith("FV-2018"), seg[1]["公式版本"])
check("新块段用 FV-2024", seg[3]["公式版本"].startswith("FV-2024"))
check("每个块段都列出受影响报告", all(len(s["受影响报告"]) == 2 for s in track["segments"]))
check("历史块段挂历史批次", seg[1]["签发批次"] == "RB-2018-001")

print("== 2. 新建批次：选择块段（3、4 号新块段）==")
r = post("/api/reserve-batches", segment_ids=[3, 4], operator="测试员")
check("建批次 201", r.status_code == 201, r.text)
batch = r.json()["batch"]
bid = batch["id"]
check("批次从草稿起步", batch["status"] == "草稿", batch["status"])
check("首步为进行中高亮", batch["steps"][0]["state"] in ("active", "todo"))

print("== 3. 历史项目不能套用新模型 ==")
r = post("/api/reserve-batches", segment_ids=[1, 3], formula_id="FV-2024")
check("混合历史块段选 2024 被拒", r.status_code == 400, r.text)

print("== 4. 分步链路：套用公式（迁移面积/品位快照并试算）==")
r = post(f"/api/reserve-batches/{bid}/advance", operator="测试员")
check("选择块段推进成功", r.status_code == 200, r.text)
r = post(f"/api/reserve-batches/{bid}/advance", operator="测试员")
check("套用公式推进成功", r.status_code == 200, r.text)
batch = r.json()["batch"]
check("检查点到套用公式", batch["checkpoint"] == 1, str(batch["checkpoint"]))
snap = batch["segments"][0]["snapshot"]
res = batch["segments"][0]["result"]
check("快照含面积与品位", snap["面积"] == "15600" and snap["品位"] == "32.4", str(snap))
# 15600 * 11.5 * 3.41 * 1.03
expected_ore = round(15600 * 11.5 * 3.41 * 1.03 + 1e-9, 2)
check("2024 版矿石量含校正系数", res["矿石量"] == expected_ore, f"{res['矿石量']} != {expected_ore}")

print("== 5. 复核签发：整批回写台账/估算表/图清单/报告 ==")
r = post(f"/api/reserve-batches/{bid}/advance", operator="测试员")
check("签发成功", r.status_code == 200, r.text)
batch = r.json()["batch"]
check("批次已确认", batch["status"] == "已确认")
check("链路三步全完成", all(s["state"] == "done" for s in batch["steps"][:3]), str(batch["steps"]))

ledger = get(f"/api/reserve/{3}")
check("台账回写矿石量/金属量", ledger["矿石量"] == expected_ore and ledger["金属量"] is not None)
check("台账记录公式版本与批次", ledger["公式版本"] == "FV-2024" and ledger["签发批次"] == batch["batch_no"])
check("台账状态变已认定", ledger["status"] == "已认定")

ests = get(f"/api/reserve-batches/estimates?batch_no={batch['batch_no']}")["items"]
check("估算表两条且引用同一批次", len(ests) == 2 and all(e["batch_no"] == batch["batch_no"] for e in ests))
maps = get(f"/api/reserve-batches/maps?batch_no={batch['batch_no']}")["items"]
check("储量图清单两条", len(maps) == 2 and all(m["batch_no"] == batch["batch_no"] for m in maps))
reps = get(f"/api/reserve-batches/reports")["items"]
touched = [r for r in reps if r["批次号"] == batch["batch_no"]]
check("两个矿体共 4 条报告引用同一批次", len(touched) == 4, str([(r['报告编号'], r['批次号']) for r in touched]))

print("== 6. 幂等锁：同版本同输入重算直接回放，不新增代次 ==")
r = post(f"/api/reserve-batches/{bid}/recompute", formula_id="FV-2024", operator="测试员")
check("幂等重算 200", r.status_code == 200, r.text)
check("返回幂等回放标记", r.json()["batch"].get("idempotent") is True, str(r.json()))
gen_after = get(f"/api/reserve-batches/{bid}")["generation"]
check("幂等重算不产生新代次", gen_after == 1, str(gen_after))

print("== 7. 变更输入后重算：2024 -> 2018 产生新一代并回写 ==")
# 直接改台账品位模拟新测量数据（指纹变化）
from app.store import store
seg3 = store.find("reserve", 3)
seg3["品位"] = "33.1"
r = post(f"/api/reserve-batches/{bid}/recompute", formula_id="FV-2018", operator="测试员")
check("重算成功", r.status_code == 200, r.text)
b2 = r.json()["batch"]
check("代次递增到 2", b2["generation"] == 2, str(b2["generation"]))
check("新代次使用 2018 版", all(s["formula_id"] == "FV-2018" for s in b2["segments"]), str(b2["segments"]))
expected_2018 = round(15600 * 11.5 * 3.41 * 1.0 + 1e-9, 2)
check("2018 版无校正系数", b2["segments"][0]["result"]["矿石量"] == expected_2018,
      f"{b2['segments'][0]['result']['矿石量']} != {expected_2018}")
ledger = get(f"/api/reserve/{3}")
check("台账更新为 2018 结果与新批次代次", ledger["矿石量"] == expected_2018 and ledger["签发代数"] == 2,
      str((ledger["矿石量"], ledger["签发代数"])))
ests_all = get("/api/reserve-batches/estimates")["items"]
old_est = [e for e in ests_all if e["segment_id"] == 3 and e["generation"] == 1]
new_est = [e for e in ests_all if e["segment_id"] == 3 and e["generation"] == 2]
check("旧估算行保留但不再最新", old_est and old_est[0]["is_latest"] is False)
check("新估算行为最新", new_est and new_est[0]["is_latest"] is True)
check("幂等锁已留痕", len(get("/api/reserve-batches/locks")["items"]) >= 2)
# 输入不变时，2018 版再次重算同样必须幂等
r = post(f"/api/reserve-batches/{bid}/recompute", formula_id="FV-2018", operator="测试员")
check("2018 版二次重算幂等回放", r.status_code == 200 and r.json()["batch"].get("idempotent") is True, r.text)
check("幂等回放代次仍为 2", get(f"/api/reserve-batches/{bid}")["generation"] == 2)

print("== 8. 并发重算同一批次只允许一个通过 ==")
# 互斥锁的本质是「锁被别人持有时，后来者立刻失败」：
# 用一个执行器先占住批次锁，另一个执行器的重算必须被 409 拒绝
from app.services.reserve_batch import BatchConflict, batch_service

seg3["品位"] = "33.2"  # 新指纹，保证抢到锁的一方会真正执行
held_lock = batch_service._batch_lock(bid)
check("第一个执行器抢到批次锁", held_lock.acquire(blocking=False))
try:
    blocked = False
    blocked_detail = ""
    try:
        batch_service.recompute(bid, operator="后来者", formula_id="FV-2024")
    except BatchConflict as exc:
        blocked = True
        blocked_detail = str(exc)
    check("持锁期间并发重算被 409 拒绝", blocked, blocked_detail)
finally:
    held_lock.release()

# 锁释放后，重算正常通过
detail = batch_service.recompute(bid, operator="获胜者", formula_id="FV-2024")
check("锁释放后重算通过并确认", detail["status"] == "已确认", detail["status"])
bd = get(f"/api/reserve-batches/{bid}")
check("获胜方已确认到新一代", bd["status"] == "已确认" and bd["generation"] == 3, str((bd["status"], bd["generation"])))
ore_2024_332 = round(15600 * 11.5 * 3.41 * 1.03 + 1e-9, 2)
check("台账采用 33.2 品位的 2024 结果", get(f"/api/reserve/{3}")["矿石量"] == ore_2024_332)

print("== 9. 故障注入：回写阶段失败 -> 整批回滚到上一确认版本 ==")
seg3["品位"] = "33.5"  # 全新指纹：幂等锁未覆盖，故障注入才能真正执行
failed_gen = get(f"/api/reserve-batches/{bid}")["generation"] + 1
r = post(f"/api/reserve-batches/{bid}/recompute", formula_id="FV-2024", operator="测试员", drill_stage=3)
check("回写阶段故障返回 400", r.status_code == 400, r.text)
bd = get(f"/api/reserve-batches/{bid}")
check("批次标记已回滚", bd["status"] == "已回滚", bd["status"])
check("检查点停在回写前(=1)", bd["checkpoint"] == 1, str(bd["checkpoint"]))
check("带回滚原因", bool(bd["rollback_reason"]))
ledger = get(f"/api/reserve/{3}")
check("台账已还原到上一确认版本(33.2 的 2024 结果)", ledger["矿石量"] == ore_2024_332, str(ledger["矿石量"]))
failed_rows = [e for e in get("/api/reserve-batches/estimates")["items"]
               if e["batch_no"] == bd["batch_no"] and e["generation"] == failed_gen]
check("失败代次估算表无残留", failed_rows == [], str(failed_rows))
bad_maps = [m for m in get("/api/reserve-batches/maps")["items"]
            if m["batch_no"] == bd["batch_no"] and m["generation"] == failed_gen]
check("失败代次储量图清单无残留", bad_maps == [])

print("== 10. 复位后从检查点继续 ==")
r = post(f"/api/reserve-batches/{bid}/reset")
check("复位成功", r.status_code == 200, r.text)
check("复位后为进行中", r.json()["batch"]["status"] == "进行中")
r = post(f"/api/reserve-batches/{bid}/resume", operator="测试员")
check("续算成功", r.status_code == 200, r.text)
b3 = r.json()["batch"]
check("续算后已确认", b3["status"] == "已确认", b3["status"])
check("续算沿用检查点后的 2024 版本", b3["segments"][0]["formula_id"] == "FV-2024")
ledger = get(f"/api/reserve/{3}")
# 矿石量与品位无关（面积×厚度×体重×系数），品位只进入金属量
metal_2024_335 = round(ore_2024_332 * 33.5 / 100 + 1e-9, 2)
check("台账最终为 33.5 品位的 2024 结果",
      ledger["矿石量"] == ore_2024_332 and str(ledger["品位"]) == "33.5" and ledger["金属量"] == metal_2024_335,
      str((ledger["矿石量"], ledger["品位"], ledger["金属量"])))

# 已确认过的输入重算命中幂等锁，直接回放而不会被故障注入打断（同版本只落库一次）
r = post(f"/api/reserve-batches/{bid}/recompute", formula_id="FV-2024", drill_stage=3)
check("命中幂等锁时故障注入不生效", r.status_code == 200 and r.json()["batch"].get("idempotent") is True, r.text)

print("== 11. 阶段 1/2 中断后从检查点续算 ==")
seg3["品位"] = "34.0"
r = post(f"/api/reserve-batches/{bid}/recompute", formula_id="FV-2018", drill_stage=1)
check("快照阶段故障", r.status_code == 400)
bd = get(f"/api/reserve-batches/{bid}")
check("快照阶段失败检查点=0", bd["checkpoint"] == 0, str(bd["checkpoint"]))
post(f"/api/reserve-batches/{bid}/reset")
r = post(f"/api/reserve-batches/{bid}/resume", operator="测试员")
check("从快照检查点续算成功", r.status_code == 200, r.text)
check("续算后已确认且 2018", r.json()["batch"]["status"] == "已确认"
      and r.json()["batch"]["segments"][0]["formula_id"] == "FV-2018")

print("== 12. 历史批次重算必须保留 2018 版 ==")
r = post("/api/reserve-batches/1/recompute", formula_id="FV-2024", operator="测试员")
check("历史批次套用 2024 被拒", r.status_code == 400, r.text)
r = post("/api/reserve-batches/1/recompute", formula_id="FV-2018", operator="测试员")
check("历史批次按 2018 重算放行", r.status_code in (200,), r.text)

print()
print(f"通过 {PASS} 项；失败 {len(FAILURES)} 项")
if FAILURES:
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("全部通过")
