"""转件工作流的数据库测试：原子性、幂等、并发冲突与证据留存。

设计上以 SQLite 内存库验证事务与状态机语义；PostgreSQL 的行级锁
（SELECT ... FOR UPDATE SKIP LOCKED）在部署环境由相同 SQLAlchemy 语句承担，
SQLite 会忽略 FOR UPDATE 子句，不影响这里对提交协议的验证。
"""

import os

os.environ.setdefault('AUTH_KEY', 'test-key-0123456789abcdef0123456789abcdef')
os.environ.setdefault('ARTIFACTS_DIR', '/tmp/folio-test-artifacts')
# 测试不依赖 PostgreSQL 驱动；引擎对象创建是惰性的，夹具会替换成内存库。
os.environ['DB_DRIVER'] = 'sqlite'

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

import src.config.settings as settings
from src.constants.workflow import Status, TransferStatus
from src.models.tables import (audit, cases, evidence, runs, schema,
                               transfers)


class FakeCache:
    def lpush(self, *args):
        return 1

    def ltrim(self, *args):
        return True

    def ping(self):
        return True

    def brpop(self, *args, **kwargs):
        return None


@pytest.fixture()
def engine(tmp_path):
    os.environ['ARTIFACTS_DIR'] = str(tmp_path)
    settings.ARTIFACTS = str(tmp_path)
    db = create_engine(
        'sqlite://', connect_args={'check_same_thread': False},
        poolclass=StaticPool)
    settings.engine = db
    settings.cache = FakeCache()
    schema.create_all(db)
    # 服务模块在导入时绑定 settings.engine，需同步替换。
    from src.services import workflow
    workflow.engine = db
    workflow.cache = FakeCache()
    yield db
    schema.drop_all(db)
    db.dispose()


def make_payload(records, batch_no):
    return {'batch_no': batch_no, 'run_bytes': 4096, 'fan_in': 2,
            'records': [{'item_id': i, 'accession_no': f'GJ-2026-{n}',
                         'title': f'题名{i}'} for n, i in enumerate(records, start=1)]}


@pytest.fixture()
def two_cases(engine):
    from src.utils.time import utc_time
    with engine.begin() as conn:
        a = conn.execute(cases.insert().values(
            slug='case-a', title='批次甲', payload=make_payload(['A1', 'A2', 'A3'], 'BATCH-A'),
            author='operator', created_at=utc_time())).inserted_primary_key[0]
        b = conn.execute(cases.insert().values(
            slug='case-b', title='批次乙', payload=make_payload(['B1', 'B2'], 'BATCH-B'),
            author='operator', created_at=utc_time())).inserted_primary_key[0]
    return a, b


def transfer_request(a, b, **over):
    data = {'handover_no': 'HJ-20261122-01', 'from_case_id': a, 'to_case_id': b,
            'from_item_ids': ['A1'], 'to_item_ids': ['B1']}
    data.update(over)
    return data


def process_one_transfer():
    """驱动 worker 处理完队列里的一条转件。"""
    from src import worker
    assert worker.run_transfer_job() is True


def ids_of(engine, case_id):
    with engine.connect() as conn:
        payload = conn.execute(select(cases.c.payload).where(cases.c.id == case_id)).scalar_one()
    return [r['item_id'] for r in payload['records']]


def test_swap_commits_both_new_orders_atomically(engine, two_cases):
    from src.services import workflow
    a, b = two_cases
    row, duplicated = workflow.enqueue_transfer(transfer_request(a, b), 'operator')
    assert duplicated is False
    process_one_transfer()

    refreshed = workflow.get_transfer(row['id'])
    assert refreshed['status'] == TransferStatus.EFFECTIVE
    # 两个批次的清册同时完成互换。
    assert ids_of(engine, a) == ['A2', 'A3', 'B1']
    assert ids_of(engine, b) == ['B2', 'A1']
    # 版本号各自增一次。
    with engine.connect() as conn:
        revisions = dict(conn.execute(select(cases.c.id, cases.c.revision)).all())
    assert revisions == {a: 1, b: 1}
    # 同一事务发布两份完整顺序单（run + evidence）。
    with engine.connect() as conn:
        new_runs = conn.execute(select(runs).where(runs.c.transfer_id == row['id'])).mappings().all()
        assert len(new_runs) == 2
        assert all(r['status'] == Status.COMPLETE for r in new_runs)
        evidences = conn.execute(
            select(evidence).where(evidence.c.run_id.in_([r['id'] for r in new_runs]))).mappings().all()
        assert len(evidences) == 2
        assert all(e['digest'] for e in evidences)
        events = conn.execute(select(audit.c.action).where(audit.c.reference == str(row['id']))).scalars().all()
    assert 'transfer_enqueue' in events and 'transfer_effective' in events


