"""主循环与抓取线程之间的执行状态，保留快照并支持线程安全取消。"""
import copy
import threading
import time
import uuid


class ScheduledRun:
    _clock = staticmethod(time.monotonic)

    def __init__(self):
        self.run_id = uuid.uuid4().hex[:12]
        self._lock = threading.Lock()
        self.last_progress = self._clock()
        self.cancelled = threading.Event()
        self._loop = None
        self._task = None
        self.future = None
        self.current_platform = None
        self.posts = {}
        self.errors = {}
        self.completed = set()

    def __call__(self):
        with self._lock:
            self.last_progress = self._clock()

    def idle_seconds(self):
        with self._lock:
            return self._clock() - self.last_progress

    def bind(self, loop, task):
        with self._lock:
            self._loop, self._task = loop, task
        if self.cancelled.is_set():
            task.cancel()

    def unbind(self):
        with self._lock:
            self._loop = self._task = None

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            loop, task = self._loop, self._task
        if loop is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # 线程已退出；调用方仍以 future.done() 确认完成。

    def start_platform(self, platform):
        with self._lock:
            self.current_platform = platform
            self.last_progress = self._clock()

    @staticmethod
    def _key(post):
        if not hasattr(post, "raw_data"):
            return repr(post)
        raw = post.raw_data or {}
        identity = post.post_id or raw.get("comment_id") or (post.author, post.content)
        return (raw.get("type"), identity, raw.get("parent_id"), tuple(post.tags))

    def _merge(self, platform, posts):
        merged = {self._key(p): p for p in self.posts.get(platform, [])}
        merged.update((self._key(p), copy.deepcopy(p)) for p in posts)
        self.posts[platform] = list(merged.values())

    def save(self, platform, posts):
        with self._lock:
            if not self.cancelled.is_set():
                self._merge(platform, posts)

    def finish_platform(self, platform, posts=None, error=None):
        with self._lock:
            self.posts.setdefault(platform, [])
            if posts is not None:
                self._merge(platform, posts)
            if error:
                self.errors[platform] = error
            self.completed.add(platform)
            self.current_platform = None

    def result(self, platforms=(), stopped=False):
        with self._lock:
            errors = dict(self.errors)
            if stopped:
                for platform in platforms:
                    if platform in self.completed:
                        continue
                    errors[platform] = ('抓取无有效进展，已请求停止并保留已采集数据'
                                        if platform == self.current_platform
                                        else '尚未执行：本轮任务已请求停止')
            return {'posts': copy.deepcopy(self.posts), 'errors': errors}
