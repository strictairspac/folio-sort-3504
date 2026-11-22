# 初始结构与转件交接扩展

`src/models/tables.py` 定义关系结构，API 启动时通过 `schema.create_all` 显式创建演示库的表。

## 2026-11-22 馆藏件转件扩展

新增与改动的对象（全新演示库直接由 `create_all` 建出）：

- `business_cases.revision`：批次乐观版本号，整数、非空、默认 0。每次转件生效自增，提交时凭它检测并发改动。
- `business_runs.transfer_id`：可空外键，指向 `item_transfers.id`；普通顺序单为空，转件派生的两份顺序单记录所属交接。
- `item_transfers`：转件交接表。
  - `handover_no` 唯一，作为库房交接号与幂等键，同号重试不重复转件。
  - `status` 取值 `queued / running / prepared / effective / failed`。
  - `from_case_id`、`to_case_id` 及排定时快照的 `from_revision`、`to_revision`。
  - `request_json` 存规范化请求，`prepared_json` 存两份新顺序单的暂存结果（提交前对批次当前数据不可见）。

### 已有部署的升级语句（PostgreSQL）

```sql
ALTER TABLE business_cases ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
CREATE TABLE item_transfers (
    id SERIAL PRIMARY KEY,
    handover_no VARCHAR(64) NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL,
    owner VARCHAR(40) NOT NULL,
    attempt_token VARCHAR(36),
    from_case_id INTEGER NOT NULL REFERENCES business_cases(id),
    to_case_id INTEGER NOT NULL REFERENCES business_cases(id),
    from_revision INTEGER NOT NULL,
    to_revision INTEGER NOT NULL,
    request_json JSON NOT NULL,
    prepared_json JSON,
    error TEXT,
    started_at VARCHAR(32),
    finished_at VARCHAR(32),
    created_at VARCHAR(32) NOT NULL
);
CREATE INDEX ix_item_transfers_status ON item_transfers(status);
ALTER TABLE business_runs ADD COLUMN transfer_id INTEGER REFERENCES item_transfers(id);
```

需要在正式环境升级表结构时，应将上述语句纳入版本化迁移工具（如 Alembic）管理。