def test_failed_transfer_leaves_original_orders_intact(engine, two_cases, monkeypatch):
    from src.services import workflow
    from src.services import transfers as transfer_domain
    a, b = two_cases
    before_a, before_b = ids_of(engine, a), ids_of(engine, b)
    # 单向转件，入队校验通过；让第二份新顺序单在排单阶段失败，验证原子回滚。
    request = transfer_request(a, b, handover_no='HJ-BAD', to_item_ids=[])
    workflow.enqueue_transfer(request, 'operator')
    original = transfer_domain.domain.build_report
    calls = {'n': 0}

    def flaky_build(payload):
        calls['n'] += 1
        if calls['n'] == 2:
            raise ValueError('模拟第二份顺序单排序失败')
        return original(payload)

    monkeypatch.setattr(transfer_domain.domain, 'build_report', flaky_build)
    process_one_transfer()
    with engine.connect() as conn:
        job = conn.execute(select(transfers).where(transfers.c.handover_no == 'HJ-BAD')).mappings().one()
        run_count = conn.execute(
            select(func.count()).select_from(runs).where(
                runs.c.transfer_id == job['id'])).scalar_one()
    assert job['status'] == TransferStatus.FAILED
    assert job['error']
    assert run_count == 0
    # 库房仍按原单上架：清册、版本号都没变。
    assert ids_of(engine, a) == before_a
    assert ids_of(engine, b) == before_b


def test_same_handover_no_retry_is_idempotent(engine, two_cases):
    from src.services import workflow
    a, b = two_cases
    request = transfer_request(a, b)
    first, duplicated_first = workflow.enqueue_transfer(request, 'operator')
    assert duplicated_first is False
    process_one_transfer()
    # 完全相同的交接号再次提交：返回原记录，不产生第二条交接，也不再转件。
    second, duplicated_second = workflow.enqueue_transfer(dict(request), 'operator')
    assert duplicated_second is True
    assert second['id'] == first['id']
    with engine.connect() as conn:
        total = conn.execute(
            select(func.count()).select_from(transfers).where(
                transfers.c.handover_no == request['handover_no'])).scalar_one()
    assert total == 1
    assert ids_of(engine, a) == ['A2', 'A3', 'B1']


def test_late_transfer_cannot_overwrite_effective_one(engine, two_cases):
    """并发交接碰到同一批：先完成的生效，迟完成的因版本号过期而失败回滚。"""
    from src.services import workflow
    from src.services.workflow import TransferConflict
    a, b = two_cases
    # 第一条交接正常生效，批次版本号推进到 1。
    workflow.enqueue_transfer(transfer_request(a, b, handover_no='HJ-FIRST'), 'operator')
    process_one_transfer()

    # 第二条交接仍引用旧版本快照（模拟它在第一条生效前就已排队并排单）。
    # 选用第一条生效后仍留在各自批次的件 A2、B2，保证排单本身可成功、只在版本校验时冲突。
    row, _ = workflow.enqueue_transfer(
        transfer_request(a, b, handover_no='HJ-LATE',
                         from_item_ids=['A2'], to_item_ids=['B2']), 'operator')
    # 手动把它的预期版本号拨回旧值，复现「排单期间批次已被改动」。
    with engine.begin() as conn:
        conn.execute(transfers.update().where(transfers.c.id == row['id']).values(
            from_revision=0, to_revision=0))
    job = workflow.claim_transfer()
    transfer_id, token, req, from_payload, to_payload = job
    from src.services.transfers import plan_transfer, prepare_reports
    prepared = prepare_reports(plan_transfer(req, from_payload, to_payload))
    assert workflow.store_prepared(transfer_id, token, prepared)
    with pytest.raises(TransferConflict):
        workflow.commit_transfer(transfer_id, token, prepared)

    workflow.fail_transfer(transfer_id, token, '冲突演示')
    with engine.connect() as conn:
        late = conn.execute(select(transfers).where(transfers.c.id == transfer_id)).mappings().one()
    assert late['status'] == TransferStatus.FAILED
    # 第一条交接的结果原样保留，没有被迟来的转件抹掉。
    assert ids_of(engine, a) == ['A2', 'A3', 'B1']
    assert ids_of(engine, b) == ['B2', 'A1']


