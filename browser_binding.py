"""通过系统默认浏览器的独立配置绑定指定站点。"""
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlsplit


def default_browser(scheme='https'):
    """通过 Windows URL 关联获取程序，不解析 shell 命令。"""
    from ctypes import wintypes
    query = ctypes.windll.shlwapi.AssocQueryStringW
    query.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
                      wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    query.restype = ctypes.c_long
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    result = query(0x1000 | 0x20, 2, scheme, 'open', buffer, ctypes.byref(size))
    if result != 0 or not buffer.value:
        raise RuntimeError('无法识别系统默认浏览器，请在 Windows 设置中检查默认应用。')
    browser = Path(buffer.value)
    if not browser.is_file():
        raise RuntimeError('系统默认浏览器程序不存在，请在 Windows 设置中重新选择默认浏览器。')
    if browser.name.lower() not in ('chrome.exe', 'msedge.exe', 'brave.exe', 'vivaldi.exe'):
        raise RuntimeError('当前默认浏览器（%s）暂不支持自动绑定；请将 Chrome 或 Edge 设为默认后再试。' % browser.name)
    return browser


def node_runtime():
    executable = shutil.which('node')
    if executable:
        version = subprocess.run([executable, '--version'], capture_output=True, text=True)
        match = re.match(r'v(\d+)', version.stdout.strip())
        if version.returncode == 0 and match and int(match.group(1)) >= 22:
            return Path(executable)
    raise RuntimeError('浏览器绑定需要 Node.js 22 或更高版本，请安装并加入 PATH。')


def _origin(base):
    try:
        url = urlsplit(base)
        if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
            raise ValueError()
        host = url.hostname.encode('idna').decode('ascii').lower()
        if ':' in host:
            host = '[' + host + ']'
        port = url.port
        if port is not None and port != (443 if url.scheme == 'https' else 80):
            host += ':' + str(port)
        return url.scheme + '://' + host
    except (ValueError, UnicodeError, TypeError):
        raise RuntimeError('请填写有效站点地址。') from None


@contextmanager
def _profile_lock(profile):
    lock = (profile / '.kk-binding.lock').open('a+b')
    try:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('此站点的登录窗口已打开，请先完成或关闭该窗口。') from None
        yield
    finally:
        lock.close()


def _remove_profile(profile):
    # 浏览器退出后，Windows 可能仍短暂持有数据库文件锁。
    for attempt in range(20):
        try:
            shutil.rmtree(profile)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 19:
                raise RuntimeError('登录窗口已关闭，但临时配置仍被占用，请关闭残留登录窗口后重试。') from None
            time.sleep(0.25)


def _run_binding(base, profile, browser, node):
    process = subprocess.Popen(
        [str(node), str(Path(__file__).with_name('browser_bind.js')), base, str(profile), str(browser)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        output, _ = process.communicate(timeout=205)
    except subprocess.TimeoutExpired:
        # 只终止本次创建的 Node 与独立浏览器进程树，不影响用户浏览器。
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise RuntimeError('浏览器绑定超时，请重新打开登录窗口。') from None
    if process.returncode:
        raise RuntimeError('浏览器绑定未完成：请在3分钟内登录支持的站点；关闭登录窗可取消。')
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        raise RuntimeError('未收到有效登录信息，请重试。') from None
    return data


def bind(base, remember=False):
    """返回同源令牌，或经验证的 new-api Cookie；remember 仅保留专用配置。"""
    origin = _origin(base)
    browser = default_browser(urlsplit(base).scheme)
    node = node_runtime()
    if remember:
        appdata = Path(os.environ.get('APPDATA') or (Path.home() / 'AppData/Roaming'))
        profile = appdata / 'BalanceDesktopBot/browser_profiles' / hashlib.sha256(origin.encode('utf-8')).hexdigest()
        profile.mkdir(parents=True, exist_ok=True)
    else:
        profile = Path(tempfile.mkdtemp(prefix='kk-login-'))
    try:
        with _profile_lock(profile):
            data = _run_binding(base, profile, browser, node)
        if not isinstance(data, dict) or data.get('origin') != origin:
            raise RuntimeError('登录站点不匹配，未保存凭证。')
        token = data.get('access_token')
        cookie = data.get('cookie')
        valid_token = isinstance(token, str) and bool(token.strip())
        valid_cookie = (isinstance(cookie, str) and bool(cookie.strip())
                        and data.get('flavor') == 'newapi' and str(data.get('user_id', '')).isdigit()
                        and int(data['user_id']) > 0)
        if not (valid_token or valid_cookie):
            raise RuntimeError('未检测到可用登录信息，未保存凭证。')
        return data
    finally:
        if not remember:
            _remove_profile(profile)
