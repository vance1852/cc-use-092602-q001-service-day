# 修复跨时区分配日额度错位基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行与移交情景；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m rural_allocation.acceptance --workspace .
PYTHONPATH=src python3 -m housing_safety.acceptance --workspace .
PYTHONPATH=src python3 -m remediation_review.acceptance
```

三条命令使用临时 SQLite 数据库完成村镇与地块登记、家庭申请分配、危房测量分析和改造审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。

## 分配日容量口径

乡镇分配日（业务日）按**安置片区（地块资源池终点设施）所在的 IANA 时区**和该乡镇登记的
`business_day_boundary`（默认 `00:00`，即本地午夜切日）换算为 UTC 半开区间 `[起, 止)`，
而不是 UTC 自然日。因此在喀什（`Asia/Urumqi`）或西安本地午夜附近登记的临时停用，
不会被拆进不同的 UTC 自然日重复或遗漏。

容量按时间积分：

- 每段临时停用（`route_outages`）只扣减它与该业务日**真正重叠**的时段，比例为
  `(1 - capacity_percent/100) × 重叠时长/业务日时长`；同一时刻多段限制重叠时连乘。
- 未填写 `ends_at` 的限制自 `starts_at` 起持续生效（之后的业务日整日受限）。
- 核算结果只依赖限制内容并按编号稳定排序，重复核算、乱序输入结果一致。
- 已确认的分配运行结果原样落库（含容量明细），事后新增或修改限制不会静默改写历史结果。

查询与解释接口：

- `GET /routes/{route_id}/capacity?service_date=YYYY-MM-DD`：返回原始容量
  `nominal_capacity`、业务日 UTC 窗口、每段限制的 `overlap_seconds/overlap_share/deducted_capacity`
  与最终 `available_capacity`，供群众白天查询与人工核对。
- `GET /allocations/{allocation_id}`：返回已确认分配运行的最终结果及当时的容量明细快照。

登记设施时可通过 `business_day_boundary`（`HH:MM`）指定乡镇采用的业务日切日时刻。
