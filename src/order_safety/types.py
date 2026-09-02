"""Order lifecycle status codes and submission errors."""


class OrderStatus:
    """주문 생애주기 명시적 상태 정의"""

    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    UNKNOWN = "UNKNOWN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    # Private WebSocket 이벤트만으로는 실제 평균 체결가를 확정하지 않는다.
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"


class AmbiguousOrderError(RuntimeError):
    """The exchange may have accepted an order, but its response was not received."""
