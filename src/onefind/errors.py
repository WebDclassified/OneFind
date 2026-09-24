"""Typed errors mapped to CLI exit codes (docs/03-app-flow.md)."""

from __future__ import annotations


class OneFindError(Exception):
    """Base class; subclasses carry their CLI exit code."""

    exit_code = 1


# Backward-compatible alias for code written against the pre-rebrand name.
ScrydbError = OneFindError


class EnvError(OneFindError):
    """Missing extension / incompatible environment."""

    exit_code = 3


class UsageError(OneFindError):
    """Bad arguments or invalid input value."""

    exit_code = 2


class EmptyQueryError(UsageError):
    pass


class DataError(OneFindError):
    """Corpus problems: missing path, unreadable files, corrupt db."""

    exit_code = 4


class CorpusNotFoundError(DataError):
    pass
