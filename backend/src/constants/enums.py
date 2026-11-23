from enum import StrEnum

class Role(StrEnum):
    ADMIN = 'admin'
    ANALYST = 'analyst'
    VIEWER = 'viewer'

class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'

class Review(StrEnum):
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    RETURNED = 'returned'

class ReplayTicketStatus(StrEnum):
    DONE = 'done'
    DONE_OVERDUE = 'done_overdue'
    PENDING = 'pending'
    PENDING_OVERDUE = 'pending_overdue'
    UNREACHED = 'unreached'
