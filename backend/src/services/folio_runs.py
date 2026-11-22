"""Disk-backed run generation and bounded merge for the accession register."""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO

SortKey = tuple[str, int, int, int, int]
Entry = tuple[SortKey, bytes]


@dataclass
class Phase:
    name: str
    input_runs: int = 0
    output_runs: int = 0
    merged_groups: int = 0
    carried_runs: int = 0
    max_fan_in: int = 0
    records_read: int = 0
    bytes_read: int = 0
    records_written: int = 0
    bytes_written: int = 0
    peak_chunk_bytes: int = 0


def _read_lines(source: BinaryIO, phase: Phase) -> Iterator[bytes]:
    for line in source:
        phase.records_read += 1
        phase.bytes_read += len(line)
        yield line


def _write_run(path: Path, lines: Iterable[bytes], phase: Phase) -> Path:
    with path.open("wb") as output:
        for line in lines:
            phase.bytes_written += output.write(line)
            phase.records_written += 1
    phase.output_runs += 1
    return path


def _spill_runs(
    directory: Path, entries: Iterable[Entry], run_bytes: int, phase: Phase
) -> list[Path]:
    runs: list[Path] = []
    chunk: list[Entry] = []
    chunk_bytes = 0

    def flush_chunk() -> None:
        chunk.sort(key=lambda entry: entry[0])
        path = directory / f"run-0-{len(runs)}.jsonl"
        runs.append(_write_run(path, (line for _, line in chunk), phase))
        phase.peak_chunk_bytes = max(phase.peak_chunk_bytes, chunk_bytes)
        chunk.clear()

    for entry in entries:
        size = len(entry[1])
        if chunk and chunk_bytes + size > run_bytes:
            flush_chunk()
            chunk_bytes = 0
        chunk.append(entry)
        chunk_bytes += size
    if chunk:
        flush_chunk()
    return runs


def _merge_pass(
    directory: Path,
    runs: list[Path],
    round_number: int,
    fan_in: int,
    key_from_line: Callable[[bytes], SortKey],
    phase: Phase,
) -> list[Path]:
    next_runs: list[Path] = []
    for start in range(0, len(runs), fan_in):
        group = runs[start : start + fan_in]
        if len(group) == 1:
            next_runs.append(group[0])
            phase.carried_runs += 1
            phase.output_runs += 1
            continue

        path = directory / f"run-{round_number}-{len(next_runs)}.jsonl"
        with ExitStack() as readers:
            streams = [
                _read_lines(readers.enter_context(source.open("rb")), phase)
                for source in group
            ]
            merged = heapq.merge(*streams, key=key_from_line)
            next_runs.append(_write_run(path, merged, phase))
        phase.merged_groups += 1
        phase.max_fan_in = max(phase.max_fan_in, len(group))
        for source in group:
            source.unlink()
    return next_runs


def sort_on_disk(
    entries: Iterable[Entry],
    *,
    run_bytes: int,
    fan_in: int,
    key_from_line: Callable[[bytes], SortKey],
    row_from_line: Callable[[int, bytes], dict[str, str | int]],
) -> tuple[list[dict[str, str | int]], list[Phase]]:
    try:
        with TemporaryDirectory(prefix="folio-sort-") as temp_dir:
            directory = Path(temp_dir)
            phases = [Phase("生成初始段")]
            runs = _spill_runs(directory, entries, run_bytes, phases[0])
            while len(runs) > 1:
                round_number = len(phases)
                phase = Phase(f"归并第 {round_number} 轮", input_runs=len(runs))
                runs = _merge_pass(directory, runs, round_number, fan_in, key_from_line, phase)
                phases.append(phase)

            final = Phase("读取上架顺序单", input_runs=len(runs))
            phases.append(final)
            if not runs:
                return [], phases
            with runs[0].open("rb") as source:
                rows = [
                    row_from_line(rank, line)
                    for rank, line in enumerate(_read_lines(source, final), start=1)
                ]
            return rows, phases
    except OSError as exc:
        raise RuntimeError("临时磁盘读写失败，未生成上架顺序单") from exc
