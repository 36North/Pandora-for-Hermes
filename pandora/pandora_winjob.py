"""Windows job object: take the mpv child down when Hermes exits.

mpv runs as a separate process. If Hermes is closed — or killed — while music
is playing, nothing tells mpv to stop and it keeps going. A job object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` makes the kernel terminate every process
in the job when the last handle to the job closes, which happens when this
process dies for any reason, including a hard kill.

Non-Windows platforms get a no-op object so callers need no branching.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


class KillOnCloseJob:
    """A job object that kills its members when this process goes away."""

    def __init__(self) -> None:
        self._handle: Optional[int] = None
        if sys.platform != "win32":
            return
        try:
            k32 = _kernel32()
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                logger.debug("CreateJobObject failed: %s", ctypes.get_last_error())
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                    wintypes.HANDLE(handle), JobObjectExtendedLimitInformation,
                    ctypes.byref(info), ctypes.sizeof(info)):
                logger.debug("SetInformationJobObject failed: %s", ctypes.get_last_error())
                k32.CloseHandle(wintypes.HANDLE(handle))
                return
            self._handle = handle
        except Exception:  # pragma: no cover - depends on the Windows runtime
            logger.debug("job object setup failed", exc_info=True)
            self._handle = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def assign(self, pid: int) -> bool:
        """Put *pid* in the job. Returns False (and logs) when that is impossible."""
        if self._handle is None:
            return False
        try:
            k32 = _kernel32()
            proc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
            if not proc:
                logger.debug("OpenProcess(%s) failed: %s", pid, ctypes.get_last_error())
                return False
            try:
                ok = k32.AssignProcessToJobObject(
                    wintypes.HANDLE(self._handle), wintypes.HANDLE(proc))
                if not ok:
                    # Nested-job rules or a process already owned elsewhere: the
                    # player still works, it just loses kill-on-close.
                    logger.debug("AssignProcessToJobObject(%s) failed: %s",
                                 pid, ctypes.get_last_error())
                return bool(ok)
            finally:
                k32.CloseHandle(wintypes.HANDLE(proc))
        except Exception:  # pragma: no cover - depends on the Windows runtime
            logger.debug("job assignment failed", exc_info=True)
            return False

    def close(self) -> None:
        """Close the job handle; any process still in the job is terminated."""
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        try:
            _kernel32().CloseHandle(wintypes.HANDLE(handle))
        except Exception:  # pragma: no cover
            logger.debug("CloseHandle(job) failed", exc_info=True)
