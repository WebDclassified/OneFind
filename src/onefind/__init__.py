"""OneFind reproduction - lexical, semantic, and hybrid search in one SQLite file.

Reimplementation of arXiv:2608.24060 for learning/portfolio purposes.
See docs/01-prd.md through docs/07-references.md for the spec system.
"""

from .errors import (
    CorpusNotFoundError,
    DataError,
    EmptyQueryError,
    EnvError,
    ScrydbError,
    UsageError,
)
from .search import Hit
from .store import Document, Index

__version__ = "0.1.0"

__all__ = [
    "CorpusNotFoundError",
    "DataError",
    "Document",
    "EmptyQueryError",
    "EnvError",
    "Hit",
    "Index",
    "ScrydbError",
    "UsageError",
    "__version__",
]
