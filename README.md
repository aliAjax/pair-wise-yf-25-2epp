# 学术会议同行评审系统

一个仅使用 Python 3.11+ 标准库的独立示例项目。SQLite 保存数据，`http.server` 提供 JSON API 和演示页面。

## 运行

```bash
python app.py --init --seed
python app.py
```

访问 <http://127.0.0.1:8101>。默认数据库为 `review.db`，端口为 `8101`。测试：

```bash
python -m unittest -v
```

## 角色和主要接口

演示用户：`alice`、`bob`（作者），`r1`、`r2`、`r3`（评审人），`chair`（主席）。所有 API 请求应带 `X-User-Id` 请求头。

- `POST /api/papers`：提交论文。
- `GET /api/papers` / `GET /api/papers/{id}`：按角色隔离查看；评审人看到双盲视图。
- `POST /api/papers/{id}/bids`：评审意向（want/maybe/decline）。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突；冲突评审人的待回复邀请立即失效并自动补人。
- `POST /api/papers/{id}/assignments`：**一次分配**——按投标意愿（want > maybe > 无意向；decline 跳过）为论文凑满两名评审人，冲突和人手不足跳过，容量满时进入排队，可幂等重放。
- `GET /api/papers/{id}/waitlist`：查看排队补位名单。
- `POST /api/papers/{id}/allocate-jobs`：对多篇论文（默认全部可分配论文）发起一次批量分配，每篇独立提交，断点可续。
- `GET /api/allocation-jobs/{id}` / `POST /api/allocation-jobs/{id}/retry`：查看任务；失败/缺员后从断点重试，已完成论文只幂等补齐缺口，补发原因（`job_retry`）写入历史。
- `POST /api/reviewers/{id}/load-limit`：调整负载上限；下调导致超额时，最新的待回复邀请立即失效（从新到旧撤），随后全局补位。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请；拒绝会释放负载并触发排队补位（`refill_decline`）。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史，包含全部补发/撤销原因。

## 一次分配流水线

1. **排序**：投标 `want` 优先，其次 `maybe`，未投标者兜底；`decline` 不参与；同档按当前负载和用户 ID 排序。
2. **过滤**：利益冲突、已在该论文上有分配记录（含历史拒绝/撤销）的评审人跳过；候选人不足时结果带 `short=true`。
3. **容量**：每位评审人 `invited + accepted` 计数受 `load_limit` 限制；满员时进入 `waitlist` 排队而不是直接放弃。
4. **补位**：拒绝邀请、新增冲突、下调负载都会释放名额，系统立即按「本篇排队者 → 本篇新候选人 → 全局其他缺员论文」顺序补位，邀请审计带 `refill_decline` / `refill_conflict` / `refill_load` 原因。
5. **并发**：所有写入走 `BEGIN IMMEDIATE` 串行化，配合 `UNIQUE(paper_id, reviewer_id)`，两位主席同时提交同篇只生成一份邀请，负载不会重复占用。
6. **断点续跑**：批量任务按篇独立事务提交，中途失败只回滚当前篇并标记 `failed`，前置论文结果保留；`retry` 从断点继续，并幂等补齐已完成论文后续出现的缺员。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配，且冲突一经登记其待回复邀请立即撤销；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制，超额邀请按最新优先撤销；每篇论文目标两名活跃评审，满员候选人排队等候补；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。
