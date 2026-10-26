"""Attempt policy owned by the durable queue, isolated across async workers."""
from contextlib import contextmanager
from contextvars import ContextVar

_managed = ContextVar('managed_comment_attempt', default=False)


def managed_comments():
    return _managed.get()


@contextmanager
def managed_attempt():
    token = _managed.set(True)
    try:
        yield
    finally:
        _managed.reset(token)
