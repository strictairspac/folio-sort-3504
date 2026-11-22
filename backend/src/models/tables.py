from sqlalchemy import (JSON, Column, ForeignKey, Integer, MetaData, String,
                        Table, Text)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False),
    # 乐观版本号：每次转件生效后自增，提交时凭它判断批次是否已被其他交接改动。
    Column('revision', Integer, nullable=False, server_default='0'))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    # 普通顺序单为空；转件派生的两份顺序单记录所属交接。
    Column('transfer_id', ForeignKey('item_transfers.id'), nullable=True),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
# 转件交接：一条交接同时编排两个批次的新顺序单，两份都成功才在同一事务生效。
transfers = Table('item_transfers', schema,
    Column('id', Integer, primary_key=True),
    # 库房交接号，幂等键：同号重试不重复转件。
    Column('handover_no', String(64), nullable=False, unique=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    # 提交时锁定的两个批次及其预期版本号（prepare 阶段快照）。
    Column('from_case_id', ForeignKey('business_cases.id'), nullable=False),
    Column('to_case_id', ForeignKey('business_cases.id'), nullable=False),
    Column('from_revision', Integer, nullable=False),
    Column('to_revision', Integer, nullable=False),
    # 转件指令与两份新顺序单的暂存结果，提交前对批次当前数据不可见。
    Column('request_json', JSON, nullable=False),
    Column('prepared_json', JSON),
    Column('error', Text),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))
