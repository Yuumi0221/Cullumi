from __future__ import annotations

import ctypes
import multiprocessing
import os
import queue
import threading
import time
from multiprocessing.context import BaseContext
from pathlib import Path
from typing import Any

from .media import analyze_photo, failed_photo_analysis
from .niqe import initialize_niqe

DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_TASKS_PER_WORKER = 50
MIN_WORKER_MEMORY_BYTES = 512 * 1024 * 1024
MAX_WORKER_MEMORY_BYTES = 1536 * 1024 * 1024


class AnalysisCancelled(Exception):
    """Raised when a scan is cancelled while a worker task is active."""


def _physical_memory_bytes() -> int:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 4 * 1024 * 1024 * 1024


def default_worker_memory_limit() -> int:
    target = int(_physical_memory_bytes() * 0.20)
    return max(MIN_WORKER_MEMORY_BYTES, min(MAX_WORKER_MEMORY_BYTES, target))


_WORKER_JOB_HANDLE: int | None = None


def _apply_windows_memory_limit(memory_limit: int) -> None:
    global _WORKER_JOB_HANDLE
    if os.name != "nt" or memory_limit <= 0:
        return

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_ulong),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_ulong),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x100 | 0x2000
    information.ProcessMemoryLimit = memory_limit
    configured = kernel32.SetInformationJobObject(
        ctypes.c_void_p(job),
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        ctypes.c_void_p(job), kernel32.GetCurrentProcess()
    )
    if assigned:
        _WORKER_JOB_HANDLE = int(job)
    else:
        kernel32.CloseHandle(ctypes.c_void_p(job))


def _worker_main(
    requests: Any,
    responses: Any,
    memory_limit: int,
) -> None:
    _apply_windows_memory_limit(memory_limit)
    niqe_initialized = False
    while True:
        request = requests.get()
        if request is None:
            return
        task_id, source, thumbnail, niqe_enabled = request
        if niqe_enabled and not niqe_initialized:
            initialize_niqe()
            niqe_initialized = True
        try:
            result = analyze_photo(
                Path(source), Path(thumbnail), niqe_enabled=niqe_enabled
            )
        except BaseException as error:
            result = failed_photo_analysis(
                Path(source), Path(thumbnail), str(error) or error.__class__.__name__
            )
        responses.put((task_id, result))


class PhotoAnalysisRunner:
    """Run scan-time photo decoding in one restartable worker process."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_ANALYSIS_TIMEOUT_SECONDS,
        max_tasks_per_worker: int = DEFAULT_MAX_TASKS_PER_WORKER,
        memory_limit: int | None = None,
        context: BaseContext | None = None,
        worker_main: Any = _worker_main,
    ) -> None:
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.max_tasks_per_worker = max(1, int(max_tasks_per_worker))
        self.memory_limit = (
            default_worker_memory_limit() if memory_limit is None else memory_limit
        )
        self._context = context or multiprocessing.get_context("spawn")
        self._worker_main = worker_main
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._process: multiprocessing.Process | None = None
        self._requests: Any = None
        self._responses: Any = None
        self._task_id = 0
        self._tasks_completed = 0

    def _start(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self._stop()
        self._requests = self._context.Queue(maxsize=1)
        self._responses = self._context.Queue(maxsize=1)
        self._process = self._context.Process(
            target=self._worker_main,
            args=(self._requests, self._responses, self.memory_limit),
            daemon=True,
        )
        self._process.start()
        self._tasks_completed = 0

    def _stop(self) -> None:
        process = self._process
        requests = self._requests
        responses = self._responses
        self._process = None
        self._requests = None
        self._responses = None
        if process is not None:
            if process.is_alive():
                try:
                    if requests is not None:
                        requests.put_nowait(None)
                except (OSError, ValueError, queue.Full):
                    pass
                process.join(0.5)
            if process.is_alive():
                process.terminate()
                process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            process.close()
        for channel in (requests, responses):
            if channel is not None:
                try:
                    channel.close()
                    channel.join_thread()
                except (OSError, ValueError):
                    pass

    def _acquire(self, cancel: threading.Event | None) -> None:
        while not self._lock.acquire(timeout=0.1):
            if self._closed.is_set() or (cancel is not None and cancel.is_set()):
                raise AnalysisCancelled

    def analyze(
        self,
        source: Path,
        thumbnail: Path,
        cancel: threading.Event | None = None,
        niqe_enabled: bool = True,
    ) -> dict[str, Any]:
        self._acquire(cancel)
        try:
            if self._closed.is_set() or (cancel is not None and cancel.is_set()):
                raise AnalysisCancelled
            self._start()
            assert self._process is not None
            assert self._requests is not None
            assert self._responses is not None
            self._task_id += 1
            task_id = self._task_id
            self._requests.put(
                (task_id, str(source), str(thumbnail), niqe_enabled)
            )
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                if self._closed.is_set() or (cancel is not None and cancel.is_set()):
                    self._stop()
                    raise AnalysisCancelled
                if not self._process.is_alive():
                    self._stop()
                    return failed_photo_analysis(
                        source, thumbnail, "图片分析进程异常退出"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop()
                    return failed_photo_analysis(
                        source,
                        thumbnail,
                        f"图片分析超过 {self.timeout_seconds:g} 秒，已安全跳过",
                    )
                try:
                    response_id, result = self._responses.get(
                        timeout=min(0.1, remaining)
                    )
                except queue.Empty:
                    continue
                if response_id != task_id:
                    continue
                self._tasks_completed += 1
                if self._tasks_completed >= self.max_tasks_per_worker:
                    self._stop()
                return result
        finally:
            self._lock.release()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            self._stop()


__all__ = [
    "AnalysisCancelled",
    "PhotoAnalysisRunner",
    "default_worker_memory_limit",
]
