"""Memory components independent of Momoi runtime.

storage: caller-owned SQLite persistence and transaction boundaries.
retrieval: candidate search, scoring, and injected model capabilities.
text: shared text sizing and bounded excerpts.

The caller owns the database connection, schema migrations, and model transport.
"""
