from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'


class TransferStatus(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    # 两份新顺序单均已排定，等待（或正在）提交。
    PREPARED = 'prepared'
    EFFECTIVE = 'effective'
    FAILED = 'failed'
