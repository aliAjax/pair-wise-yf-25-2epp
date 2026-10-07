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
- `POST /api/papers/{id}/bids`：评审意向。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行负载上限与冲突检查。
- `POST /api/assignments/auto`：主席按投标、冲突、负载自动分配，每篇凑够两名评审人；满负载排队，失败可断点续跑。
- `POST /api/users/{id}/load-limit`：主席修改评审人负载上限；超载时待回复邀请立即失效并补人。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。

自动分配以 `BEGIN IMMEDIATE` 串行化，配合 `UNIQUE(paper_id, reviewer_id)` 保证两位主席同时分配同一篇时只生成一份邀请、不重复占负载；投标 `want` 优先于 `maybe`，冲突与满负载跳过，满负载进入 `assignment_queue` 排队，容量释放后自动补位。登记利益冲突或下调负载上限后，该评审人的待回复邀请立即置为 `expired` 并按原因补人（原因写入审计历史）。自动分配逐篇提交，中途失败保留已完成部分，重试从未完成的论文断点补齐。
