"""Dedicated, bounded thread pools for work that must not run on the event loop (PERF-2, PERF-3, PERF-6).

``asyncio.to_thread`` uses the loop's ONE default pool (about 6-20 threads shared by everything), so a Pack's
image work could starve the pre-check that gives a new customer their buttons. Here each kind of work has its
own pool with its own limit:

* ``run_cpu``: image decode / hashing / PDF rendering (CPU-bound; threads are enough because PIL, hashlib and
  zlib release the GIL for the heavy parts).
* ``run_io``: blocking database sequences and slow third-party calls that have no async client.

A burst of one kind queues behind its own pool and cannot take threads from the other.
"""

import asyncio
import functools
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

from app.config import settings

T = TypeVar("T")

_cpu_pool: Optional[ThreadPoolExecutor] = None
_io_pool: Optional[ThreadPoolExecutor] = None
_net_pool: Optional[ThreadPoolExecutor] = None
_disk_pool: Optional[ThreadPoolExecutor] = None


def _cpu() -> ThreadPoolExecutor:
    global _cpu_pool
    if _cpu_pool is None:
        # Leave one core free for the event loop: image work is CPU-bound, and when every core is busy the loop
        # thread is not scheduled for ~100 ms (measured: all cores busy stalled the loop 100-200 ms, one core
        # left free about 20 ms).
        workers = int(settings.CPU_WORKER_THREADS or 0) or max(1, min(4, (os.cpu_count() or 2) - 1))
        _cpu_pool = ThreadPoolExecutor(max_workers=max(workers, 1), thread_name_prefix="moraa-cpu")
        # While CPU threads run, Python hands the interpreter to the event loop only every 5 ms by default; a
        # shorter interval lets the loop thread get back in sooner, which keeps requests responsive.
        sys.setswitchinterval(0.001)
    return _cpu_pool


def _io() -> ThreadPoolExecutor:
    global _io_pool
    if _io_pool is None:
        workers = int(settings.IO_WORKER_THREADS or 0) or 8
        _io_pool = ThreadPoolExecutor(max_workers=max(workers, 1), thread_name_prefix="moraa-io")
    return _io_pool


async def run_cpu(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a CPU-heavy function on the CPU pool and wait for it without blocking the event loop."""
    return await asyncio.get_running_loop().run_in_executor(_cpu(), functools.partial(fn, *args, **kwargs))


async def run_io(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking I/O function (database work, a sync SDK call) on the I/O pool."""
    return await asyncio.get_running_loop().run_in_executor(_io(), functools.partial(fn, *args, **kwargs))


def _net() -> ThreadPoolExecutor:
    global _net_pool
    if _net_pool is None:
        workers = int(settings.NET_WORKER_THREADS or 0) or 8
        _net_pool = ThreadPoolExecutor(max_workers=max(workers, 1), thread_name_prefix="moraa-net")
    return _net_pool


async def run_net(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a slow blocking third-party call (a sync AI SDK call that can take 30 s) on its own pool, so it can
    never hold up the short database jobs on the I/O pool."""
    return await asyncio.get_running_loop().run_in_executor(_net(), functools.partial(fn, *args, **kwargs))


def _disk() -> ThreadPoolExecutor:
    global _disk_pool
    if _disk_pool is None:
        _disk_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="moraa-disk")
    return _disk_pool


async def run_disk(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Write a file on a pool of its own. Jobs here hold a whole image in memory while they wait, so they must never
    queue behind database work: every writer gets a thread at once and the image is released as soon as it is on disk."""
    return await asyncio.get_running_loop().run_in_executor(_disk(), functools.partial(fn, *args, **kwargs))


def shutdown_executors() -> None:
    """Stop all pools (application shutdown). Running jobs finish; nothing new is accepted."""
    global _cpu_pool, _io_pool, _net_pool, _disk_pool
    for pool in (_cpu_pool, _io_pool, _net_pool, _disk_pool):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    _cpu_pool = _io_pool = _net_pool = _disk_pool = None
