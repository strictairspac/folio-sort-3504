"""微页典藏的馆藏征集号整理与上架顺序单。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

from .folio_runs import Entry, SortKey, sort_on_disk

SPEC = {
    "name": "微页典藏·馆藏征集号整理",
    "summary": "按馆藏登录号生成上架顺序单，同号馆藏保留交接清册的登记顺序。",
    "customer": "微页典藏古籍部·编目与库房交接组",
    "entity_labels": ["登录批次", "有界归并", "上架顺序单"],
    "field_labels": {
        "batch_no": "登录批次号",
        "run_bytes": "初始段内存预算（字节）",
        "fan_in": "每组最多归并段数",
        "records": "交接清册",
        "item_id": "馆藏件号",
        "accession_no": "馆藏登录号",
        "title": "题名",
    },
    "guidance": (
        "按交接清册原顺序录入馆藏件。登录号形如 GJ-2026-2 或 GJ-2026-2-1，"
        "按馆藏代码、年份、流水号和分册号排序。初始段预算按 UTF-8 字节计，归并上限为 2 至 32 段。"
    ),
    "sample": {
        "batch_no": "WX-20261121-AM",
        "run_bytes": 110,
        "fan_in": 2,
        "records": [
            {"item_id": "BX-01", "accession_no": "GJ-2026-10", "title": "江南舆图"},
            {"item_id": "BX-02", "accession_no": "GJ-2026-002", "title": "海盐县志"},
            {"item_id": "BX-03", "accession_no": "QP-2025-3", "title": "访碑手记"},
            {"item_id": "BX-04", "accession_no": "GJ-2026-2-02", "title": "海盐县志续卷"},
            {"item_id": "BX-05", "accession_no": "GJ-2025-8", "title": "山川图考"},
            {"item_id": "BX-06", "accession_no": "gj-2026-2", "title": "海盐县志副本"},
            {"item_id": "BX-07", "accession_no": "GJ-2026-2-1", "title": "海盐县志首卷"},
            {"item_id": "BX-08", "accession_no": "GJ-2026-002-2", "title": "海盐县志续卷副本"},
            {"item_id": "BX-09", "accession_no": "GJ-2026-1", "title": "江口纪行"},
        ],
    },
    "expect": {
        "上架件数": 9,
        "初始磁盘段数": 5,
        "归并轮数": 3,
        "峰值归并输入段数": 2,
        "磁盘读取字节": 1494,
        "磁盘写入字节": 1494,
    },
}

_ACCESSION = re.compile(r"([A-Za-z]{2,4})-([1-9][0-9]{3})-([0-9]{1,9})(?:-([0-9]{1,4}))?")


def _accession_key(accession_no: str, position: int) -> SortKey:
    match = _ACCESSION.fullmatch(accession_no)
    if match is None:
        raise ValueError(f"第 {position} 行馆藏登录号无效，应形如 GJ-2026-2 或 GJ-2026-2-1")
    code, year, serial, part = match.groups()
    serial_number = int(serial)
    part_number = int(part) if part is not None else 0
    if serial_number == 0 or (part is not None and part_number == 0):
        raise ValueError(f"第 {position} 行馆藏登录号的流水号和显式分册号必须大于零")
    return code.upper(), int(year), serial_number, part_number, position


def _read_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}必须是非空字符串")
    return value


def _batch_inputs(payload: object) -> tuple[str, int, int, list]:
    if not isinstance(payload, dict):
        raise ValueError("登录批次必须是对象")
    batch_no = _read_text(payload.get("batch_no"), "登录批次号")
    try:
        batch_no.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("登录批次号包含无效 Unicode 字符") from exc
    run_bytes = payload.get("run_bytes")
    if type(run_bytes) is not int or run_bytes < 1:
        raise ValueError("初始段内存预算必须是正整数（字节）")
    fan_in = payload.get("fan_in")
    if type(fan_in) is not int or not 2 <= fan_in <= 32:
        raise ValueError("每组最多归并段数必须是 2 至 32 之间的整数")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("交接清册必须是记录数组")
    return batch_no, run_bytes, fan_in, records


def _entries(records: list, run_bytes: int) -> Iterator[Entry]:
    for position, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"第 {position} 行馆藏记录必须是对象")
        item_id = _read_text(record.get("item_id"), f"第 {position} 行馆藏件号")
        accession_no = _read_text(record.get("accession_no"), f"第 {position} 行馆藏登录号")
        title = _read_text(record.get("title"), f"第 {position} 行题名")
        key = _accession_key(accession_no, position)
        try:
            line = json.dumps(
                [position, item_id, accession_no, title], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8") + b"\n"
        except UnicodeEncodeError as exc:
            raise ValueError(f"第 {position} 行馆藏记录包含无效 Unicode 字符") from exc
        if len(line) > run_bytes:
            raise ValueError(
                f"第 {position} 行馆藏记录需要 {len(line)} 字节，超过初始段预算 {run_bytes} 字节"
            )
        yield key, line


def _key_from_line(line: bytes) -> SortKey:
    position, _, accession_no, _ = json.loads(line)
    return _accession_key(accession_no, position)


def _row_from_line(rank: int, line: bytes) -> dict[str, str | int]:
    position, item_id, accession_no, title = json.loads(line)
    return {
        "上架顺位": rank,
        "馆藏件号": item_id,
        "馆藏登录号": accession_no,
        "题名": title,
        "原登记顺位": position,
    }


def validate(payload):
    _, run_bytes, _, records = _batch_inputs(payload)
    for _ in _entries(records, run_bytes):
        pass


def solve(payload, engine):
    batch_no, run_bytes, fan_in, records = _batch_inputs(payload)
    order, phases = sort_on_disk(
        _entries(records, run_bytes),
        run_bytes=run_bytes,
        fan_in=fan_in,
        key_from_line=_key_from_line,
        row_from_line=_row_from_line,
    )
    metrics = {
        "上架件数": len(order),
        "初始磁盘段数": phases[0].output_runs,
        "归并轮数": len(phases) - 2,
        "初始段峰值字节": phases[0].peak_chunk_bytes,
        "峰值归并输入段数": max(phase.max_fan_in for phase in phases),
        "磁盘读取字节": sum(phase.bytes_read for phase in phases),
        "磁盘写入字节": sum(phase.bytes_written for phase in phases),
    }
    evidence = [
        {
            "阶段": phase.name,
            "输入段数": phase.input_runs,
            "输出段数": phase.output_runs,
            "归并组数": phase.merged_groups,
            "直接结转段数": phase.carried_runs,
            "最大组宽": phase.max_fan_in,
            "读取件数": phase.records_read,
            "写入件数": phase.records_written,
            "读取字节": phase.bytes_read,
            "写入字节": phase.bytes_written,
        }
        for phase in phases
    ]
    summary = (
        f"{batch_no} 已排定 {len(order)} 件馆藏的上架顺序，完成 {metrics['归并轮数']} 轮磁盘归并。"
        if order
        else f"{batch_no} 清册为空，无馆藏待上架。"
    )
    return {
        "summary": summary,
        "metrics": metrics,
        "sections": [
            {"title": "上架顺序单", "rows": order},
            {"title": "磁盘读写与归并记录", "rows": evidence},
        ],
    }
