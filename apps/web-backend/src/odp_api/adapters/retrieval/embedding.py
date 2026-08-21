"""Deterministic, dependency-free embeddings for the pgvector retrieval backend."""

import hashlib
import math
import re

from odp_api.adapters.retrieval.pgvector import PGVECTOR_EMBEDDING_DIMENSIONS

# Token shape matches the pgvector provider's lexical scoring so both scoring
# paths split Chinese and Latin text the same way.
_TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)


def hash_embedding(text: str) -> list[float]:
    """Return a fixed 64-dim L2-normalized hashed token embedding.

    Each token contributes +1 or -1 to a bucket chosen by its SHA-256 digest;
    the vector is L2-normalized so cosine distance is well-defined. This is a
    deterministic stand-in with no ML dependency: replace it with a production
    embedding model while keeping the dimension synchronized with the
    ``embedding vector(64)`` column from migration 0001.
    """
    values = [0.0] * PGVECTOR_EMBEDDING_DIMENSIONS
    for token in _TOKEN_PATTERN.findall(text.lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = digest[0] % PGVECTOR_EMBEDDING_DIMENSIONS
        values[index] += 1.0 if digest[1] % 2 else -1.0
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return values
    return [value / norm for value in values]
