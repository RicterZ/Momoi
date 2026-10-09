"""Memory components independent of Momoi runtime.

storage: caller-owned SQLite persistence and transaction boundaries.
retrieval: candidate search, scoring, and injected model capabilities.
indexing: document encoding, retries, and background maintenance.
text: shared text sizing and bounded excerpts.

The caller owns the database connection, schema migrations, and model transport.
"""

from .service import Memory
from .retrieval.models import MemoryRecallQuery

__all__ = ["Memory", "MemoryRecallQuery"]
