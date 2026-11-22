"""馆藏件转件（含两批互换）的领域逻辑。

一次库房交接包含两条方向相反的转件指令：
- from_case 的 ``item_ids`` 移到 to_case；
- to_case 的 ``item_ids`` 移到 from_case。
两条指令在同一次交接里原子生效；只填一侧即为单向转件，两侧都填同一组件即互换。

本模块只做纯数据处理，不接触数据库与文件系统，便于在没有外部依赖时测试。
新清册的外部排序仍复用 :mod:`src.services.domain` 的既有口径。
"""

from __future__ import annotations

from typing import Any

from . import domain
from .domain import _batch_inputs

TRANSFER_SPEC = {
    "name": "馆藏件转件交接",
    "summary": "两批馆藏件转件或互换，两份新顺序单都排定后一起生效；任一份失败仍按原单上架。",
    "guidance": (
        "填写库房交接号与两个登录批次，分别列出要转出的馆藏件号。"
        "两侧都填件号即为两批互换，只填一侧为单向转件。同号重复提交不重复转件。"
    ),
    "field_labels": {
        "handover_no": "库房交接号",
        "from_case_id": "转出批次（档案编号）",
        "to_case_id": "接收批次（档案编号）",
        "from_item_ids": "转出批次移出的馆藏件号",
        "to_item_ids": "接收批次移出的馆藏件号",
    },
    "sample": {
        "handover_no": "HJ-20261122-01",
        "from_case_id": 1,
        "to_case_id": 2,
        "from_item_ids": ["BX-01"],
        "to_item_ids": ["BX-11"],
    },
}


class TransferError(ValueError):
    """转件业务校验失败，整条交接作废，两个批次保持原样。"""


def _read_handover_no(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TransferError("库房交接号必须是非空字符串")
    handover_no = value.strip()
    if len(handover_no) > 64:
        raise TransferError("库房交接号不能超过 64 个字符")
    return handover_no


def _read_case_id(value: object, label: str) -> int:
    # bool 是 int 的子类，显式排除。
    if type(value) is not int or value < 1:
        raise TransferError(f"{label}必须是正整数档案编号")
    return value


def _read_item_ids(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise TransferError(f"{label}必须是馆藏件号数组")
    result: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise TransferError(f"{label}第 {index} 个馆藏件号必须是非空字符串")
        result.append(item)
    if len(set(result)) != len(result):
        raise TransferError(f"{label}中存在重复馆藏件号")
    return result


def parse_request(payload: object) -> dict[str, Any]:
    """校验并规范化工作台提交的转件请求。"""
    if not isinstance(payload, dict):
        raise TransferError("转件请求必须是对象")
    handover_no = _read_handover_no(payload.get("handover_no"))
    from_case_id = _read_case_id(payload.get("from_case_id"), "转出批次")
    to_case_id = _read_case_id(payload.get("to_case_id"), "接收批次")
    if from_case_id == to_case_id:
        raise TransferError("转件的两个批次不能相同")
    from_item_ids = _read_item_ids(payload.get("from_item_ids", []), "转出批次移出件")
    to_item_ids = _read_item_ids(payload.get("to_item_ids", []), "接收批次移出件")
    if not from_item_ids and not to_item_ids:
        raise TransferError("至少要指定一个移动的馆藏件号，否则无需办理转件")
    return {
        "handover_no": handover_no,
        "from_case_id": from_case_id,
        "to_case_id": to_case_id,
        "from_item_ids": from_item_ids,
        "to_item_ids": to_item_ids,
    }


def _case_records(case_payload: object) -> tuple[str, int, int, list[dict]]:
    batch_no, run_bytes, fan_in, records = _batch_inputs(case_payload)
    for record in records:
        if not isinstance(record, dict):
            raise TransferError("批次清册记录必须是对象")
    return batch_no, run_bytes, fan_in, list(records)


def _split_records(records: list[dict], moving: list[str], label: str) -> tuple[list[dict], list[dict]]:
    """按件号把清册拆成「留批件」和「转出件」，保留原登记顺位。"""
    moving_set = set(moving)
    staying: list[dict] = []
    moved: list[dict] = []
    present: set[str] = set()
    for record in records:
        item_id = record.get("item_id")
        present.add(item_id)
        if item_id in moving_set:
            moved.append(record)
        else:
            staying.append(record)
    missing = moving_set - present
    if missing:
        raise TransferError(f"{label}中找不到馆藏件：{'、'.join(sorted(missing))}")
    return staying, moved


def _rebuild_payload(case_payload: object, own: list[dict], incoming: list[dict]) -> dict:
    """留批件保持原登记顺位在前，转入件按对方清册顺位追加在后。"""
    batch_no, run_bytes, fan_in, _ = _case_records(case_payload)
    return {
        "batch_no": batch_no,
        "run_bytes": run_bytes,
        "fan_in": fan_in,
        "records": own + incoming,
    }


def plan_transfer(request: dict, from_payload: object, to_payload: object) -> dict:
    """依据两份当前清册算出转件后的两份新清册（尚未排序、尚未写库）。"""
    _, _, _, from_records = _case_records(from_payload)
    _, _, _, to_records = _case_records(to_payload)
    from_staying, from_moved = _split_records(
        from_records, request["from_item_ids"], "转出批次")
    to_staying, to_moved = _split_records(
        to_records, request["to_item_ids"], "接收批次")
    new_from = _rebuild_payload(from_payload, from_staying, to_moved)
    new_to = _rebuild_payload(to_payload, to_staying, from_moved)
    return {
        "handover_no": request["handover_no"],
        "from": {"case_id": request["from_case_id"], "payload": new_from,
                 "moved_out": request["from_item_ids"], "moved_in": request["to_item_ids"]},
        "to": {"case_id": request["to_case_id"], "payload": new_to,
               "moved_out": request["to_item_ids"], "moved_in": request["from_item_ids"]},
    }


def prepare_reports(plan: dict) -> dict:
    """对两份新清册分别跑外部排序，生成两份新顺序单证据。

    任一份排序失败都会抛异常，由 worker 把整条交接标记为失败，
    数据库里两个批次的当前清册与已发布顺序单均不受影响。
    """
    from_report = domain.build_report(plan["from"]["payload"])
    to_report = domain.build_report(plan["to"]["payload"])
    return {**plan, "from": {**plan["from"], "report": from_report},
            "to": {**plan["to"], "report": to_report}}
