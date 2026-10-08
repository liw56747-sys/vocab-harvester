"""抓取协程的进度与增量结果钩子；ContextVar 隔离并发任务与线程。"""
from contextlib import contextmanager
from contextvars import ContextVar

_heartbeat = ContextVar('crawl_heartbeat', default=None)
_rows = ContextVar('crawl_rows', default=None)
_checkpoint = ContextVar('crawl_checkpoint', default=None)


@contextmanager
def progress_scope(heartbeat, checkpoint):
    beat_token = _heartbeat.set(heartbeat)
    save_token = _checkpoint.set(checkpoint)
    try:
        yield
    finally:
        _checkpoint.reset(save_token)
        _heartbeat.reset(beat_token)


@contextmanager
def observe_rows(callback):
    token = _rows.set(callback)
    try:
        yield
    finally:
        _rows.reset(token)


def row_observer():
    return _rows.get()


def report_progress():
    callback = _heartbeat.get()
    if callback:
        callback()


def report_rows(rows):
    callback = _rows.get()
    if callback:
        callback(rows)
    report_progress()


def checkpoint(posts):
    callback = _checkpoint.get()
    if callback:
        callback(posts)
