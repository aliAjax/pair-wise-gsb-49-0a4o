# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额和恢复保费和冲突检查。
- `src/ledger_rules.py`：合约台账规则——批单校验、按生效日重算各版限额、减保缺口检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面（含合约台账界面）。
- `tests/`：完整流程、规则计算、失败场景和合约台账测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 合约台账（批单与核定赔案承载力）

合约限额随批单变化，已核定赔案摊回占用承载力。台账为每个合约维护：
当前限额、已用（已核定摊回合计）、剩余承载力，以及按生效先后重算的限额版本；
每次批单确认和赔案登记都追加一条余额快照，重开页面可完整回放。

- `POST /api/treaties`：建账，`{"reference":"TREATY-01","data":{"name":"...","initial_limit":1000000,"inception_date":"2026-01-01","currency":"CNY"}}`（`underwriter`/`admin`）。
- `GET /api/treaties`：合约列表，含当前限额、已用、剩余、待确认批单数。
- `GET /api/treaties/{id}`：合约台账——限额版本、批单、已核定赔案、全部余额快照。
- `POST /api/treaties/{id}/endorsements`：登记批单（待确认），`data`为`{"effective_date":"2026-06-01","direction":"increase|decrease","amount":300000,"reason":"经办原因"}`。
- `POST /api/endorsements/{id}/confirm`：确认批单。按`(生效日, 确认序号)`重算全部限额版本；减保后任一受影响版本的可用余额低于该生效日已核定摊回时返回`409 conflict`，响应`details`给出合约、缺口（`shortfall`）和占用赔案明细（`occupancies`），事务整体回滚，批单保持待确认。
- `POST /api/treaties/{id}/claims`：登记已核定赔案，`data`为`{"claim_number":"CLM-1","approved_date":"2026-05-01","recoverable_amount":800000,"source_record_id":12}`（`claims_officer`/`admin`，`source_record_id`可选，关联已有工作流记录）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
