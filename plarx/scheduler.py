from collections import defaultdict
import concurrent.futures as cf
from functools import partial
import os
from random import random
import time
import typing as ty

import psutil
import numpy as np

from .utils import exporter
export, __all__ = exporter()


class Task:
    """Task object, tracking a future submitted to a pool

    :param content: Task to submit to thread/process pool
    or data to yield to user
    :param is_final: If true, mark TaskGenerator as finished on completion
    :param generator: Generator that made the task (need python 3.7
    to annotate properly)
    :param future: Future object. Result will be {dtypename: array, ...}
    Usually set later.
    :param chunk_i: Chunk number about to be produced
    """
    content: ty.Union[np.ndarray, ty.Callable]
    is_final: False
    generator: ty.Any
    dtypes: ty.Tuple[str]
    submit_to: str
    future: cf.Future = None
    chunk_i = 0

    def __init__(self, content, generator, chunk_i, is_final=False,
                 future=None):
        self.chunk_i = chunk_i
        self.generator = generator
        self.dtypes = self.generator.dtypes
        self.submit_to = self.generator.submit_to
        self.content = content
        self.is_final = is_final
        self.future = future

    def __repr__(self):
        return f"{self.generator.dtypes[0]}:{self.chunk_i}"


@export
class TaskGenerator:
    dtypes: ty.Tuple[str]       # Produced data types
    wants_input: ty.List[ty.List[str, int]]
                                # [(dtype, chunk_i), ...] of inputs
                                # needed to make progress
    is_source = False           # True if loader/producer of data without deps
    submit_to = 'thread'        # thread, process, or user
    parallel = False            # Can we start more than one task at a time?
    input_delivery = 'direct'   # 'direct' = inputs are argument to task
                                # 'separate': call new_input with inputs
                                #   in main thread. Cannot parallelize.

    priority = 0                # 0 = saver/target, 1 = source, 2 = other
    depth = 0                   # Dependency hops to final target

    chunk_i = 0                 # Chunk number of last emitted task
    need_result_chunk = -1      # Cannot generate new tasks until this result
                                # chunk number has arrived
    input_cache: ty.Dict[str, np.ndarray]
                                # Inputs we could not yet pass to computation
    has_final_task = False
    has_finished = False

    def __init__(self):
        if self.input_delivery != 'direct' and not self.parallel:
            raise RuntimeError("Cannot parallelize a TaskGenerator with "
                               "indirect input delivery.")

    def __repr__(self):
        _from = _to = ''
        if len(self.wants_input):
            _from = self.wants_input[0][0]
        if len(self.dtypes):
            _to = self.dtypes[0]
        return f"TaskGenerator[{_from}>{_to}]"

    def task(self, inputs) -> Task:
        self.chunk_i += 1
        assert self.chunk_i > self.need_result_chunk
        assert not self.has_finished
        if not self.parallel:
            self.need_result_chunk = self.chunk_i
            self.deliver_inputs(**inputs)

        if self.is_source:
            assert inputs is None, "Passed inputs to source"
            task_f = partial(self.task_function, chunk_i=self.chunk_i)
        else:
            assert all([k in self.wants_input for k in inputs]), "unwanted input"
            assert all([k in inputs for k in self.wants_input]), "missing input"
            if self.input_delivery == 'direct':
                task_f = partial(self.task_function,
                                 chunk_i=self.chunk_i,
                                 **inputs)
                self.wants_input = [[dt, i + 1] for dt, i in self.wants_input]
            else:
                self.receive_inputs(chunk_i=self.chunk_i, **inputs)
                task_f = partial(self.task_function, chunk_i=self.chunk_i)
        return Task(content=task_f,
                    generator=self,
                    chunk_i=self.chunk_i,
                    is_final=False)

    def external_inputs_exhausted(self):
        """For sources, return whether external inputs exhausted"""
        return False

    def external_input_ready(self):
        """For sources, return whether the next external input
         (i.e. self.chunk_id + 1) is ready"""
        return True

    def finish(self):
        pass

    def receive_inputs(self, **inputs):
        """For indirect input delivery: receive inputs and update want_inputs"""
        pass

    def final_task(self) -> Task:
        raise NotImplementedError

    def finish_on_exception(self, exception):
        self.finish()

    def task_function(self, chunk_i: int, is_final=False, **kwargs)\
            -> ty.Dict[str: np.ndarray]:
        """Function executing the task"""
        raise NotImplementedError


