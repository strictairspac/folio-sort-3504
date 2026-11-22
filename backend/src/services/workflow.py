import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from src.config.settings import cache, engine
from src.constants.workflow import Status, TransferStatus
from src.models.tables import audit, cases, evidence, runs, transfers
from src.services import domain, transfers as transfer_domain
from src.utils.encoding import canonical
from src.utils.time import utc_time


class TransferConflict(Exception):
    """并发转件冲突：目标批次已被其他交接先行改动。"""


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(100)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def enqueue(case_id, actor):
    with engine.begin() as conn:
        found(conn, cases, case_id)
        run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                              owner=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'enqueue', run_id)
        result = found(conn, runs, run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass
    return result


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        return run.id, token, found(conn, cases, run.case_id)['payload']


def finish(run_id, token, result=None, error=None):
    digest = hashlib.sha256(canonical(result)).hexdigest() if result is not None else None
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return
        if result is not None:
            conn.execute(evidence.insert().values(run_id=run_id, report=result, digest=digest))
        log(conn, 'worker', status.value, run_id)


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        found(conn, evidence, identity)
        count = conn.execute(evidence.update().where(evidence.c.id == identity, evidence.c.reviewed_at.is_(None))
                            .values(review_note=note, reviewer=actor, reviewed_at=utc_time())).rowcount
        if count != 1:
            raise HTTPException(409, '该证据已经复核')
        log(conn, actor, 'review', identity)
        return found(conn, evidence, identity)


# ---------------------------------------------------------------------------
# 馆藏件转件：两份新顺序单原子生效
# ---------------------------------------------------------------------------

def list_transfers():
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(
            select(transfers).order_by(transfers.c.id.desc()).limit(100)).mappings()]


def get_transfer(identity):
    with engine.connect() as conn:
        return found(conn, transfers, identity)


