"""按当前用户与应用路径获取进程生命周期的单实例锁。"""

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import tempfile
import threading


_LOCK = threading.Lock()
_HANDLES = {}


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _user_id():
    if os.name != "nt":
        return str(os.getuid())
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), 0x8,
                                     ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        if not _advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size)):
            error = ctypes.get_last_error()
            if error != 122:
                raise ctypes.WinError(error)
        buffer = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(token, 1, buffer, size,
                                             ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            if _kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p)):
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if not _kernel32.CloseHandle(token):
            raise ctypes.WinError(ctypes.get_last_error())


def acquire(path=None):
    """成功返回 True，已有实例返回 False；成功句柄保留到进程退出。

    path 默认为本模块所在目录；调用方也可传配置路径或应用路径。
    其他系统错误直接抛出。
    """
    canonical = os.path.normcase(os.path.realpath(
        os.fspath(path) if path is not None else str(Path(__file__).parent)))
    identity = _user_id() + "\0" + canonical
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    with _LOCK:
        if digest in _HANDLES:
            return False
        if os.name == "nt":
            ctypes.set_last_error(0)
            handle = _kernel32.CreateMutexW(None, True, "Local\\kkvibe-" + digest)
            error = ctypes.get_last_error()
            if not handle:
                raise ctypes.WinError(error)
            if error == 183:
                if not _kernel32.CloseHandle(handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                return False
            _HANDLES[digest] = handle
        else:
            import errno
            import fcntl
            lock_path = os.path.join(tempfile.gettempdir(), "kkvibe-" + digest + ".lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return False
                raise
            _HANDLES[digest] = fd
        return True
