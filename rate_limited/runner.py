import asyncio
import contextvars
import functools
import traceback
from asyncio import Condition, create_task, events, gather
from asyncio import sleep as asyncio_sleep
from asyncio import to_thread
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging import getLogger
from typing import Callable, Collection, Optional

from rate_limited.calls import Call
from rate_limited.queue import CompletionTrackingQueue
from rate_limited.resources import Resource


class Runner:
    def __init__(
        self,
        function: Callable,
        resources: Collection[Resource],
        max_concurrent: int,
        max_retries: int = 0,
        min_wait_time: float = 1,
        progress_interval: Optional[float] = 5,
    ):
        self.function = function
        self.resource_manager = ResourceManager(resources)
        self.max_concurrent = max_concurrent
        self.executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self.max_retries = max_retries
        self.min_wait_time = min_wait_time
        self.progress_interval = progress_interval
        # TODO: add verification functions?
        # (checking if response meets criteria, retrying otherwise)

        # two views - one in order of scheduling, the other: tasks to execute, incl. retries
        self.scheduled_calls: list[Call] = []
        self.execution_queue = CompletionTrackingQueue()

        self.logger = getLogger(f"rate_limited.Runner.{function.__name__}")

    def schedule(self, *args, **kwargs):
        # TODO: use docstring from self.function?
        call = Call(args, kwargs, 0)
        self.scheduled_calls.append(call)
        self.execution_queue.put_nowait(call)

    async def worker(self):
        while True:
            # wait to get a task
            call = await self.execution_queue.get()
            # wait for resources to be available
            await self.resource_manager.wait_for_resources(call)

            # starting to execute - but first, register the usage
            self.resource_manager.register_call(call)
            self.resource_manager.pre_allocate(call)
            try:
                # TODO: add a timeout mechanism?
                call.result = await to_thread(self.function, *call.args, **call.kwargs)
                # TODO: are there cases where we need to register result-based usage on error?
                # (one case: if we have user-defined verification functions)
                self.resource_manager.register_result(call.result)
            except Exception as e:
                will_retry = call.num_retries < self.max_retries
                self.logger.warning(
                    f"Exception occurred, will retry: {will_retry}\n{traceback.format_exc()}"
                )
                call.exceptions.append(e)
                if will_retry:
                    call.num_retries += 1
                    self.execution_queue.put_nowait(call)
            finally:
                self.resource_manager.remove_pre_allocation(call)
                self.execution_queue.task_done()

    async def to_thread_in_pool(self, func, /, *args, **kwargs):
        """Copy of asyncio.to_thread, but using a custom thread pool
        (and not requiring Python 3.9)

        Asynchronously run function *func* in a separate thread.

        Any *args and **kwargs supplied for this function are directly passed
        to *func*. Also, the current :class:`contextvars.Context` is propagated,
        allowing context variables from the main thread to be accessed in the
        separate thread.

        Return a coroutine that can be awaited to get the eventual result of *func*.
        """
        loop = events.get_running_loop()
        ctx = contextvars.copy_context()
        func_call = functools.partial(ctx.run, func, *args, **kwargs)
        return await loop.run_in_executor(self.executor, func_call)

    async def run_coro(self) -> tuple[list, list]:
        """
        Actual implementation of run() - to be used by run() and run_sync()
        """
        worker_tasks = [create_task(self.worker()) for _ in range(self.max_concurrent)]

        last_progress_update = datetime.now().timestamp()
        while not self.execution_queue.all_tasks_done():
            now = datetime.now().timestamp()
            next_expiration = self.resource_manager.get_next_usage_expiration().timestamp()
            wait_time = (
                max(self.min_wait_time, next_expiration - now)
                if not self.execution_queue.empty()
                else self.min_wait_time
            )
            if self.progress_interval and now - last_progress_update > self.progress_interval:
                self.logger.info(
                    f"Queue size: {self.execution_queue.qsize()}, waiting for {wait_time} seconds"
                )
                last_progress_update = now
            await asyncio_sleep(wait_time)
            async with self.resource_manager.condition:
                self.resource_manager.wake_workers()
        self.logger.info("Queue is empty, waiting for workers to finish")
        await self.execution_queue.join()
        self.logger.debug("Workers finished, cancelling remaining tasks")
        for task in worker_tasks:
            task.cancel()
        await gather(*worker_tasks, return_exceptions=True)
        self.logger.info("Workers finished")
        results = [call.result for call in self.scheduled_calls]
        exception_lists = [call.exceptions for call in self.scheduled_calls]
        self.scheduled_calls = []
        # TODO: consider returning a generator, instead of waiting for all calls to finish?
        return results, exception_lists

    def run_sync(self):
        """
        Execute run_coro() from sync code
        """
        return asyncio.run(self.run_coro())

    def run(self):
        """
        Runs the scheduled calls, returning a tuple of:
        - results (list, in order of scheduling) and
        - exceptions(list of lists, in order of scheduling)

        Can be called from both sync and async code
        (so that the same code can be used in a script and a notebook - Jupyter runs an event loop)
        """
        try:
            # detect if running in an event loop
            asyncio.get_running_loop()
            running_loop = True
        except RuntimeError:
            running_loop = False
        if running_loop:
            return self.run_coro()
        else:
            return self.run_sync()


class ResourceManager:
    def __init__(self, resources: Collection[Resource]):
        self.resources = list(resources)
        self.condition = Condition()
        self.logger = getLogger("rate_limited.ResourceManager")

    def register_call(self, call: Call):
        for resource in self.resources:
            if resource.arguments_usage_extractor:
                resource.add_usage(resource.arguments_usage_extractor(call))

    def pre_allocate(self, call: Call):
        for resource in self.resources:
            if resource.max_results_usage_estimator:
                resource.pre_allocate(resource.max_results_usage_estimator(call))

    def register_result(self, result):
        for resource in self.resources:
            if resource.results_usage_extractor:
                resource.add_usage(resource.results_usage_extractor(result))

    def remove_pre_allocation(self, call: Call):
        """
        Right now assuming that pre-allocation is only based on the call,
        this could change to e.g. be also based on history of results
        (would need passing the amounts around)
        """
        for resource in self.resources:
            if resource.max_results_usage_estimator:
                resource.remove_pre_allocated(resource.max_results_usage_estimator(call))

    def get_next_usage_expiration(self) -> datetime:
        return min(resource.get_next_expiration() for resource in self.resources)

    def _has_space_for_call(self, call: Call) -> bool:
        # important - we should NOT have any async code here!
        # (because we are inside a condition check)
        for resource in self.resources:
            # assuming we either have arguments_usage_extractor or max_results_usage_estimator
            # if use of a resource can be determined from the arguments, we should fully handle
            # it here
            # TODO: ensure on init that combination of extractors is valid
            assert not (resource.arguments_usage_extractor and resource.max_results_usage_estimator)
            if resource.arguments_usage_extractor is not None:
                needed = resource.arguments_usage_extractor(call)
            elif resource.max_results_usage_estimator is not None:
                needed = resource.max_results_usage_estimator(call)
            else:
                needed = 0
            if not resource.is_available(needed):
                self.logger.debug(f"resource {resource.name} is not available: {resource}")
                return False
        self.logger.debug("all resources are available")
        return True

    async def wait_for_resources(self, call: Call):
        async with self.condition:
            await self.condition.wait_for(lambda: self._has_space_for_call(call))

    def wake_workers(self):
        # TODO: this is too eager, we could only wake a subset of workers
        # (exact solution non-trivial?)
        self.condition.notify_all()