def enqueue_transfer(payload, actor):
    """登记转件交接并排队。同一库房交接号重试返回原记录，不重复转件。"""
    request = transfer_domain.parse_request(payload)
    with engine.begin() as conn:
        # 幂等：交接号是唯一键，先查再插，已存在则原样返回（含已生效结果）。
        existing = conn.execute(
            select(transfers).where(transfers.c.handover_no == request['handover_no'])
        ).mappings().first()
        if existing is not None:
            return dict(existing), True
        from_case = found(conn, cases, request['from_case_id'])
        to_case = found(conn, cases, request['to_case_id'])
        # 入队前先做一次件号校验，尽早把行级错误退回工作台；提交时还会再校验。
        transfer_domain.plan_transfer(request, from_case['payload'], to_case['payload'])
        transfer_id = conn.execute(transfers.insert().values(
            handover_no=request['handover_no'], status=TransferStatus.QUEUED, owner=actor,
            from_case_id=request['from_case_id'], to_case_id=request['to_case_id'],
            from_revision=from_case.get('revision', 0), to_revision=to_case.get('revision', 0),
            request_json=request, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'transfer_enqueue', transfer_id)
        row = found(conn, transfers, transfer_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # Redis 只负责唤醒，数据库队列本身持久，worker 会轮询。
        pass
    return row, False


def claim_transfer():
    """认领一条排队（或心跳超时）的转件交接，返回 (id, token, request, 两批清册)。"""
    ready = or_(transfers.c.status == TransferStatus.QUEUED,
                and_(transfers.c.status == TransferStatus.RUNNING,
                     transfers.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        row = conn.execute(
            select(transfers).where(ready).order_by(transfers.c.id)
            .limit(1).with_for_update(skip_locked=True)).mappings().first()
        if row is None:
            return None
        token = str(uuid.uuid4())
        conn.execute(transfers.update().where(transfers.c.id == row['id']).values(
            status=TransferStatus.RUNNING, started_at=utc_time(), attempt_token=token))
        from_case = found(conn, cases, row['from_case_id'])
        to_case = found(conn, cases, row['to_case_id'])
        return (row['id'], token, dict(row['request_json']),
                from_case['payload'], to_case['payload'])


def store_prepared(transfer_id, token, prepared):
    """两份新顺序单都排好后暂存结果，批次当前数据此刻仍未改变。"""
    with engine.begin() as conn:
        count = conn.execute(transfers.update().where(
            transfers.c.id == transfer_id, transfers.c.attempt_token == token,
            transfers.c.status == TransferStatus.RUNNING).values(
            status=TransferStatus.PREPARED, prepared_json=prepared)).rowcount
        if not count:
            return False
    return True


def fail_transfer(transfer_id, token, message):
    with engine.begin() as conn:
        count = conn.execute(transfers.update().where(
            transfers.c.id == transfer_id, transfers.c.attempt_token == token,
            transfers.c.status.in_([TransferStatus.RUNNING, TransferStatus.PREPARED])).values(
            status=TransferStatus.FAILED, error=str(message)[:500],
            finished_at=utc_time())).rowcount
        if count:
            log(conn, 'worker', TransferStatus.FAILED.value, transfer_id)


def commit_transfer(transfer_id, token, prepared):
    """单事务提交：锁两个批次 → 版本号校验 → 更新清册 → 发布两份新顺序单。

    任一步不满足条件即抛 :class:`TransferConflict`，整个事务回滚，
    库房仍按两个批次原来的上架顺序单作业。
    """
    with engine.begin() as conn:
        current = conn.execute(
            select(transfers).where(transfers.c.id == transfer_id)
            .with_for_update()).mappings().first()
        if current is None:
            raise transfer_domain.TransferError('转件交接不存在')
        if current['status'] == TransferStatus.EFFECTIVE:
            # 极端重试：已生效则幂等返回，不再动数据。
            return False
        if current['attempt_token'] != token or current['status'] != TransferStatus.PREPARED:
            raise transfer_domain.TransferError('转件交接状态已变化，拒绝提交')

        # 按固定顺序加行锁，避免两个并发交接互相等待形成死锁。
        ordered_ids = sorted((prepared['from']['case_id'], prepared['to']['case_id']))
        locked = {
            row['id']: dict(row)
            for row in conn.execute(
                select(cases).where(cases.c.id.in_(ordered_ids))
                .order_by(cases.c.id).with_for_update()).mappings()
        }
        from_case = locked[prepared['from']['case_id']]
        to_case = locked[prepared['to']['case_id']]

        # 乐观校验：prepare 之后这两个批次若已被其他交接改动，本交接必须失败，
        # 迟完成的转件不能抹掉已经生效的转件。
        if from_case.get('revision', 0) != current['from_revision']:
            raise TransferConflict(
                f"批次 {from_case['id']} 在本交接排定后已发生其他转件，本次交接失效")
        if to_case.get('revision', 0) != current['to_revision']:
            raise TransferConflict(
                f"批次 {to_case['id']} 在本交接排定后已发生其他转件，本次交接失效")

        # 提交前依据锁内最新清册再算一遍，防止 prepare 与 commit 之间出现数据漂移。
        fresh_plan = transfer_domain.plan_transfer(
            current['request_json'], from_case['payload'], to_case['payload'])
        fresh_prepared = transfer_domain.prepare_reports(fresh_plan)

        now = utc_time()
        updated_ids: list[int] = []
        for side in ('from', 'to'):
            case_id = fresh_prepared[side]['case_id']
            new_payload = fresh_prepared[side]['payload']
            report = fresh_prepared[side]['report']
            revision = locked[case_id].get('revision', 0) + 1
            conn.execute(cases.update().where(cases.c.id == case_id).values(
                payload=new_payload, revision=revision))
            run_id = conn.execute(runs.insert().values(
                case_id=case_id, status=Status.COMPLETE, owner=current['owner'],
                attempt_token=token, started_at=now, finished_at=now,
                created_at=now, transfer_id=transfer_id)).inserted_primary_key[0]
            digest = hashlib.sha256(canonical(report)).hexdigest()
            conn.execute(evidence.insert().values(
                run_id=run_id, report=report, digest=digest))
            updated_ids.append(run_id)

        conn.execute(transfers.update().where(transfers.c.id == transfer_id).values(
            status=TransferStatus.EFFECTIVE, prepared_json=fresh_prepared,
            finished_at=now, error=None))
        log(conn, current['owner'], 'transfer_effective', transfer_id)
        return True
