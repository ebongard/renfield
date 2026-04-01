"""Request-scoped correlation ID for traceability logging."""
from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar("request_id", default="--------")