def test_disjoint_batches_can_process_together(engine, two_cases):
    """互不相干的批次可连续办理、各自生效，互不阻塞。"""
    from src.services import workflow
    from src.utils.time import utc_time
    a, b = two_cases
    with engine.begin() as conn:
        c = conn.execute(cases.insert().values(
            slug='case-c', title='批次丙', payload=make_payload(['C1', 'C2'], 'BATCH-C'),
            author='operator', created_at=utc_time())).inserted_primary_key[0]
        d = conn.execute(cases.insert().values(
            slug='case-d', title='批次丁', payload=make_payload(['D1'], 'BATCH-D'),
            author='operator', created_at=utc_time())).inserted_primary_key[0]
    workflow.enqueue_transfer(transfer_request(a, b, handover_no='HJ-PAIR-1'), 'operator')
    workflow.enqueue_transfer(
        transfer_request(c, d, handover_no='HJ-PAIR-2', from_item_ids=['C1'],
                         to_item_ids=['D1']), 'operator')
    process_one_transfer()
    process_one_transfer()
    statuses = {row['handover_no']: row['status']
                for row in workflow.list_transfers()}
    assert statuses == {'HJ-PAIR-1': TransferStatus.EFFECTIVE,
                        'HJ-PAIR-2': TransferStatus.EFFECTIVE}
    assert ids_of(engine, c) == ['C2', 'D1']
    assert ids_of(engine, d) == ['C1']
    # 两组互不相交的批次，版本号各自独立推进、互不影响。
    with engine.connect() as conn:
        revisions = dict(conn.execute(select(cases.c.id, cases.c.revision)).all())
    assert revisions[a] == revisions[b] == revisions[c] == revisions[d] == 1


def test_signed_evidence_remains_queryable_after_transfer(engine, two_cases):
    """已签署的顺序单只追加、不改写，转件后照原样可查。"""
    from src.services import workflow
    a, b = two_cases
    # 先给批次甲跑一份普通顺序单并完成复核签署。
    run_id = workflow.enqueue(a, 'operator')
    from src import worker
    worker.run_sort_job()
    with engine.connect() as conn:
        old_evidence = conn.execute(select(evidence)).mappings().one()
        eid = old_evidence['id']
        old_digest = old_evidence['digest']
        old_report = dict(old_evidence['report'])
    workflow.review(eid, '复核通过，照此上架。', 'reviewer')

    # 再办理转件。
    workflow.enqueue_transfer(transfer_request(a, b, handover_no='HJ-AFTER-REVIEW'), 'operator')
    process_one_transfer()

    with engine.connect() as conn:
        kept = conn.execute(select(evidence).where(evidence.c.id == eid)).mappings().one()
    assert kept['digest'] == old_digest
    assert kept['report'] == old_report
    assert kept['reviewer'] == 'reviewer'
    assert kept['review_note'] == '复核通过，照此上架。'


def test_prepare_requires_matching_token(engine, two_cases):
    from src.services import workflow
    a, b = two_cases
    row, _ = workflow.enqueue_transfer(transfer_request(a, b), 'operator')
    workflow.claim_transfer()
    # 错误的 attempt token 不能写入暂存结果。
    ok = workflow.store_prepared(row['id'], 'wrong-token', {'fake': True})
    assert ok is False
    with engine.connect() as conn:
        job = conn.execute(select(transfers).where(transfers.c.id == row['id'])).mappings().one()
    assert job['status'] == TransferStatus.RUNNING
    assert job['prepared_json'] is None


@pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')
def test_worker_idle_loop_does_not_crash(engine, monkeypatch):
    """work() 空闲轮询路径必须可运行（回归曾删除 HEARTBEAT 全局变量的问题）。"""
    import threading
    from src import worker
    calls = {'n': 0}

    def stop_brpop(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] >= 2:
            raise SystemExit  # 只终止该线程，不影响测试进程
        return None

    monkeypatch.setattr(worker.cache, 'brpop', stop_brpop)
    thread = threading.Thread(target=worker.work)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), 'worker 主循环意外未退出'
    assert calls['n'] == 2


def test_one_way_transfer_keeps_receiver_append_order(engine, two_cases):
    from src.services import workflow
    a, b = two_cases
    workflow.enqueue_transfer(
        transfer_request(a, b, handover_no='HJ-ONEWAY', to_item_ids=[]), 'operator')
    process_one_transfer()
    assert ids_of(engine, a) == ['A2', 'A3']
    assert ids_of(engine, b) == ['B1', 'B2', 'A1']
