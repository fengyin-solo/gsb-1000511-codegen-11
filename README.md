# 地质勘探数据管理平台

面向地质勘探的钻孔编录、岩心取样、物探数据、化探分析、测绘资料与储量估算的综合数据管理后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 钻孔编录 | `borehole` | 钻孔 | 钻孔编号、勘探区、孔口坐标 |
| 岩心管理 | `core` | 岩心样本 | 岩心编号、所属钻孔、取样深度起 |
| 地层划分 | `stratigraphy` | 地层单元 | 单元编号、钻孔编号、地层名称 |
| 地球物理 | `geophysics` | 物探测线 | 测线编号、勘探区、物探方法 |
| 化探分析 | `geochem` | 化探样品 | 样品编号、样品类型、采样点位 |
| 化验数据 | `assay` | 化验结果 | 化验编号、样品编号、元素名称 |
| 地质填图 | `mapping` | 填图单元 | 图幅编号、图幅名称、比例尺 |
| 测绘控制 | `survey_point` | 控制点 | 点号、点类型、坐标X |
| 钻探日志 | `drilling_log` | 钻探记录 | 日志编号、钻孔编号、钻进深度 |
| 储量估算 | `reserve` | 矿体块段 | 块段编号、矿体名称、面积 |
| 样品登记 | `sample_registry` | 送检样品 | 送检编号、样品名称、采样位置 |
| 勘探设备 | `equipment` | 勘探仪器 | 仪器编号、仪器名称、型号规格 |
| 水文地质 | `hydro` | 水文观测点 | 观测编号、观测类型、所在钻孔 |
| 剖面编录 | `section` | 实测剖面 | 剖面编号、剖面名称、剖面长度 |
| 地质报告 | `geological_report` | 勘探报告 | 报告编号、勘探区、报告类型 |
| 遥感解译 | `remote` | 遥感数据 | 数据编号、数据源、分辨率 |
| 矿产评价 | `mineral` | 矿化线索 | 线索编号、勘探区、矿种 |
| 环境地质 | `environmental` | 环境调查点 | 调查编号、调查区域、灾害类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 储量估算 · 计算批次轨道

储量估算在块段台账之外新增一条「计算批次轨道」，把一次估算拆成分步链路：

```
选择矿体块段 -> 套用公式 -> 复核签发
```

签发成功后结果**同批回写**四处，且四个入口都通过同一批次号 `batch_no` 关联：

- `reserve` 矿体块段台账（公式版本、批次号、代次、矿石量、金属量）
- `reserve_estimate` 估算表
- `reserve_map` 储量图清单
- `reserve_report` 受影响报告

版本口径：

- **历史项目**按当时公式版本（`FV-2018`）固化保留，不允许套用新模型；
- **新项目**一律以当前模型（`FV-2024`，含 1.03 校正系数）为准；
- 重算产生新「代次」（generation），旧代次估算行/图清单保留并标记 `is_latest=false`。

重算与一致性保障：

- 重算按「迁移面积品位快照 -> 按公式版本重算 -> 整批回写签发」三阶段执行，
  检查点（checkpoint）记录已完成阶段；
- **幂等任务锁** `reserve_task_lock` 以「批次 + 块段 + 公式版本 + 输入指纹」为键，
  同一版本只落库一次，重复重算直接回放既有结果（响应带 `idempotent: true`）；
- **并发重算同一批次只允许一个通过**，未抢到批次锁的请求返回 409；
- 中途失败**整批回滚到上一确认版本**（台账/估算表/图清单/报告按快照还原），
  批次置为「已回滚」，复位后调用续算接口**从检查点继续**，已完成阶段不重复执行。

接口（前缀 `/api/reserve-batches`）：

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /track` | 首屏：每块段公式版本/批次/受影响报告 + 批次汇总 |
| `GET /formulas` | 公式版本（FV-2018 历史版、FV-2024 当前模型） |
| `POST /` | 建批次并完成「选择块段」 |
| `POST /{id}/advance` | 链路推进一步（套用公式 / 复核签发） |
| `POST /{id}/recompute` | 已确认批次重算（支持 `drill_stage` 故障注入） |
| `POST /{id}/reset` / `POST /{id}/resume` | 复位 / 从检查点继续 |
| `GET /estimates` `/maps` `/reports` `/locks` | 共享同一批次号的多入口视图 |

后端自检（覆盖幂等、并发互斥、回滚、检查点续算、历史版本保留）：

```bash
cd backend && python3 test_reserve_batch.py
```

