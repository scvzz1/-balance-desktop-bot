"""使用当前 Windows 用户的 DPAPI 保存 JSON 配置。"""

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
import threading


_LOCK = threading.RLock()
_SECRET_FIELDS = frozenset({"key", "password", "refresh_token", "cookie", "api_key"})
_MARKER = "__secure_config__"
_SCHEME = "dpapi-current-user-v1"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


if os.name == "nt":
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _dpapi(data, decrypt=False):
    if os.name != "nt":
        raise NotImplementedError("敏感配置加解密需要 Windows DPAPI")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _DATA_BLOB(len(data), buffer)
    target = _DATA_BLOB()
    operation = (_crypt32.CryptUnprotectData if decrypt
                 else _crypt32.CryptProtectData)
    # 仅设置禁止交互标志，不设置 CRYPTPROTECT_LOCAL_MACHINE。
    if not operation(ctypes.byref(source), None, None, None, None,
                     0x1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        if _kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p)):
            raise ctypes.WinError(ctypes.get_last_error())


def _decode(value):
    if isinstance(value, dict):
        if _MARKER in value:
            if (set(value) != {_MARKER, "ciphertext"}
                    or value[_MARKER] != _SCHEME
                    or not isinstance(value["ciphertext"], str)):
                raise ValueError("加密配置标记无效")
            payload = base64.b64decode(value["ciphertext"], validate=True)
            return json.loads(_dpapi(payload, decrypt=True).decode("utf-8"))
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _encode(value):
    if isinstance(value, dict):
        if _MARKER in value:
            raise ValueError("配置包含保留的加密标记")
        result = {}
        for key, item in value.items():
            if key in _SECRET_FIELDS:
                payload = json.dumps(item, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")
                result[key] = {
                    _MARKER: _SCHEME,
                    "ciphertext": base64.b64encode(_dpapi(payload)).decode("ascii"),
                }
            else:
                result[key] = _encode(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _serialize(conf):
    if not isinstance(conf, dict):
        raise TypeError("配置必须是 JSON 对象")
    return (json.dumps(_encode(conf), ensure_ascii=False, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".",
                                     suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(path):
    """读取配置；兼容旧明文，解密失败及文件错误直接抛出。"""
    with _LOCK:
        with open(path, "r", encoding="utf-8") as stream:
            conf = _decode(json.load(stream))
        if not isinstance(conf, dict):
            raise TypeError("配置必须是 JSON 对象")
        return conf


def save(path, conf):
    """原子保存；已有配置先加密保存到同目录的 path.bak。"""
    with _LOCK:
        path = Path(path)
        data = _serialize(conf)
        if path.exists():
            backup_data = _serialize(load(path))
            _atomic_write(path.with_name(path.name + ".bak"), backup_data)
        _atomic_write(path, data)