class StoredData:
    dtype: str                  # Name of data type
    stored: ty.Dict[int, np.ndarray]
                                # [(chunk_i, data), ...]
    seen_by_consumers: ty.Dict[TaskGenerator, int]
                                # Last chunk seen by each of the generators
                                # that need it
    last_contiguous = -1        # Latest chunk that has arrived
                                # for which all previous chunks have arrived
    source_exhausted = False    # Whether source will produce more data

    def __init__(self, dtype, wanted_by: ty.List[TaskGenerator]):
        self.dtype = dtype
        self.stored = dict()
        self.seen_by_consumers = {tg: -1
                                  for tg in wanted_by}


@export
class Scheduler:
    pending_tasks: ty.List[Task]
    stored_data: ty.Dict[str, StoredData]  # {dtypename: StoredData}
    final_target: str
    task_generators: ty.List[TaskGenerator]
    this_process: psutil.Process
    threshold_mb = 1000

    def __init__(self, task_generators, max_workers=5):
        self.max_workers = max_workers
        self.task_generators = task_generators
        self.task_generators.sort(key=lambda _tg: (_tg.priority, _tg.depth))
        self.pending_tasks = []
        self.this_process = psutil.Process(os.getpid())
        self.processpool = cf.ProcessPoolExecutor(max_workers=self.max_workers)
        self.threadpool = cf.ThreadPoolExecutor(max_workers=self.max_workers)

        who_wants = defaultdict(list)
        for tg in task_generators:
            for dt in tg.dtypes:
                who_wants[dt].append(tg)
        self.stored_data = {
            dt: StoredData(dt, tgs)
            for dt, tgs in who_wants.items()}

    def main_loop(self):
        while True:
            self._receive_from_done_tasks()
            task = self._get_new_task()
            if task is None:
                # No more work, except pending tasks
                # and tasks that may follow from their results.
                if not self.pending_tasks:
                    if self.all_exhausted():
                        break  # All done. We win!
                    self.exit_with_exception(RuntimeError(
                        "No available or pending tasks, "
                        "but data is not exhausted!"))
                # Wait for a pending task to complete
            else:
                if task.submit_to == 'user':
                    yield task.content  # Give back a piece of the final target
                else:
                    self._submit_task(task)
                if len(self.pending_tasks) < self.max_workers:
                    continue            # Find another task
            self.wait_until_task_done()
        self.threadpool.shutdown()
        self.processpool.shutdown()

    def wait_until_task_done(self):
        while True:
            done, not_done = cf.wait(
                [t.future for t in self.pending_tasks],
                return_when=cf.FIRST_COMPLETED,
                timeout=5)
            if len(done):
                break
            self._emit_status("Waiting for a task to complete")

    def _emit_status(self, msg):
        print(msg)
        print(f"\tPending tasks: {self.pending_tasks}")

    def _receive_from_done_tasks(self):
        still_pending = []
        for task in self.pending_tasks:
            f = task.future
            if not f.done():
                still_pending.append(task)
                continue
            if task.is_final:
                # Note we do this BEFORE the exception checking
                # so we do not retry a failed finishing task.
                task.generator.is_finished = True
            if f.exception() is not None:
                self.exit_with_exception(
                    f.exception(),
                    f"Exception while computing {task.dtypes}:{task.chunk_i}")
            if not task.dtypes:
                continue
            for dtype, result in f.result().items():
                d = self.stored_data[dtype]
                d.stored[task.chunk_i] = f.result()
                if d.last_contiguous == task.chunk_i - 1:
                    d.last_contiguous += 1
        self.pending_tasks = still_pending

    def _submit_task(self, task):
        raise NotImplementedError

    def _get_new_task(self):
        external_waits = []    # TaskGenerators waiting for external conditions
        sources = []           # Sources we could load more data from
        requests_for = defaultdict(int)  # Requests for particular inputs
        exhausted_inputs = sum([list(tg.dtypes) for tg in self.task_generators],
                               [])

        for tg in self.task_generators:
            if not tg.external_input_ready():
                external_waits.append(tg)
                continue
            if not tg.parallel and (
                    self.stored_data[tg.dtypes[0]].last_contiguous
                    < tg.need_result_chunk):
                continue        # Need previous task to finish first
            if tg.is_source:
                # Handle these separately (at the end) regardless of priority
                sources.append(tg)
                continue

            # Are the required inputs available?
            if ((not tg.is_source or tg.external_inputs_exhausted())
                    and all([dt in exhausted_inputs for dt in tg.dtypes])):
                # Inputs are exhausted
                if not tg.has_finished:
                    if tg.has_final_task:
                        return tg.final_task()   # Submit final task (no inputs)
                    else:
                        tg.has_finished = True
                continue
            for dtype, chunk_id in tg.wants_input:
                if chunk_id not in self.stored_data[dtype]:
                    requests_for[dtype] += 1
                    continue    # Need input to arrive first

            # Yes! Submit the task
            task_inputs = dict()
            for dtype, chunk_i in tg.wants_input:
                self.stored_data[dtype].seen_by_consumers[tg] = chunk_i
                task_inputs[dtype] = self.stored_data[dtype[chunk_i]]
            self._cleanup_cache()

            return tg.task(task_inputs)

        if sources:
            # No computation tasks to do, but we could load new data
            if (self.this_process.memory_info().rss / 1e6 > self.threshold_mb
                    and self.pending_tasks):
                # ... Let's not though; instead wait for current tasks.
                # (We could perhaps also wait for an external condition
                # but in all likelihood a task will complete soon enough)
                return None
            # Load data for the source that is blocking the most tasks
            # Jitter it a bit for better performance on ties..
            # TODO: There is a better way, but I'm too lazy now
            requests_for_source = [(s, sum([requests_for.get(dt, 0)
                                           for dt in s.dtypes]) + random())
                                   for s in sources]
            s, _ = max(requests_for_source, key=lambda q: q[1])
            return s.task(None)

        if external_waits:
            # We could wait for an external condition...
            if len(self.pending_tasks):
                # ... but probably an existing task will complete first
                return None
            # ... so let's do that.
            self._emit_status(f"{external_waits} waiting on external condition")
            time.sleep(5)
            # TODO: for very long waits this will trip the recursion limit!
            return self._get_new_task()

        # No work to do. Maybe a pending task will still generate some though.
        return None

    def _cleanup_cache(self):
        """Remove any data from our stored_data that has been seen
        by all the consumers"""
        for d in self.stored_data.values():
            if not len(d.seen_by_consumers):
                raise RuntimeError(f"{d} is not consumed by anyone??")
            seen_by_all = min(d.seen_by_consumers.values())
            d.stored = {chunk_i: data
                        for chunk_i, data in d.stored.items()
                        if chunk_i > seen_by_all}

    def exit_with_exception(self, exception, extra_message=''):
        print(extra_message)
        for tg in self.task_generators:
            if not tg.finished:
                try:
                    tg.finish_on_exception(exception)
                except Exception as e:
                    print(f"Exceptional shutdown of {tg} failed")
                    print(f"Got eanother xception: {e}")
                    pass   # These are exceptional times...
            raise exception
        for t in self.pending_tasks:
            t.future.cancel()
        self.threadpool.shutdown()
        self.processpool.shutdown()

    def all_exhausted(self):
        raise NotImplementedError
