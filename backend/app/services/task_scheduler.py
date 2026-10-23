"""Bounded, owner-fair background execution; queued tasks do not own a thread."""
import threading
from collections import OrderedDict, deque
from concurrent.futures import Future
from contextvars import copy_context


class JobQueueFullError(RuntimeError):
    pass


class TaskScheduler:
    def __init__(self, *, workers=2, capacity=32, per_owner=4, name='research'):
        self.workers, self.capacity, self.per_owner, self.name = workers, capacity, per_owner, name
        self._condition = threading.Condition()
        self._queues = OrderedDict()
        self._counts = {}
        self._started = False

    def submit(self, owner, work):
        context = copy_context()
        future = Future()
        item = (future, context, work)
        with self._condition:
            if self._counts.get(owner, 0) >= self.per_owner:
                raise JobQueueFullError('你的待处理任务已达上限，请等待完成或取消后重试')
            if sum(self._counts.values()) >= self.capacity:
                raise JobQueueFullError('服务器任务队列已满，请稍后重试')
            self._counts[owner] = self._counts.get(owner, 0) + 1
            self._queues.setdefault(owner, deque()).append(item)
            if not self._started:
                self._started = True
                for index in range(self.workers):
                    threading.Thread(target=self._run, name=f'{self.name}-{index}', daemon=True).start()
            self._condition.notify()

        def cancelled(done):
            if not done.cancelled():
                return
            with self._condition:
                pending = self._queues.get(owner)
                if pending is not None and item in pending:
                    pending.remove(item)
                    if not pending:
                        del self._queues[owner]
                    self._release(owner)
        future.add_done_callback(cancelled)
        return future

    def _release(self, owner):
        self._counts[owner] -= 1
        if not self._counts[owner]:
            del self._counts[owner]

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: bool(self._queues))
                owner, pending = self._queues.popitem(last=False)
                future, context, work = pending.popleft()
                if pending:
                    self._queues[owner] = pending
                if not future.set_running_or_notify_cancel():
                    self._release(owner)
                    continue
            try:
                result = context.run(work)
            except BaseException as exc:  # noqa: BLE001 — executor boundary must resolve futures and keep workers alive
                with self._condition:
                    self._release(owner)
                future.set_exception(exc)
            else:
                with self._condition:
                    self._release(owner)
                future.set_result(result)


RESEARCH_TASKS = TaskScheduler()


def submit_research(work):
    from app.services.user_context import current_user
    owner = current_user()
    return RESEARCH_TASKS.submit((str(owner.data_dir.resolve()), owner.id), work)
