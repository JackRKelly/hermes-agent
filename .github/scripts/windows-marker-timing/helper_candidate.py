"""UNPROVEN fixture-only candidate. Do not install until native gates pass."""


def format_filetime(ticks: int) -> str:
    """FILETIME 100ns ticks since 1601 -> Unix milliseconds, without float."""
    milliseconds = (ticks - 116444736000000000) // 10000
    seconds, fraction = divmod(abs(milliseconds), 1000)
    sign = '-' if milliseconds < 0 else ''
    return f'{sign}{seconds}.{fraction:03d}'


def _creation_time(pid: int) -> str:
    """Query the real process with limited rights; never cache or fabricate identity."""
    import ctypes
    from ctypes import wintypes
    import sys

    if sys.platform != 'win32':
        raise OSError('Windows kernel required')
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        raise ValueError('pid must be a nonzero DWORD')
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.restype = wintypes.BOOL
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION only
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        exit_code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Exit FILETIME also disambiguates a process that exited with STILL_ACTIVE (259).
        if exit_code.value != 259 or times[1].dwHighDateTime or times[1].dwLowDateTime:
            raise ProcessLookupError(f'process {pid} has exited')
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        if not ticks:
            raise ProcessLookupError(f'process {pid} has no creation identity')
        return format_filetime(ticks)
    finally:
        if not k32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
