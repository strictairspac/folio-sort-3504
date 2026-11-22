import logging
from pathlib import Path
from threading import Event, Thread
from time import sleep

from src.config import settings
from src.config.settings import cache, engine
from src.services.domain import solve
from src.services.transfers import (TransferError, plan_transfer,
                                    prepare_reports)
from src.services.workflow import (TransferConflict, claim, claim_transfer,
                                   commit_transfer, fail_transfer, finish,
                                   store_prepared)


def heartbeat(stop):
    Path(settings.ARTIFACTS).mkdir(parents=True, exist_ok=True)
    beat = Path(settings.ARTIFACTS) / 'worker-heartbeat'
    while not stop.is_set():
        beat.touch()
        stop.wait(2)


def run_sort_job():
    """处理一条普通上架顺序单执行。"""
    job = claim()
    if not job:
        return False
    run_id, token, payload = job
    stop = Event()
    thread = Thread(target=heartbeat, args=(stop,), daemon=True)
    thread.start()
    try:
        result = solve(payload, engine)
        finish(run_id, token, result=result)
    except (ValueError, KeyError, TypeError) as exc:
        finish(run_id, token, error=str(exc)[:500])
    except Exception:
        logging.exception('Domain run failed: %s', run_id)
        finish(run_id, token, error='后台执行失败，请检查输入或查看 worker 日志。')
    finally:
        stop.set()
        thread.join()
    return True


def run_transfer_job():
    """处理一条转件交接：两份新顺序单都排好后，单事务一起生效。"""
    job = claim_transfer()
    if not job:
        return False
    transfer_id, token, request, from_payload, to_payload = job
    stop = Event()
    thread = Thread(target=heartbeat, args=(stop,), daemon=True)
    thread.start()
    try:
        # 阶段一（无锁、不写业务数据）：分别对两个批次排定新顺序单。
        plan = plan_transfer(request, from_payload, to_payload)
        prepared = prepare_reports(plan)
        if not store_prepared(transfer_id, token, prepared):
            return True
        # 阶段二（单事务）：锁批次、版本校验、更新清册并发布两份新顺序单。
        try:
            commit_transfer(transfer_id, token, prepared)
        except TransferConflict as exc:
            # 迟完成的交接发现批次已被改动：整单失败，原顺序单保持有效。
            fail_transfer(transfer_id, token, str(exc))
    except (TransferError, ValueError, KeyError, TypeError) as exc:
        # 任一份顺序单排不出来：整条交接失败，两个批次仍按原单上架。
        fail_transfer(transfer_id, token, str(exc))
    except Exception:
        logging.exception('Transfer job failed: %s', transfer_id)
        fail_transfer(transfer_id, token, '转件后台处理失败，请查看 worker 日志。')
    finally:
        stop.set()
        thread.join()
    return True


def work():
    Path(settings.ARTIFACTS).mkdir(parents=True, exist_ok=True)
    beat = Path(settings.ARTIFACTS) / 'worker-heartbeat'
    beat.unlink(missing_ok=True)
    while True:
        try:
            beat.touch()
            # 先排空转件交接，再处理普通执行；两类任务互不阻塞。
            if run_transfer_job() or run_sort_job():
                continue
            cache.brpop('worker:wake', timeout=1)
        except Exception:
            logging.exception('Worker loop failure')
            sleep(2)


if __name__ == '__main__':
    work()
