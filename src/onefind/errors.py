"""Typed errors mapped to CLI exit codes (docs/03-app-flow.md)."""

from __future__ import annotations


class ScrydbError(Exception):
    """Base class; subclasses carry their CLI exit code."""

    exit_code = 1


class EnvError(ScrydbError):
    """Missing extension / incompatible environment."""

    exit_code = 3


class UsageError(ScrydbError):
    """Bad arguments or invalid input value."""

    exit_code = 2


class EmptyQueryError(UsageError):
    pass


class DataError(ScrydbError):
    """Corpus problems: missing path, unreadable files, corrupt db."""

    exit_code = 4


class CorpusNotFoundError(DataError):
    pass
