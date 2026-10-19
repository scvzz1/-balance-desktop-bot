# -*- coding: utf-8 -*-
"""
AI API 中转站 余额桌面挂件（多账号版）
------------------------------------
一个无边框、置顶、可拖动的小机器人，实时显示 API 中转站的账户余额。
支持多个账号：可以同时挂 sub2api、其它 sub2api 站点、以及各家 new-api / one-api 站点。

用法：
    双击「start.bat」      —— 无控制台窗口静默启动（推荐）
    或 pythonw bot.py
    或 python bot.py --demo      —— 不联网，用假数据预览外观
    或 python bot.py --check     —— 命令行打印所有账号的接口原始返回（排查用）

操作：
    左键拖动        移动位置（自动记住）
    左键单击        切换到下一个账号
    双击            立即刷新
    右键            菜单（切换账号 / 全部余额 / 设置 / 置顶 / 开机自启 / 退出）
    Ctrl+滚轮       调整透明度

依赖：仅 Python 标准库（tkinter + urllib）。已在 Python 3.10 测试。
"""

import json
from concurrent.futures import ThreadPoolExecutor
import math
import os
import queue
import random
import sys
import threading
import time
import urllib.error
import urllib.request

import anime_skin
import pastel_skin
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk, messagebox
import secure_config
import instance_lock

APP_NAME = "BalanceDesktopBot"
DEFAULT_BASE = "https://example.com"

CONF_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
CONF_PATH = os.path.join(CONF_DIR, "config.json")
STARTUP_DIR = os.path.join(
    os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"
)
STARTUP_VBS = os.path.join(STARTUP_DIR, APP_NAME + ".vbs")

TRANSPARENT = "#FE01FE"          # 透明键色（正常 UI 不会用到这个颜色）
C_BG = "#FFFFFF"
C_BORDER = "#E7E9F2"
C_TEXT = "#1E2130"
C_SUB = "#8B90A6"
C_ACCENT = "#6C5CE7"
C_ACCENT_LT = "#A78BFA"
C_OK = "#22C55E"
C_WARN = "#F59E0B"
C_ERR = "#EF4444"

NEW_ACCOUNT = {"name": "新账号", "base": DEFAULT_BASE, "key": "",
               "style": "auto", "auth": "key",
               "email": "", "password": "", "refresh_token": ""}

DEFAULTS = {
    "interval": 60,
    "topmost": True,
    "alpha": 1.0,
    "x": None,
    "y": None,
    "active": 0,
    "accounts": [],
    "settings_size": None,
    "overview_size": None,
    "mascot": "cat",
}

W, H = 268, 116


# --------------------------------------------------------------------------
# 配置读写（兼容老版本的单 api_key 配置）
# --------------------------------------------------------------------------
def load_conf():
    conf = dict(DEFAULTS)
    conf["accounts"] = []
    try:
        data = secure_config.load(CONF_PATH)
    except FileNotFoundError:
        data = {}
    if isinstance(data, dict):
        for k in ("interval", "topmost", "alpha", "x", "y", "active",
                  "settings_size", "overview_size", "mascot"):
            if k in data:
                conf[k] = data[k]
        accs = data.get("accounts")
        if isinstance(accs, list):
            for a in accs:
                if not isinstance(a, dict):
                    continue
                item = dict(NEW_ACCOUNT)
                item["name"] = str(a.get("name") or "账号")
                item["base"] = str(a.get("base") or DEFAULT_BASE)
                item["key"] = str(a.get("key") or "")
                item["style"] = str(a.get("style") or "auto")
                item["auth"] = str(a.get("auth") or "key")
                item["email"] = str(a.get("email") or "")
                item["password"] = str(a.get("password") or "")
                item["refresh_token"] = str(a.get("refresh_token") or "")
                for field in ("flavor", "detected_style", "detected_base", "cookie", "user_id"):
                    item[field] = str(a.get(field) or "")
                conf["accounts"].append(item)
        elif data.get("api_key"):
            # 老配置升级
            conf["accounts"].append({
                "name": "Balance Bot", "base": DEFAULT_BASE,
                "key": str(data["api_key"]), "style": "sub2api",
            })
    return conf


def save_conf(conf):
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        secure_config.save(CONF_PATH, conf)
    except Exception as error:
        if threading.current_thread() is threading.main_thread():
            messagebox.showerror("保存失败", "配置未保存，原文件保持不变。请检查磁盘空间或权限。\n" + type(error).__name__)
        raise RuntimeError("配置保存失败，请检查磁盘空间或权限") from error


# --------------------------------------------------------------------------
# 接口：自动识别站点类型
#   sub2api  : GET {base}/v1/usage                      -> remaining / balance / quota
#   new-api  : GET {base}/v1/dashboard/billing/subscription + /usage
#   credit   : GET {base}/v1/dashboard/billing/credit_grants
# --------------------------------------------------------------------------
class StyleMiss(Exception):
    """这个站点不支持当前接口，换下一种试"""


def norm_base(base):
    b = (base or "").strip().rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    if not b.startswith("http"):
        b = "https://" + b
    return b


def connection_error(error):
    text = str(error)
    low = text.lower()
    if "429" in low:
        return "请求过于频繁（HTTP 429），请稍后再试，避免连续登录。"
    if any(word in low for word in ("captcha", "turnstile", "验证码")):
        return "站点要求验证码，请先在浏览器完成验证；当前无法自动通过。"
    if any(word in low for word in ("2fa", "totp", "two-factor")):
        return "账号开启了两步验证，当前自动登录不支持，请使用已有面板 Token。"
    if "timed out" in low or "timeout" in low:
        return "连接超时：请检查站点是否可访问，以及挂件与浏览器的代理是否一致。"
    if "certificate" in low or "ssl" in low:
        return "HTTPS 证书或 TLS 连接异常，请检查系统时间、站点证书和代理。"
    if any(word in low for word in ("getaddrinfo", "name resolution", "11001")):
        return "域名解析失败，请检查站点地址、DNS 或代理。"
    if "403" in low:
        return "访问被拒绝（HTTP 403）：可能是权限或站点防护拦截，请在浏览器核对。"
    return text


def _http_json(url, key, timeout=15, label="Key", extra_headers=None):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + key.strip(),
            "Accept": "application/json",
            "User-Agent": "BalanceDesktopBot/2.1",
        },
    )
    for header, value in (extra_headers or {}).items():
        req.add_header(header, str(value))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise StyleMiss("404")
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        msg = ""
        try:
            j = json.loads(body)
            msg = (j.get("error") or {}).get("message") or j.get("message") or ""
        except Exception:
            msg = body[:120]
        if e.code in (401, 403):
            hint = "（登录态会过期，去面板重新复制）" if label != "Key" else ""
            raise RuntimeError("%s 无效或无权限（HTTP %s）%s%s"
                               % (label, e.code, "：" + msg if msg else "", hint))
        raise RuntimeError("HTTP %s %s" % (e.code, msg))
    except Exception as e:
        raise RuntimeError("网络错误：%s" % e)

    try:
        return json.loads(raw), raw
    except Exception:
        raise StyleMiss("not-json")


def _fetch_sub2api(base, key, timeout):
    d, raw = _http_json(base + "/v1/usage", key, timeout)
    if not isinstance(d, dict):
        raise StyleMiss("bad-body")
    if not any(k in d for k in ("remaining", "balance", "quota", "subscription", "mode")):
        raise StyleMiss("unexpected")
    out = parse_sub2api(d)
    out["style"] = "sub2api"
    return out, raw


def _get_public(url, timeout=8):
    """不带鉴权的 GET（读 /api/status 这类公开配置）"""
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "BalanceDesktopBot/2.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


PROVIDER_CACHE = {}


def provider_info(base, timeout=8):
    """new-api 系的公开配置：货币类型 / 汇率 / QuotaPerUnit"""
    now = time.time()
    hit = PROVIDER_CACHE.get(base)
    if hit and now - hit[1] < 900:
        return hit[0]
    info = {}
    d = _get_public(base + "/api/status", timeout)
    if isinstance(d, dict):
        data = d.get("data") if isinstance(d.get("data"), dict) else d
        for k in ("quota_display_type", "usd_exchange_rate", "quota_per_unit",
                  "custom_currency_symbol", "system_name", "display_in_currency"):
            if k in data:
                info[k] = data[k]
    PROVIDER_CACHE[base] = (info, now)
    return info


STYLE_CACHE = {}


def _fetch_openai(base, key, timeout):
    with ThreadPoolExecutor(max_workers=3) as pool:
        subscription = pool.submit(_http_json, base + "/v1/dashboard/billing/subscription", key, timeout)
        usage_job = pool.submit(_http_json,
            base + "/v1/dashboard/billing/usage?start_date=2020-01-01&end_date=2099-12-31", key, timeout)
        info_job = pool.submit(provider_info, base, min(timeout, 4))
        sub, raw1 = subscription.result()
        if not isinstance(sub, dict) or "hard_limit_usd" not in sub:
            raise StyleMiss("no-subscription")
        usage, raw2 = usage_job.result()
        info = info_job.result()
    cents = 0.0
    if isinstance(usage, dict):
        try:
            cents = float(usage.get("total_usage") or 0)
        except Exception:
            cents = 0.0
    limit = float(sub.get("hard_limit_usd") or 0)
    used = cents / 100.0

    # 站点可能按人民币/代币计，甚至给「不限量」Key 返回 1e8 这个哨兵值
    dtype = str(info.get("quota_display_type") or "USD").upper()
    unit = {"CNY": "CNY", "TOKENS": "TOK"}.get(dtype, "USD")
    unlimited = limit >= 1e7          # 哨兵值，别把它当成真余额显示
    remaining = -1.0 if unlimited else round(limit - used, 6)

    return {
        "style": "openai",
        "mode": "unrestricted",
        "is_valid": True,
        "unit": unit,
        "plan": "可用额度（不限量）" if unlimited else "可用额度",
        "remaining": remaining,
        "limit": None if unlimited else limit,
        "used": used,
        "balance": remaining,
        "sub_note": ("已用 " + fmt_money(used, unit)) if used > 0 else None,
        "today_cost": None,
        "total_cost": round(used, 6),
        "today_tokens": None,
        "expires_at": None,
        "days_left": None,
        "unlimited": unlimited,
    }, raw1


def _fetch_credit(base, key, timeout):
    d, raw = _http_json(base + "/v1/dashboard/billing/credit_grants", key, timeout)
    if not isinstance(d, dict) or "total_available" not in d:
        raise StyleMiss("no-credit")
    avail = float(d.get("total_available") or 0)
    used = float(d.get("total_used") or 0)
    return {
        "style": "credit",
        "mode": "unrestricted",
        "is_valid": True,
        "unit": "USD",
        "plan": "OpenAI 兼容站点",
        "remaining": round(avail, 6),
        "limit": round(avail + used, 6),
        "used": round(used, 6),
        "balance": round(avail, 6),
        "today_cost": None,
        "total_cost": round(used, 6),
        "today_tokens": None,
        "expires_at": None,
        "days_left": None,
        "unlimited": False,
    }, raw


def _post_json(url, payload, timeout=15, label="请求"):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "BalanceDesktopBot/2.2",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise StyleMiss("404")
        txt = ""
        try:
            txt = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        msg = ""
        try:
            j = json.loads(txt)
            msg = (j.get("error") or {}).get("message") or j.get("message") or ""
        except Exception:
            msg = txt[:120]
        raise RuntimeError("%s失败（HTTP %s）%s" % (label, e.code, "：" + msg if msg else ""))
    except Exception as e:
        raise RuntimeError("网络错误：%s" % e)
    try:
        return json.loads(raw), raw
    except Exception:
        raise RuntimeError("返回不是合法 JSON：%s" % raw[:120])


# access_token 只放内存，不落盘
TOKEN_CACHE = {}


def _auth_fields(r):
    """令牌可能在顶层，也可能被 {code,data} 包了一层，两种都认"""
    if not isinstance(r, dict):
        return {}
    for src in (r, r.get("data") if isinstance(r.get("data"), dict) else {},):
        if src.get("access_token"):
            return src
    return r if isinstance(r.get("data"), dict) else {}


def _login_err_hint(msg):
    """把服务端的报错翻译成能照着做的提示"""
    low = (msg or "").lower()
    if "401" in msg or "invalid email or password" in low or "invalid_credentials" in low:
        return (msg + "\n可能是：① 密码不对；② 账号是第三方登录（LinuxDo/GitHub/微信）注册的，"
                       "本来就没有密码 —— 先去站点「忘记密码」设一个，或改用 API Key。")
    if "400" in msg or "email" in low:
        return msg + "\n这类站点的账号模式要填注册用的邮箱（带 @），不是用户名。"
    return msg


def ensure_access_token(acc, timeout=15):
    """账号密码模式：优先用 refresh_token 续期，失败再用邮箱密码登录一次"""
    base = norm_base(acc.get("base"))
    email = (acc.get("email") or "").strip()
    ck = (base, email)
    now = time.time()
    cached = TOKEN_CACHE.get(ck) or {}
    if cached.get("access_token") and cached.get("expires_at", 0) > now + 60:
        return cached["access_token"]

    rt = cached.get("refresh_token") or (acc.get("refresh_token") or "").strip()
    if rt:
        try:
            r, _ = _post_json(base + "/api/v1/auth/refresh",
                              {"refresh_token": rt}, timeout, label="续期")
            f = _auth_fields(r)
            tok = f.get("access_token")
            if tok:
                TOKEN_CACHE[ck] = {
                    "access_token": tok,
                    "refresh_token": f.get("refresh_token") or rt,
                    "expires_at": now + int(f.get("expires_in") or 3600),
                }
                if f.get("refresh_token"):
                    acc["refresh_token"] = f["refresh_token"]
                return tok
        except StyleMiss:
            raise
        except Exception:
            pass  # 续期失败就走登录

    pwd = acc.get("password") or ""
    if not email or not pwd:
        raise RuntimeError("登录态已失效，且没有保存密码：请在设置里重填密码，或改用 API Key")
    try:
        r, _ = _post_json(base + "/api/v1/auth/login",
                          {"email": email, "password": pwd}, timeout, label="登录")
    except StyleMiss:
        raise
    except RuntimeError as e:
        raise RuntimeError(_login_err_hint(str(e)))
    if isinstance(r, dict) and r.get("requires_2fa"):
        raise RuntimeError("该账号开了两步验证（2FA），无法自动登录；请改用 API Key 模式")
    f = _auth_fields(r)
    tok = f.get("access_token")
    if not tok:
        msg = r.get("message") if isinstance(r, dict) else ""
        raise RuntimeError(msg or "登录返回里没有 access_token（站点可能改了登录协议）")
    TOKEN_CACHE[ck] = {
        "access_token": tok,
        "refresh_token": f.get("refresh_token"),
        "expires_at": now + int(f.get("expires_in") or 3600),
    }
    if f.get("refresh_token"):
        acc["refresh_token"] = f["refresh_token"]
    return tok


def _newapi_login(acc, base, timeout):
    """new-api 系：POST /api/user/login（username + password）→ access_token"""
    user = (acc.get("email") or "").strip()
    pwd = acc.get("password") or ""
    if not user or not pwd:
        raise RuntimeError("账号模式要填用户名和密码")
    r, raw = _post_json(base + "/api/user/login",
                        {"username": user, "password": pwd}, timeout, label="登录")
    if not isinstance(r, dict):
        raise RuntimeError("登录返回异常")
    if r.get("success") is False:
        raise RuntimeError(r.get("message") or "登录失败（用户名或密码不对）")
    data = r.get("data") if isinstance(r.get("data"), dict) else {}
    tok = data.get("access_token") or r.get("access_token")
    if not tok:
        raise RuntimeError("登录没返回 access_token（站点可能开了验证码或密码加密登录）")
    return tok


def _newapi_self(base, tok, timeout, user_id=None):
    headers = {"New-Api-User": str(user_id)} if user_id else None
    d, raw = _http_json(base + "/api/user/self", tok, timeout, label="登录态", extra_headers=headers)
    if isinstance(d, dict) and d.get("success") is False:
        raise RuntimeError(d.get("message") or "读取账户信息失败")
    data = d.get("data") if isinstance(d, dict) and isinstance(d.get("data"), dict) else d
    return data or {}, raw


def _newapi_token(acc, base, timeout):
    ck = (base, (acc.get("email") or "").strip(), "newapi")
    now = time.time()
    c = TOKEN_CACHE.get(ck) or {}
    if c.get("access_token") and c.get("expires_at", 0) > now + 60:
        return c["access_token"]
    tok = _newapi_login(acc, base, timeout)
    TOKEN_CACHE[ck] = {"access_token": tok, "expires_at": now + 1800}
    return tok


def parse_newapi_self(data, base):
    """new-api 面板数据：data.quota / data.used_quota（单位是 quota，不是钱）"""
    info = provider_info(base)
    dtype = str(info.get("quota_display_type") or "USD").upper()
    try:
        qpu = float(info.get("quota_per_unit") or 500000) or 500000.0
    except Exception:
        qpu = 500000.0
    try:
        rate = float(info.get("usd_exchange_rate") or 1) or 1.0
    except Exception:
        rate = 1.0
    unit = {"CNY": "CNY", "TOKENS": "TOK"}.get(dtype, "USD")

    def conv(v):
        try:
            v = float(v)
        except Exception:
            return None
        if dtype == "TOKENS":
            return round(v, 2)
        v = v / qpu
        if dtype == "CNY":
            v *= rate
        return round(v, 6)

    quota = conv(data.get("quota"))
    used = conv(data.get("used_quota"))
    out = {
        "mode": "unrestricted",
        "status": data.get("status") or "",
        "is_valid": True,
        "unit": unit,
        "plan": "钱包余额",
        "remaining": quota,
        "balance": quota,
        "limit": None,
        "used": used,
        "today_cost": None,
        "total_cost": used,
        "today_tokens": None,
        "expires_at": None,
        "days_left": None,
        "unlimited": False,
        "sub_note": ("已用 " + fmt_money(used, unit)) if used else None,
        "user": data.get("username") or data.get("email") or "",
    }
    if quota is not None and quota <= 0:
        out["plan"] = "余额已用尽"
    return out


def _login_newapi_flow(acc, base, timeout):
    tok = _newapi_token(acc, base, timeout)
    try:
        data, raw = _newapi_self(base, tok, timeout)
    except RuntimeError:
        # token 失效（会话过期）→ 重新登录一次
        TOKEN_CACHE.pop((base, (acc.get("email") or "").strip(), "newapi"), None)
        tok = _newapi_login(acc, base, timeout)
        TOKEN_CACHE[(base, (acc.get("email") or "").strip(), "newapi")] = {
            "access_token": tok, "expires_at": time.time() + 1800}
        data, raw = _newapi_self(base, tok, timeout)
    out = parse_newapi_self(data, base)
    out["style"] = "newapi"
    return out, raw, "newapi"


def fetch_login(acc, timeout=15):
    """账号密码模式：sub2api（/api/v1/auth/login）或 new-api（/api/user/login）自动识别"""
    base = norm_base(acc.get("base"))
    if (acc.get("flavor") or "") == "newapi":
        return _login_newapi_flow(acc, base, timeout)
    try:
        tok = ensure_access_token(acc, timeout)
        d, raw = _http_json(base + "/api/v1/user/profile", tok, timeout, label="登录态")
        if isinstance(d, dict) and d.get("code") not in (None, 0, 200):
            raise RuntimeError(d.get("message") or "面板接口返回错误")
    except StyleMiss:
        acc["flavor"] = "newapi"
        return _login_newapi_flow(acc, base, timeout)
    acc["flavor"] = "sub2api"
    out = parse_profile(d)
    out["style"] = "login"
    return out, raw, "login"


def fetch_account(acc, timeout=15):
    """返回 (parsed, raw, style)。style='auto' 时按 sub2api → new-api → credit 顺序探测。"""
    base = norm_base(acc.get("base"))
    if acc.get("detected_base") != base:
        for field in ("flavor", "detected_style"):
            acc.pop(field, None)
        acc["detected_base"] = base
    auth = (acc.get("auth") or "key").lower()
    if auth == "login":
        return fetch_login(acc, timeout)
    key = (acc.get("key") or "").strip()
    if not key and auth != "cookie":
        raise RuntimeError("还没填登录 Token" if auth == "token" else "还没填 API Key")
    if auth == "cookie":
        headers = {"Cookie": acc.get("cookie") or ""}
        if acc.get("user_id"):
            headers["New-Api-User"] = str(acc["user_id"])
        d, raw = _http_json(base + "/api/user/self", "", timeout, label="登录态", extra_headers=headers)
        data = d.get("data") if isinstance(d, dict) else None
        if not isinstance(data, dict) or "quota" not in data:
            raise RuntimeError("浏览器登录会话已失效，请重新绑定")
        return parse_newapi_self(data, base), raw, "cookie"
    if auth == "token":
        try:
            return fetch_profile(acc, timeout)
        except RuntimeError as error:
            if not acc.get("refresh_token") or "401" not in str(error):
                raise
            r, _ = _post_json(base + "/api/v1/auth/refresh", {"refresh_token": acc["refresh_token"]}, timeout, label="续期")
            fields = _auth_fields(r)
            if not fields.get("access_token"):
                raise RuntimeError("登录续期失败，请重新浏览器绑定")
            acc["key"] = fields["access_token"]
            acc["refresh_token"] = fields.get("refresh_token") or acc["refresh_token"]
            return fetch_profile(acc, timeout)
    base = norm_base(acc.get("base"))
    style = (acc.get("style") or "auto").lower()

    chain = {
        "sub2api": [_fetch_sub2api],
        "openai": [_fetch_openai, _fetch_credit],
        "credit": [_fetch_credit, _fetch_openai],
        "auto": [_fetch_sub2api, _fetch_openai, _fetch_credit],
    }.get(style, [_fetch_sub2api, _fetch_openai, _fetch_credit])

    remembered = {"sub2api": _fetch_sub2api, "openai": _fetch_openai, "credit": _fetch_credit}
    preferred = (remembered.get(acc.get("detected_style")) or STYLE_CACHE.get(base)) if style == "auto" else None
    if preferred in chain:
        chain = [preferred] + [fn for fn in chain if fn is not preferred]
    last = None
    for fn in chain:
        try:
            out, raw = fn(base, key, timeout)
            STYLE_CACHE[base] = fn
            acc["detected_style"] = out.get("style", style)
            return out, raw, out.get("style", style)
        except StyleMiss as e:
            last = e
            continue
    raise RuntimeError("这个地址不认（接口都不通：%s）" % (last or "无匹配"))


def fetch_profile(acc, timeout=15):
    """粘贴 Token 模式：sub2api 读 /api/v1/user/profile，new-api 读 /api/user/self"""
    token = (acc.get("key") or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    base = norm_base(acc.get("base"))
    if acc.get("flavor") == "newapi":
        data, raw = _newapi_self(base, token, timeout, acc.get("user_id"))
        out = parse_newapi_self(data, base)
        out["style"] = "profile"
        return out, raw, "profile"
    try:
        d, raw = _http_json(base + "/api/v1/user/profile", token, timeout, label="登录 Token")
        if isinstance(d, dict) and d.get("code") not in (None, 0, 200):
            raise RuntimeError(d.get("message") or "面板接口返回错误")
        out = parse_profile(d)
        out["style"] = "profile"
        return out, raw, "profile"
    except StyleMiss:
        # new-api：/api/user/self，也是 Authorization: Bearer <access_token>
        acc["flavor"] = "newapi"
        data, raw = _newapi_self(base, token, timeout, acc.get("user_id"))
        out = parse_newapi_self(data, base)
        out["style"] = "profile"
        return out, raw, "profile"


def parse_profile(d):
    """面板 profile 返回：{code,message,data:{balance,total_recharged,username,...}}"""
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    bal = data.get("balance")
    out = {
        "mode": "unrestricted",
        "status": data.get("status") or "",
        "is_valid": True,
        "unit": "USD",
        "plan": "钱包余额",
        "remaining": bal,
        "balance": bal,
        "limit": None,
        "used": None,
        "today_cost": None,
        "total_cost": None,
        "today_tokens": None,
        "expires_at": None,
        "days_left": None,
        "unlimited": False,
        "sub_note": None,
        "user": data.get("username") or data.get("email") or "",
    }
    frozen = data.get("frozen_balance")
    if isinstance(frozen, (int, float)) and frozen > 0:
        out["sub_note"] = "冻结 " + fmt_money(frozen, "USD")
    recharged = data.get("total_recharged")
    if out["sub_note"] is None and isinstance(recharged, (int, float)) and recharged > 0:
        out["sub_note"] = "累计充值 " + fmt_money(recharged, "USD")
    return out


def parse_sub2api(d):
    """把 sub2api 的 /v1/usage 拍平成界面好用的结构"""
    out = {
        "mode": d.get("mode") or "",
        "status": d.get("status") or "",
        "is_valid": d.get("isValid", True),
        "unit": d.get("unit") or "USD",
        "plan": d.get("planName") or "",
        "remaining": None,
        "limit": None,
        "used": None,
        "today_cost": None,
        "total_cost": None,
        "today_tokens": None,
        "expires_at": None,
        "days_left": None,
        "unlimited": False,
    }
    quota = d.get("quota") if isinstance(d.get("quota"), dict) else None
    rem = d.get("remaining")
    if rem is None and quota:
        rem = quota.get("remaining")
    if rem is None:
        rem = d.get("balance")
    out["remaining"] = rem
    if isinstance(rem, (int, float)) and rem < 0:
        out["unlimited"] = True
    out["balance"] = d.get("balance")
    if quota:
        out["limit"] = quota.get("limit")
        out["used"] = quota.get("used")
    usage = d.get("usage") or {}
    today = usage.get("today") or {}
    total = usage.get("total") or {}
    out["today_cost"] = today.get("actual_cost", today.get("cost"))
    out["total_cost"] = total.get("actual_cost", total.get("cost"))
    out["today_tokens"] = today.get("total_tokens")
    sub = d.get("subscription") or {}
    out["expires_at"] = d.get("expires_at") or sub.get("expires_at")
    out["days_left"] = d.get("days_until_expiry")
    out["rate_limits"] = d.get("rate_limits")
    rl = d.get("rate_limits") if isinstance(d.get("rate_limits"), list) else None

    # 只设了「周期限额 / 速率限制」、没有设总额度的 Key：
    # 接口不会给 remaining，只能从 rate_limits 里取最紧的那个窗口
    if out["remaining"] is None and rl:
        best = None
        for r in rl:
            if not isinstance(r, dict):
                continue
            v = r.get("remaining")
            if isinstance(v, (int, float)) and (best is None or v < best[0]):
                best = (v, r.get("window") or "")
        if best:
            out["remaining"] = best[0]
            out["window"] = best[1]

    if not out["plan"]:
        st = (out.get("status") or "").lower()
        st_map = {
            "active": "API Key",
            "quota_exhausted": "额度已用尽",
            "expired": "已过期",
            "disabled": "已禁用",
        }
        if st in st_map and st != "active":
            out["plan"] = st_map[st]
        elif out["mode"] == "quota_limited":
            if out["limit"]:
                out["plan"] = "额度剩余"
            elif out.get("window"):
                out["plan"] = "%s 窗口余量" % out["window"]
            else:
                out["plan"] = "限额余量"
        elif out["mode"] == "unrestricted":
            out["plan"] = "钱包余额"
    return out


# --------------------------------------------------------------------------
# 格式化
# --------------------------------------------------------------------------
def money_sym(unit):
    u = (unit or "").upper()
    if u in ("CNY", "RMB"):
        return "¥"
    if u == "USD":
        return "$"
    return ""


def fmt_money(v, unit):
    if v is None:
        return "—"
    if not isinstance(v, (int, float)):
        return str(v)
    if (unit or "").upper() in ("TOK", "TOKENS"):
        return "{:,.0f}".format(v)
    a = abs(v)
    sym = money_sym(unit)
    if a >= 100:
        s = "%.2f" % v
    elif a >= 1:
        s = "%.3f" % v
    else:
        s = "%.4f" % v
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return sym + s


def fmt_cost(v, unit):
    if v is None:
        return None
    if not isinstance(v, (int, float)):
        return str(v)
    if v == 0:
        return money_sym(unit) + "0"
    if abs(v) < 0.01:
        return money_sym(unit) + ("%.4f" % v).rstrip("0")
    return money_sym(unit) + ("%.4f" % v).rstrip("0").rstrip(".")


# --------------------------------------------------------------------------
# 画圆角矩形
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 造型：全部用 Canvas 图元画，不依赖任何图片资源
# --------------------------------------------------------------------------
MASCOT_ORDER = tuple(pastel_skin.NAMES) + ("anime", "blob", "cat", "robot", "ghost", "pixel")
MASCOT_NAMES = {
    **pastel_skin.NAMES,
    "anime": "星璃 · 全身互动",
    "blob": "圆仔",
    "cat": "橘猫",
    "robot": "机械仔",
    "ghost": "小幽灵",
    "pixel": "像素狗",
}


def _skin_blob(c, cx, cy, blink, mood):
    base, dark, light = "#6C5CE7", "#5847D6", "#8D7CF3"
    c.create_oval(cx - 21, cy + 24, cx + 21, cy + 33, fill="#EDEAFB", outline="", tags="mascot")
    c.create_line(cx, cy - 26, cx, cy - 38, fill=dark, width=3, capstyle="round", tags="mascot")
    c.create_oval(cx - 5, cy - 49, cx + 5, cy - 39, fill="#A78BFA", outline="", tags="mascot")
    c.create_oval(cx - 30, cy - 30, cx + 30, cy + 30, fill=dark, outline="", tags="mascot")
    c.create_oval(cx - 29, cy - 31, cx + 27, cy + 26, fill=base, outline="", tags="mascot")
    c.create_oval(cx - 21, cy - 23, cx - 5, cy - 9, fill=light, outline="", tags="mascot")
    c.create_oval(cx - 15, cy - 17, cx - 9, cy - 11, fill="#B3A6FF", outline="", tags="mascot")
    c.create_oval(cx - 24, cy + 5, cx - 14, cy + 12, fill="#FF9DB6", outline="", tags="mascot")
    c.create_oval(cx + 14, cy + 5, cx + 24, cy + 12, fill="#FF9DB6", outline="", tags="mascot")
    for ex in (cx - 12, cx + 12):
        if blink:
            c.create_line(ex - 6, cy - 2, ex + 6, cy - 2, fill="#FFFFFF", width=3,
                          capstyle="round", tags="mascot")
        else:
            c.create_oval(ex - 6, cy - 10, ex + 6, cy + 3, fill="#FFFFFF", outline="", tags="mascot")
            py = cy + 2 if mood == "error" else cy - 1
            c.create_oval(ex - 3, py - 4, ex + 3, py + 2, fill="#2E2A48", outline="", tags="mascot")
    if mood == "error":
        c.create_line(cx - 5, cy + 17, cx + 5, cy + 17, fill="#FFFFFF", width=2,
                      capstyle="round", tags="mascot")
    elif mood == "loading":
        c.create_line(cx - 5, cy + 16, cx + 5, cy + 16, fill="#FFFFFF", width=2,
                      capstyle="round", tags="mascot")
    else:
        c.create_arc(cx - 7, cy + 8, cx + 7, cy + 21, start=200, extent=140,
                     style="arc", outline="#FFFFFF", width=2, tags="mascot")


def _skin_cat(c, cx, cy, blink, mood):
    base, dark, light, inner = "#F7B267", "#E09A48", "#FFD49C", "#FFD9B0"
    c.create_oval(cx - 20, cy + 25, cx + 20, cy + 33, fill="#F1EEE6", outline="", tags="mascot")
    # 耳朵
    c.create_polygon(cx - 25, cy - 10, cx - 23, cy - 33, cx - 3, cy - 20,
                     fill=dark, outline="", tags="mascot")
    c.create_polygon(cx + 25, cy - 10, cx + 23, cy - 33, cx + 3, cy - 20,
                     fill=dark, outline="", tags="mascot")
    c.create_polygon(cx - 21, cy - 14, cx - 20, cy - 27, cx - 9, cy - 19,
                     fill=inner, outline="", tags="mascot")
    c.create_polygon(cx + 21, cy - 14, cx + 20, cy - 27, cx + 9, cy - 19,
                     fill=inner, outline="", tags="mascot")
    # 头
    c.create_oval(cx - 27, cy - 24, cx + 27, cy + 26, fill=dark, outline="", tags="mascot")
    c.create_oval(cx - 26, cy - 25, cx + 24, cy + 23, fill=base, outline="", tags="mascot")
    c.create_oval(cx - 18, cy - 18, cx - 5, cy - 7, fill=light, outline="", tags="mascot")
    for dx in (-7, 0, 7):
        c.create_line(cx + dx, cy - 21, cx + dx, cy - 15, fill=dark, width=2, tags="mascot")
    c.create_oval(cx - 24, cy + 3, cx - 15, cy + 10, fill="#FFAFB9", outline="", tags="mascot")
    c.create_oval(cx + 15, cy + 3, cx + 24, cy + 10, fill="#FFAFB9", outline="", tags="mascot")
    for ex in (cx - 10, cx + 10):
        if blink:
            c.create_line(ex - 5, cy - 4, ex + 5, cy - 4, fill="#4A3423", width=3,
                          capstyle="round", tags="mascot")
        else:
            c.create_oval(ex - 4, cy - 11, ex + 4, cy - 1, fill="#3B2A1C", outline="", tags="mascot")
            c.create_oval(ex - 2, cy - 9, ex + 1, cy - 6, fill="#FFFFFF", outline="", tags="mascot")
    c.create_polygon(cx - 3, cy + 1, cx + 3, cy + 1, cx, cy + 4,
                     fill="#FF8FA3", outline="", tags="mascot")
    if mood == "error":
        c.create_arc(cx - 6, cy + 3, cx + 6, cy + 12, start=20, extent=140,
                     style="arc", outline="#8A5A2B", width=2, tags="mascot")
    elif mood == "loading":
        c.create_line(cx - 4, cy + 8, cx + 4, cy + 8, fill="#8A5A2B", width=2,
                      capstyle="round", tags="mascot")
    else:
        c.create_arc(cx - 5, cy + 4, cx + 5, cy + 13, start=200, extent=140,
                     style="arc", outline="#8A5A2B", width=2, tags="mascot")


def _skin_robot(c, cx, cy, blink, mood):
    base, dark, screen = "#5B8DEF", "#3F6FD1", "#EAF3FF"
    c.create_oval(cx - 21, cy + 24, cx + 21, cy + 33, fill="#E6ECF7", outline="", tags="mascot")
    c.create_line(cx, cy - 27, cx, cy - 37, fill=dark, width=3, capstyle="round", tags="mascot")
    c.create_oval(cx - 4, cy - 45, cx + 4, cy - 37, fill="#FFD166", outline="", tags="mascot")
    # 侧耳
    c.create_rectangle(cx - 31, cy - 8, cx - 26, cy + 3, fill=dark, outline="", tags="mascot")
    c.create_rectangle(cx + 26, cy - 8, cx + 31, cy + 3, fill=dark, outline="", tags="mascot")
    # 头
    round_rect(c, cx - 26, cy - 26, cx + 26, cy + 26, 11, fill=dark, outline="", tags="mascot")
    round_rect(c, cx - 25, cy - 27, cx + 25, cy + 24, 11, fill=base, outline="", tags="mascot")
    round_rect(c, cx - 18, cy - 18, cx + 18, cy + 11, 7, fill=screen, outline="", tags="mascot")
    for ex in (cx - 8, cx + 8):
        if blink:
            c.create_line(ex - 4, cy - 4, ex + 4, cy - 4, fill="#2A3F5F", width=3,
                          capstyle="round", tags="mascot")
        else:
            c.create_rectangle(ex - 4, cy - 10, ex + 4, cy + 1, fill="#2A3F5F",
                               outline="", tags="mascot")
            c.create_rectangle(ex - 3, cy - 9, ex, cy - 5, fill="#7DF9FF",
                               outline="", tags="mascot")
    if mood == "error":
        c.create_line(cx - 5, cy + 5, cx + 5, cy + 5, fill="#2A3F5F", width=2,
                      capstyle="round", tags="mascot")
    elif mood == "loading":
        c.create_line(cx - 5, cy + 5, cx + 5, cy + 5, fill="#2A3F5F", width=2,
                      capstyle="round", tags="mascot")
    else:
        c.create_line(cx - 5, cy + 4, cx, cy + 7, fill="#2A3F5F", width=2,
                      capstyle="round", tags="mascot")
        c.create_line(cx, cy + 7, cx + 5, cy + 4, fill="#2A3F5F", width=2,
                      capstyle="round", tags="mascot")


def _skin_ghost(c, cx, cy, blink, mood):
    body, dark, edge = "#F5F3FF", "#E4DEFF", "#D5CDF8"
    c.create_oval(cx - 18, cy + 26, cx + 18, cy + 33, fill="#EDEAFB", outline="", tags="mascot")
    # 身体：上半圆 + 波浪下摆
    c.create_oval(cx - 25, cy - 26, cx + 25, cy + 22, fill=edge, outline="", tags="mascot")
    c.create_oval(cx - 24, cy - 26, cx + 24, cy + 21, fill=body, outline="", tags="mascot")
    c.create_polygon(cx - 24, cy + 6, cx + 24, cy + 6, cx + 24, cy + 24, cx - 24, cy + 24,
                     fill=body, outline="", tags="mascot")
    for i, gx in enumerate((-16, 0, 16)):
        c.create_oval(cx + gx - 8, cy + 14, cx + gx + 8, cy + 30,
                      fill=body, outline="", tags="mascot")
    c.create_oval(cx - 16, cy - 18, cx - 4, cy - 7, fill="#FBFAFF", outline="", tags="mascot")
    for ex in (cx - 9, cx + 9):
        if blink:
            c.create_line(ex - 5, cy - 2, ex + 5, cy - 2, fill="#5B4E8C", width=3,
                          capstyle="round", tags="mascot")
        else:
            c.create_oval(ex - 4, cy - 9, ex + 4, cy + 1, fill="#5B4E8C", outline="", tags="mascot")
            c.create_oval(ex - 2, cy - 7, ex + 1, cy - 4, fill="#FFFFFF", outline="", tags="mascot")
    c.create_oval(cx - 18, cy + 2, cx - 11, cy + 7, fill="#FFD1E0", outline="", tags="mascot")
    c.create_oval(cx + 11, cy + 2, cx + 18, cy + 7, fill="#FFD1E0", outline="", tags="mascot")
    if mood == "error":
        c.create_oval(cx - 3, cy + 8, cx + 3, cy + 14, fill="#5B4E8C", outline="", tags="mascot")
    elif mood == "loading":
        c.create_line(cx - 4, cy + 11, cx + 4, cy + 11, fill="#5B4E8C", width=2,
                      capstyle="round", tags="mascot")
    else:
        c.create_arc(cx - 5, cy + 5, cx + 5, cy + 15, start=200, extent=140,
                     style="arc", outline="#5B4E8C", width=2, tags="mascot")


def _skin_pixel(c, cx, cy, blink, mood):
    U = 4  # 像素格

    def px(gx, gy, gw, gh, color):
        c.create_rectangle(cx + gx * U, cy + gy * U, cx + (gx + gw) * U, cy + (gy + gh) * U,
                           fill=color, outline="", tags="mascot")

    fur, dark, muzzle = "#D9A06B", "#A9713F", "#F6E7D3"
    px(-7, -9, 3, 3, dark)          # 左耳
    px(4, -9, 3, 3, dark)           # 右耳
    px(-6, -6, 12, 11, fur)         # 脸
    px(-6, -6, 12, 2, dark)         # 额头阴影
    px(-7, -4, 1, 4, fur)           # 左腮
    px(6, -4, 1, 4, fur)            # 右腮
    px(-3, 1, 6, 4, muzzle)         # 嘴套
    px(-1, 2, 2, 1, "#5B3B22")      # 鼻子
    if blink:
        px(-4, -3, 2, 1, "#3B2A1C")
        px(2, -3, 2, 1, "#3B2A1C")
    else:
        px(-4, -4, 2, 2, "#3B2A1C")
        px(2, -4, 2, 2, "#3B2A1C")
        px(-4, -4, 1, 1, "#FFFFFF")
        px(2, -4, 1, 1, "#FFFFFF")
    if mood == "error":
        px(-3, 4, 2, 1, "#5B3B22")
        px(1, 4, 2, 1, "#5B3B22")
    elif mood == "loading":
        px(-1, 4, 2, 1, "#5B3B22")
    else:
        px(-1, 4, 2, 2, "#FF7B8E")   # 吐舌头


SKINS = {
    "blob": _skin_blob,
    "cat": _skin_cat,
    "robot": _skin_robot,
    "ghost": _skin_ghost,
    "pixel": _skin_pixel,
}



def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2,
        x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r,
        x1, y1 + r, x1, y1, x1 + r, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


# --------------------------------------------------------------------------
# 主界面
# --------------------------------------------------------------------------
class Bot:
    def __init__(self, root, conf, demo=False):
        self.root = root
        self.conf = conf
        self.demo = demo
        self.q = queue.Queue()
        self.ui_q = queue.Queue()
        self.loading = False
        self._request_id = 0
        self._request_identity = None
        self.last_raw = ""
        self.last_data = None
        self.last_time = None
        self.last_plan = ""
        self.error_msg = ""
        self.mood = "idle"
        self.blink_until = 0
        self.next_blink = time.time() + random.uniform(2.5, 5.0)
        self.t0 = time.time()
        self.settings_win = None
        self.wheel_alpha = float(conf.get("alpha") or 1.0)
        self._press = None
        self._click_job = None
        self.pet_action = "idle"
        self.pet_gaze = (0.0, 0.0)
        self.pet_expression = "normal"
        self.pet_reactions = 0
        self._pet_motion_job = None
        self.pet_started = 0
        self.pet_until = 0
        self.pet_message = "点我打个招呼吧～"
        self._pet_click = False
        self._last_visibility_check = 0

        if not self.conf["accounts"]:
            self.conf["accounts"] = [dict(NEW_ACCOUNT, name="新账号", style="auto")]
        self.conf["active"] = max(0, min(int(conf.get("active") or 0), len(self.conf["accounts"]) - 1))

        root.overrideredirect(True)
        root.configure(bg=TRANSPARENT)
        root.title("AI 余额挂件")
        try:
            root.wm_attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass
        root.wm_attributes("-topmost", bool(conf.get("topmost", True)))
        try:
            root.wm_attributes("-alpha", self.wheel_alpha)
        except tk.TclError:
            pass

        self.x = conf.get("x")
        self.y = conf.get("y")
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        if self.x is None or self.y is None:
            self.x = sw - W - 28
            self.y = sh - H - 90
        else:
            self.x = max(-W + 80, min(int(self.x), sw - 80))
            self.y = max(0, min(int(self.y), sh - 60))
        self._place()

        self.canvas = tk.Canvas(
            root, width=W, height=H, bg=TRANSPARENT, highlightthickness=0, bd=0
        )
        self.canvas.pack()

        fam = "Microsoft YaHei UI"
        # Canvas coordinates use pixels; use pixel fonts too, so Windows DPI
        # scaling cannot squeeze three text rows into each other.
        self.f_plan = tkfont.Font(family=fam, size=-12)
        self.f_big = tkfont.Font(family=fam, size=-28, weight="bold")
        self.f_sub = tkfont.Font(family=fam, size=-11)

        self._build_static()
        self._build_menu()
        self._layout_mascot()

        root.bind("<Button-1>", self._on_press)
        root.bind("<B1-Motion>", self._on_drag)
        root.bind("<ButtonRelease-1>", self._on_release)
        root.bind("<Double-Button-1>", lambda e: self._on_double())
        root.bind("<Button-3>", self._on_menu)
        root.bind("<MouseWheel>", self._on_wheel)
        root.bind("<Control-MouseWheel>", self._on_wheel)
        root.bind("<Map>", self._on_window_expose, add="+")
        root.bind("<Expose>", self._on_window_expose, add="+")
        root.after(200, self._ensure_visible)

        self.root.after(120, self._pump)

        if self.demo:
            self._apply_demo()
        elif not (self.current().get("key") or self.current().get("cookie") or self.current().get("password") or self.current().get("refresh_token") or "").strip():
            self.set_status("nokey")
            self.root.after(400, lambda: self.open_settings(first_run=True))
        else:
            self.refresh()

        self._schedule()
        self._animate_pet()

    # ---------------- 账号 ----------------
    def accounts(self):
        return self.conf["accounts"]

    def current(self):
        i = max(0, min(int(self.conf.get("active") or 0), len(self.accounts()) - 1))
        self.conf["active"] = i
        return self.accounts()[i]

    def next_account(self):
        n = len(self.accounts())
        if n <= 1:
            return
        self.conf["active"] = (int(self.conf.get("active") or 0) + 1) % n
        save_conf(self.conf)
        self.last_data = None
        self.loading = False
        self._request_id += 1
        self.refresh()

    def select_account(self, i):
        self.conf["active"] = max(0, min(int(i), len(self.accounts()) - 1))
        save_conf(self.conf)
        self.last_data = None
        self.loading = False
        self._request_id += 1
        self.refresh()

    # ---------------- 窗口 ----------------
    def _is_full_body(self):
        return self.conf.get("mascot") == "anime" or self.conf.get("mascot") in pastel_skin.NAMES

    def _place(self):
        anime = self._is_full_body()
        self.root.geometry("%dx%d+%d+%d" % (390 if anime else W, 206 if anime else H, int(self.x), int(self.y)))

    def _ensure_visible(self):
        """Restore the topmost layer without activating or focusing the widget."""
        if not self.conf.get("topmost", True) or not self.root.winfo_ismapped():
            return
        # Let our own menus/settings stay above the companion.
        if self.root.grab_current() is not None:
            return
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel) and child.winfo_exists() and child.winfo_ismapped():
                return
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            user = ctypes.windll.user32
            user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user.GetAncestor.restype = wintypes.HWND
            user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, wintypes.UINT]
            user.SetWindowPos.restype = wintypes.BOOL
            hwnd = user.GetAncestor(self.root.winfo_id(), 2)
            # HWND_TOPMOST; no move/resize/activation/owner-order changes.
            user.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0013 | 0x0200)
        else:
            self.root.wm_attributes("-topmost", True)

    def _on_window_expose(self, event):
        if event.widget == self.root:
            self.root.after_idle(self._draw_mascot)

    def _on_press(self, e):
        self._dx = e.x_root - int(self.x)
        self._dy = e.y_root - int(self.y)
        self._press = (e.x_root, e.y_root)
        self._pet_click = self._is_full_body() and e.x < 130

    def _on_drag(self, e):
        self.x = e.x_root - self._dx
        self.y = e.y_root - self._dy
        self._place()

    def _on_release(self, e):
        if not self._press:
            return
        dx = abs(e.x_root - self._press[0])
        dy = abs(e.y_root - self._press[1])
        self._press = None
        if dx <= 3 and dy <= 3 and self._pet_click:
            self.interact_pet()
            return
        if dx <= 3 and dy <= 3 and len(self.accounts()) > 1:
            # 双击会触发两次 release，用延时任务区分「单击切换」和「双击刷新」
            if self._click_job:
                self.root.after_cancel(self._click_job)
            self._click_job = self.root.after(260, self._cycle_account)

    def _cycle_account(self):
        self._click_job = None
        self.next_account()

    def _on_double(self):
        if self._click_job:
            self.root.after_cancel(self._click_job)
            self._click_job = None
        if self._pet_click:
            self.interact_pet("happy")
        else:
            self.refresh()

    def _on_wheel(self, e):
        step = 0.05 if e.delta > 0 else -0.05
        self.wheel_alpha = max(0.3, min(1.0, self.wheel_alpha + step))
        try:
            self.root.wm_attributes("-alpha", self.wheel_alpha)
        except tk.TclError:
            pass
        self.conf["alpha"] = round(self.wheel_alpha, 2)
        save_conf(self.conf)

    # ---------------- 静态元素 ----------------
    def _build_static(self):
        c = self.canvas
        round_rect(c, 1, 1, W - 1, H - 1, 24, fill=C_BG, outline=C_BORDER, width=1)
        round_rect(c, 14, 3, W - 14, 26, 12, fill="#F6F4FF", outline="")

        self.txt_plan = c.create_text(99, 23, text="读取中…", anchor="w",
                                      font=self.f_plan, fill=C_SUB)
        self.txt_big = c.create_text(98, 54, text="—", anchor="w",
                                     font=self.f_big, fill=C_TEXT)
        self.txt_sub = c.create_text(99, 88, text="", anchor="w",
                                     font=self.f_sub, fill=C_SUB)
        self.dot = c.create_oval(W - 26, 21, W - 18, 29, fill=C_WARN, outline="")

    # ---------------- 小机器人 ----------------
    def _draw_mascot(self):
        c = self.canvas
        c.delete("mascot")
        if self._is_full_body():
            now = time.time()
            action = self.pet_action if now < self.pet_until else "idle"
            expression = self.pet_expression if now < self.pet_until else (
                "worried" if self.mood == "error" else "normal")
            if self.conf.get("mascot") in pastel_skin.NAMES:
                pastel_skin.draw(c, self.conf["mascot"], now-self.t0,
                                 now < self.blink_until, self.mood, action,
                                 now-self.pet_started, self.pet_gaze, expression)
            else:
                anime_skin.draw(c, now-self.t0, now < self.blink_until,
                                self.mood, action, now-self.pet_started,
                                gaze=self.pet_gaze, expression=expression)
            c.delete("petbubble")
            if now < self.pet_until:
                round_rect(c, 132, 8, 384, 46, 13, fill="#F7F1FF", outline="#DED2F2", tags="petbubble")
                c.create_text(258, 27, text=self.pet_message, fill="#6C568B",
                              font=("Microsoft YaHei UI", 9), tags="petbubble")
            return
        bob = math.sin((time.time() - self.t0) * 1.6) * 1.4
        skin = self.conf.get("mascot", "cat")
        SKINS.get(skin, SKINS["cat"])(c, 52, 60 + bob,
                                           time.time() < self.blink_until, self.mood)

    def _animate_pet(self):
        if self._is_full_body():
            px, py = self.root.winfo_pointerxy()
            dx = px - (self.root.winfo_rootx() + 65)
            dy = py - (self.root.winfo_rooty() + 58)
            # Return to a relaxed gaze when the pointer is far away.
            near = abs(dx) < 650 and abs(dy) < 500
            target_x = max(-1.0, min(1.0, dx / 180.0)) if near else 0.0
            target_y = max(-1.0, min(1.0, dy / 150.0)) if near else 0.0
            gx, gy = self.pet_gaze
            self.pet_gaze = (gx + (target_x-gx)*0.22, gy + (target_y-gy)*0.22)
            self._draw_mascot()
        self._pet_motion_job = self.root.after(40, self._animate_pet)

    def interact_pet(self, action=None):
        self.pet_reactions += 1
        self.pet_expression = ("happy" if action == "happy" else
                               ("happy", "shy", "surprised")[self.pet_reactions % 3])
        self.pet_action = action or "wave"
        self.pet_started = time.time()
        self.pet_until = self.pet_started + 3.2
        self.pet_message = random.choice(("在呢～ 今天也要元气满满！", "余额交给我，你安心忙吧～", "收到！陪你一起努力～", "摸摸头，幸运值 +1！"))
        self._draw_mascot()

    def _layout_mascot(self):
        c = self.canvas
        c.delete("petbubble")
        if self._is_full_body():
            c.configure(width=390, height=206)
            if not getattr(self, "_anime_layout", False):
                for item in c.find_all():
                    if "mascot" not in c.gettags(item):
                        c.move(item, 122, 62)
                self._anime_layout = True
        else:
            c.configure(width=W, height=H)
            if getattr(self, "_anime_layout", False):
                for item in c.find_all():
                    if "mascot" not in c.gettags(item):
                        c.move(item, -122, -62)
                self._anime_layout = False
        self._place()

    def select_mascot(self, skin):
        if skin not in MASCOT_ORDER:
            return
        self.conf["mascot"] = skin
        self.var_mascot.set(skin)
        self._layout_mascot()
        if self._is_full_body():
            self.interact_pet("wave")
        self._draw_mascot()
        save_conf(self.conf)

    # ---------------- 状态显示 ----------------
    def _account_label(self, plan=None):
        """第一行：优先显示「这是什么数」（钱包余额/额度剩余），再带账号名和序号"""
        accs = self.accounts()
        name = self.current().get("name") or "账号"
        n = len(accs)
        cands = []
        if n > 1:
            i = int(self.conf.get("active") or 0) + 1
            if plan:
                cands.append("%s · %s %d/%d" % (plan, name, i, n))
                cands.append("%s · %d/%d" % (plan, i, n))
            cands.append("%s %d/%d" % (name, i, n))
        else:
            if plan:
                cands.append("%s · %s" % (plan, name))
            cands.append(plan or name)
        for t in cands:
            if self.f_plan.measure(t) <= 148:
                return t
        return self._ellipsize(cands[0], self.f_plan, 148)

    def set_status(self, kind, msg=""):
        c = self.canvas
        self.error_msg = msg
        c.itemconfigure(self.txt_plan, text=self._account_label(self.last_plan))
        if kind == "loading":
            self.mood = "loading"
            c.itemconfigure(self.txt_big, text="···", fill=C_SUB, font=self.f_big)
            c.itemconfigure(self.txt_sub, text="读取中", fill=C_SUB)
            c.itemconfigure(self.dot, fill=C_WARN)
        elif kind == "error":
            self.mood = "error"
            c.itemconfigure(self.txt_big, text="—", fill=C_TEXT, font=self.f_big)
            short = self._ellipsize(msg or "读取失败", self.f_sub, 140)
            c.itemconfigure(self.txt_sub, text=short, fill=C_ERR)
            c.itemconfigure(self.dot, fill=C_ERR)
        elif kind == "nokey":
            self.mood = "error"
            c.itemconfigure(self.txt_big, text="—", fill=C_TEXT, font=self.f_big)
            hint = {
                "login": "还没填账号密码，右键→设置",
                "token": "还没填登录 Token，右键→设置",
            }.get(self.current().get("auth") or "key", "还没填 API Key，右键→设置")
            c.itemconfigure(self.txt_sub, text=hint, fill=C_WARN)
            c.itemconfigure(self.dot, fill=C_WARN)
        self._draw_mascot()

    def show_data(self, d):
        c = self.canvas
        self.last_data = d
        self.mood = "ok"
        unit = d.get("unit")

        plan = d.get("plan") or ("钱包余额" if d.get("mode") == "unrestricted" else "API Key 额度")

        if d.get("unlimited"):
            big = "∞"
            big_color = C_ACCENT
        else:
            big = fmt_money(d.get("remaining"), unit)
            big_color = C_TEXT
            rem = d.get("remaining")
            if isinstance(rem, (int, float)):
                if rem <= 0:
                    big_color = C_ERR
                    self.mood = "error"
                elif rem < 1:
                    big_color = C_WARN

        self._fit_big(big)
        c.itemconfigure(self.txt_big, text=big, fill=big_color)
        # 第一行同时交代「这个数是什么」和「哪个账号」
        self.last_plan = plan
        c.itemconfigure(self.txt_plan, text=self._account_label(plan), fill=C_SUB)
        c.itemconfigure(self.txt_sub, text=self._compose_sub(d, unit), fill=C_SUB)
        rem = d.get("remaining")
        if isinstance(rem, (int, float)) and rem <= 0 and not d.get("unlimited"):
            dot_color = C_ERR
        elif self.mood == "ok":
            dot_color = C_OK
        else:
            dot_color = C_WARN
        c.itemconfigure(self.dot, fill=dot_color)
        self._draw_mascot()
        self._update_menu_status(plan)

    def _fit_big(self, text):
        size = 28
        f = tkfont.Font(family="Microsoft YaHei UI", size=-size, weight="bold")
        while f.measure(text) > 146 and size > 16:
            size -= 1
            f.configure(size=-size)
        self._amount_font = f
        self.canvas.itemconfigure(self.txt_big, font=f)

    def _ellipsize(self, text, font, max_w):
        if font.measure(text) <= max_w:
            return text
        s = text
        while s and font.measure(s + "…") > max_w:
            s = s[:-1]
        return (s + "…") if s else ""

    def _compose_sub(self, d, unit):
        """拼副标题：按信息量从多到少排队，取第一个放得下的"""
        tc = fmt_cost(d.get("today_cost"), unit)
        hm = time.strftime("%H:%M", time.localtime(self.last_time or time.time()))
        lim = fmt_money(d.get("limit"), unit) if d.get("limit") else None
        days = d.get("days_left")
        has_days = isinstance(days, int) and days <= 365

        cands = []
        if lim and tc:
            cands.append("限额 %s · 今日 %s · %s 更新" % (lim, tc, hm))
            cands.append("限额 %s · 今日 %s" % (lim, tc))
        if lim:
            cands.append("限额 %s · %s 更新" % (lim, hm))
            cands.append("限额 %s" % lim)
        note = d.get("sub_note")
        if note and not tc:
            cands.append("%s · %s 更新" % (note, hm))
        if tc:
            if has_days:
                cands.append("今日 %s · %d天到期 · %s 更新" % (tc, days, hm))
            cands.append("今日 %s · %s 更新" % (tc, hm))
            cands.append("今日 %s" % tc)
        if has_days:
            cands.append("%d天后到期 · %s 更新" % (days, hm))
        cands.append(hm + " 更新")
        for c in cands:
            if self.f_sub.measure(c) <= 150:
                return c
        return self._ellipsize(cands[0], self.f_sub, 150)

    def _apply_demo(self):
        d = parse_sub2api({
            "mode": "unrestricted",
            "isValid": True,
            "planName": "钱包余额",
            "remaining": 12.3456,
            "balance": 12.3456,
            "unit": "USD",
            "usage": {"today": {"actual_cost": 0.2831, "total_tokens": 182344},
                      "total": {"actual_cost": 5.914}},
        })
        self.last_time = time.time()
        self.show_data(d)

    # ---------------- 菜单 ----------------
    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, font=("Microsoft YaHei UI", 9))
        self.menu.add_command(label="状态：未读取", state="disabled")
        self.menu.add_separator()
        self.acc_menu = tk.Menu(self.menu, tearoff=0, font=("Microsoft YaHei UI", 9))
        self.var_acc = tk.IntVar(value=int(self.conf.get("active") or 0))
        self.menu.add_cascade(label="切换账号", menu=self.acc_menu)
        self.menu.add_command(label="全部账号余额…", command=self.open_overview)
        self.menu.add_command(label="立即刷新", command=self.refresh)
        self.menu.add_command(label="设置…", command=self.open_settings)
        self.skin_menu = tk.Menu(self.menu, tearoff=0, font=("Microsoft YaHei UI", 9))
        skin = self.conf.get("mascot", "cat")
        if skin not in MASCOT_ORDER:
            skin = "cat"
            self.conf["mascot"] = skin
        self.var_mascot = tk.StringVar(value=skin)
        for name in MASCOT_ORDER:
            self.skin_menu.add_radiobutton(
                label=MASCOT_NAMES[name], value=name, variable=self.var_mascot,
                command=lambda name=name: self.select_mascot(name))
        self.menu.add_cascade(label="切换造型", menu=self.skin_menu)
        self.menu.add_separator()
        self.var_top = tk.BooleanVar(value=bool(self.conf.get("topmost", True)))
        self.menu.add_checkbutton(label="窗口置顶", variable=self.var_top,
                                  command=self.toggle_topmost)
        self.var_auto = tk.BooleanVar(value=os.path.exists(STARTUP_VBS))
        self.menu.add_checkbutton(label="开机自动启动", variable=self.var_auto,
                                  command=self.toggle_autostart)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.quit)

    def _on_menu(self, e):
        # Use regular widgets instead of the native popup menu: some Windows
        # setups leave its rows blank until the pointer highlights them.
        existing = getattr(self, "action_win", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        win = tk.Toplevel(self.root)
        self.action_win = win
        win.title("挂件操作")
        win.configure(bg="#FFFFFF")
        win.resizable(True, True)
        win.wm_attributes("-topmost", True)
        win.minsize(350, 420)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(0, min(e.x_root, sw - 390))
        y = max(0, min(e.y_root, sh - 540))
        win.geometry("390x500+%d+%d" % (x, y))
        fam = ("Microsoft YaHei UI", 10)
        def run(fn):
            win.destroy()
            fn()
        # Reserve the exit row before allocating space to the main content.
        footer = tk.Frame(win, bg="#FFFFFF", padx=18, pady=12)
        footer.pack(side="bottom", fill="x")
        tk.Button(footer, text="退出挂件", command=lambda: run(self.quit),
                  font=fam, fg="#B42318").pack(fill="x")
        frame = tk.Frame(win, bg="#FFFFFF", padx=18, pady=14)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="挂件操作", font=("Microsoft YaHei UI", 13, "bold"),
                 bg="#FFFFFF", fg=C_TEXT).pack(anchor="w")
        tk.Label(frame, text=self.menu.entrycget(0, "label"), font=("Microsoft YaHei UI", 9),
                 bg="#FFFFFF", fg="#667085", wraplength=340, justify="left").pack(anchor="w", pady=(4, 12))
        tk.Label(frame, text="切换账号", bg="#FFFFFF", fg=C_TEXT, font=fam).pack(anchor="w")
        accounts = ttk.Combobox(frame, state="readonly", font=fam,
                                values=[a.get("name") or "账号" for a in self.accounts()])
        accounts.pack(fill="x", pady=(4, 10))
        accounts.current(int(self.conf.get("active") or 0))
        def pick_account(event):
            index = accounts.current()
            run(lambda: self.select_account(index))
        accounts.bind("<<ComboboxSelected>>", pick_account)
        tk.Label(frame, text="切换造型", bg="#FFFFFF", fg=C_TEXT, font=fam).pack(anchor="w")
        skins = ttk.Combobox(frame, state="readonly", font=fam,
                             values=[MASCOT_NAMES[s] for s in MASCOT_ORDER])
        skins.pack(fill="x", pady=(4, 12))
        skin = self.conf.get("mascot", "cat")
        skins.current(MASCOT_ORDER.index(skin) if skin in MASCOT_ORDER else 0)
        def pick_skin(event):
            selected = MASCOT_ORDER[skins.current()]
            run(lambda: self.select_mascot(selected))
        skins.bind("<<ComboboxSelected>>", pick_skin)
        self.var_top.set(bool(self.conf.get("topmost", True)))
        self.var_auto.set(os.path.exists(STARTUP_VBS))
        for text, var, command in (("窗口置顶", self.var_top, self.toggle_topmost),
                                   ("开机自动启动", self.var_auto, self.toggle_autostart)):
            tk.Checkbutton(frame, text=text, variable=var, command=command, font=fam,
                           bg="#FFFFFF", fg=C_TEXT, activebackground="#FFFFFF",
                           selectcolor="#FFFFFF").pack(anchor="w", pady=3)
        row = tk.Frame(frame, bg="#FFFFFF")
        row.pack(fill="x", pady=(14, 5))
        for text, fn in (("立即刷新", self.refresh), ("全部余额", self.open_overview), ("设置", self.open_settings)):
            tk.Button(row, text=text, command=lambda fn=fn: run(fn), font=fam).pack(side="left", expand=True, fill="x", padx=2)
        win.update_idletasks()
        needed_w = max(390, win.winfo_reqwidth())
        needed_h = max(500, win.winfo_reqheight())
        win.minsize(needed_w, needed_h)
        x = max(0, min(e.x_root, sw - needed_w - 16))
        y = max(0, min(e.y_root, sh - needed_h - 80))
        win.geometry("%dx%d+%d+%d" % (needed_w, needed_h, x, y))
        win.bind("<Escape>", lambda event: win.destroy())

    def _update_menu_status(self, plan=None):
        d = self.last_data
        if not d:
            return
        parts = [self.current().get("name") or "账号"]
        if plan:
            parts.append(plan)
        if not d.get("unlimited"):
            parts.append("剩余 " + fmt_money(d.get("remaining"), d.get("unit")))
        if d.get("total_cost") is not None:
            parts.append("累计 " + (fmt_cost(d.get("total_cost"), d.get("unit")) or "—"))
        self.menu.entryconfigure(0, label="状态：" + " · ".join(parts))

    def toggle_topmost(self):
        self.conf["topmost"] = bool(self.var_top.get())
        self.root.wm_attributes("-topmost", self.conf["topmost"])
        save_conf(self.conf)

    def toggle_autostart(self):
        if self.var_auto.get():
            self._install_autostart()
        else:
            try:
                if os.path.exists(STARTUP_VBS):
                    os.remove(STARTUP_VBS)
            except Exception:
                pass

    def _install_autostart(self):
        pyw = sys.executable
        cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(cand):
            pyw = cand
        script = os.path.abspath(__file__)
        vbs = (
            'Set sh = CreateObject("WScript.Shell")\r\n'
            'sh.Run """%s"" ""%s""", 0, False\r\n' % (pyw, script)
        )
        try:
            os.makedirs(STARTUP_DIR, exist_ok=True)
            # newline="" 防止文本模式把 \n 再转一次，导致 \r\r\n（WSH 会解析异常）
            with open(STARTUP_VBS, "w", encoding="ascii", newline="") as f:
                f.write(vbs)
        except Exception:
            pass

    # ---------------- 刷新 ----------------
    def refresh(self):
        if self.demo:
            return
        acc = self.current()
        if self.loading and self._request_identity == self._account_identity(acc):
            return
        acc = self.current()          # 传引用，令牌轮换后能写回配置
        if acc.get("auth") == "login":
            if not (acc.get("password") or acc.get("refresh_token")):
                self.set_status("nokey")
                return
        elif not (acc.get("key") or acc.get("cookie") or "").strip():
            self.set_status("nokey")
            return
        self.loading = True
        self.set_status("loading")
        self._request_id += 1
        identity = self._account_identity(acc)
        self._request_identity = identity
        threading.Thread(target=self._worker,
                         args=(dict(acc), self._request_id, acc, identity), daemon=True).start()

    @staticmethod
    def _account_identity(acc):
        return tuple(acc.get(k) or "" for k in ("base", "auth", "email", "key", "password", "style", "cookie", "user_id"))

    def _worker(self, acc, request_id, original, identity):
        try:
            data, raw, style = fetch_account(acc)
            self.q.put((request_id, original, identity, acc, "ok", data, raw))
        except Exception as e:
            self.q.put((request_id, original, identity, acc, "err", connection_error(e), ""))

    def _pump(self):
        # 把后台线程的结果搬回主线程（tkinter 不能在子线程里碰控件）
        try:
            while True:
                request_id, original, identity, updated, kind, payload, raw = self.q.get_nowait()
                valid_response = (request_id == self._request_id and self.current() is original
                                  and self._account_identity(original) == identity)
                # Metadata can be retained for the original account even after
                # switching, but its balance must never paint another account.
                if any(a is original for a in self.accounts()) and self._account_identity(original) == identity:
                    changed = False
                    for field in ("key", "refresh_token", "flavor", "detected_style", "detected_base"):
                        if field in updated and original.get(field) != updated[field]:
                            original[field] = updated[field]
                            changed = True
                    if changed:
                        try:
                            save_conf(self.conf)
                        except RuntimeError:
                            pass  # save_conf already displayed a visible error
                if not valid_response:
                    continue
                self.loading = False
                if kind == "ok":
                    self.last_raw = raw
                    self.last_time = time.time()
                    self.show_data(payload)
                else:
                    self.last_raw = raw
                    self.set_status("error", payload)
                    self.menu.entryconfigure(0, label="状态：" + payload[:40])
        except queue.Empty:
            pass

        try:
            while True:
                fn = self.ui_q.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass

        now = time.time()
        if now - self._last_visibility_check >= 2:
            self._last_visibility_check = now
            self._ensure_visible()
        if now > self.next_blink:
            self.blink_until = now + 0.13
            self.next_blink = now + random.uniform(2.5, 6.0)
        if not self._is_full_body():
            self._draw_mascot()
        self.root.after(120, self._pump)

    def _schedule(self):
        sec = max(15, int(self.conf.get("interval") or 60))
        self.root.after(sec * 1000, self._tick_refresh)

    def _tick_refresh(self):
        self.refresh()
        self._schedule()

    # ---------------- 全部账号一览 ----------------
    def open_overview(self):
        try:
            if self.ov_win and self.ov_win.winfo_exists():
                self.ov_win.lift()
                self.ov_win.focus_force()
                self.load_overview()
                return
        except AttributeError:
            pass
        win = tk.Toplevel(self.root)
        self.ov_win = win
        win.title("全部账号余额")
        win.configure(bg="#FFFFFF")
        win.resizable(True, True)
        win.minsize(520, 300)
        ov = self.conf.get("overview_size")
        if isinstance(ov, list) and len(ov) == 2:
            try:
                win.geometry("%dx%d" % (max(520, int(ov[0])), max(300, int(ov[1]))))
            except Exception:
                win.geometry("580x360")
        else:
            win.geometry("580x360")
        try:
            win.wm_attributes("-topmost", True)
        except tk.TclError:
            pass

        tk.Label(win, text="全部账号", bg="#FFFFFF", fg=C_TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        body = tk.Frame(win, bg="#FFFFFF")
        body.pack(fill="both", expand=True, padx=16)
        self.ov_font = tkfont.Font(family="Microsoft YaHei UI", size=10)
        self.ov_heading_font = tkfont.Font(family="Microsoft YaHei UI", size=10, weight="bold")
        style = ttk.Style(win)
        style.configure("Balances.Treeview", font=self.ov_font,
                        rowheight=max(34, self.ov_font.metrics("linespace") + 14),
                        background="#FFFFFF", fieldbackground="#FFFFFF", foreground=C_TEXT)
        style.configure("Balances.Treeview.Heading", font=self.ov_heading_font, padding=(10, 8))
        tv = ttk.Treeview(body, columns=("name", "bal", "today", "note"),
                          show="headings", height=7, style="Balances.Treeview")
        tv.tag_configure("even", background="#F5F6FC")
        tv.tag_configure("odd", background="#FFFFFF")
        for col, txt, wdt, anchor in (
            ("name", "账号", 120, "w"), ("bal", "剩余", 100, "e"),
            ("today", "今日", 90, "e"), ("note", "说明", 190, "w"),
        ):
            tv.heading(col, text=txt)
            tv.column(col, width=wdt, anchor=anchor)
        tv.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=tv.yview)
        sb.pack(side="right", fill="y")
        tv.configure(yscrollcommand=sb.set)
        self.ov_tv = tv

        bar = tk.Frame(win, bg="#FFFFFF")
        bar.pack(side="bottom", fill="x", padx=16, pady=10)
        body.pack_forget()
        body.pack(fill="both", expand=True, padx=16, pady=(0, 4))
        self.ov_status = tk.Label(bar, text="", bg="#FFFFFF", fg=C_SUB,
                                  font=("Microsoft YaHei UI", 8))
        self.ov_status.pack(side="left")

        def close_ov():
            try:
                self.conf["overview_size"] = [win.winfo_width(), win.winfo_height()]
                save_conf(self.conf)
            except Exception:
                pass
            win.destroy()

        tk.Button(bar, text="关闭", command=close_ov, width=8,
                  font=("Microsoft YaHei UI", 9)).pack(side="right")
        tk.Button(bar, text="刷新全部", command=self.load_overview, width=10,
                  font=("Microsoft YaHei UI", 9), bg=C_ACCENT, fg="#FFFFFF",
                  relief="flat").pack(side="right", padx=(0, 6))
        win.protocol("WM_DELETE_WINDOW", close_ov)
        self.load_overview()

    def load_overview(self):
        self._overview_generation = getattr(self, "_overview_generation", 0) + 1
        generation = self._overview_generation
        tv, window, label = self.ov_tv, self.ov_win, self.ov_status
        tv.delete(*tv.get_children())
        accounts = list(self.accounts())
        snapshots = [dict(a) for a in accounts]
        identities = [self._account_identity(a) for a in accounts]
        for i, account in enumerate(accounts):
            tv.insert("", "end", iid=str(i), tags=("even" if i % 2 == 0 else "odd",),
                      values=(account.get("name") or "账号", "…", "", "连接中"))
        label.configure(text="正在并行读取 %d 个账号…" % len(accounts))
        completed = [0]
        def apply(i, parsed, note):
            if generation != self._overview_generation or not window.winfo_exists():
                return
            original = accounts[i]
            if any(a is original for a in self.accounts()) and self._account_identity(original) == identities[i]:
                balance = "∞" if parsed.get("unlimited") else fmt_money(parsed.get("remaining"), parsed.get("unit"))
                today = fmt_cost(parsed.get("today_cost"), parsed.get("unit")) or "—"
                tv.item(str(i), values=(original.get("name") or "账号", balance, today, note))
                for field in ("key", "refresh_token", "flavor", "detected_style", "detected_base"):
                    if field in snapshots[i]:
                        original[field] = snapshots[i][field]
            completed[0] += 1
            label.configure(text="已完成 %d / %d" % (completed[0], len(accounts)))
            if completed[0] == len(accounts):
                save_conf(self.conf)
        def query(i):
            try:
                parsed, raw, style = fetch_account(snapshots[i], timeout=6)
                note = (parsed.get("plan") or "") + " · " + style
            except Exception as error:
                parsed, note = {}, connection_error(error)
            self.ui_q.put(lambda i=i, parsed=parsed, note=note: apply(i, parsed, note))
        def coordinator():
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(query, range(len(accounts))))
        threading.Thread(target=coordinator, daemon=True).start()

    def _ov_row(self, i, bal, today, note):
        try:
            self.ov_tv.item(str(i), values=(self.accounts()[i].get("name") or "账号",
                                            bal, today, note))
        except Exception:
            pass

    # ---------------- 设置窗口 ----------------
    def open_settings(self, first_run=False):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("余额挂件 · 设置")
        win.configure(bg="#FFFFFF")
        win.resizable(True, True)
        win.geometry("+%d+%d" % (max(60, int(self.x) - 220), max(40, int(self.y) - 260)))
        try:
            win.wm_attributes("-topmost", True)
        except tk.TclError:
            pass

        fam = ("Microsoft YaHei UI", 9)
        small = ("Microsoft YaHei UI", 8)

        # 先把底部三块占好位置，窗口再矮也不会把按钮挤没
        bar = tk.Frame(win, bg="#FFFFFF")
        bar.pack(side="bottom", fill="x", padx=16, pady=(8, 14))
        bottom = tk.Frame(win, bg="#FFFFFF")
        bottom.pack(side="bottom", fill="x", padx=16, pady=(6, 0))
        status = tk.Label(win, text="", bg="#FFFFFF", fg=C_SUB, font=small,
                          anchor="w", justify="left")
        status.pack(side="bottom", fill="x", padx=16)

        top = tk.Frame(win, bg="#FFFFFF")
        top.pack(fill="both", expand=True, padx=16, pady=(14, 0))

        # 左：账号列表
        left = tk.Frame(top, bg="#FFFFFF")
        left.pack(side="left", fill="both")
        tk.Label(left, text="账号列表", bg="#FFFFFF", fg=C_TEXT,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        tk.Label(left, text="可多个站点 / 多个 Key / 多个登录态", bg="#FFFFFF", fg=C_SUB,
                 font=small).pack(anchor="w")
        lb = tk.Listbox(left, width=34, height=12, font=fam, activestyle="none",
                        relief="solid", bd=1, exportselection=False)
        lb.pack(pady=(5, 6), fill="both", expand=True)
        lb_btn = tk.Frame(left, bg="#FFFFFF")
        lb_btn.pack(fill="x")

        # 右：表单
        right = tk.Frame(top, bg="#FFFFFF")
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        def row_text(a):
            base = (a.get("base") or "").replace("https://", "").replace("http://", "").rstrip("/")
            return "%s  ·  %s" % (a.get("name") or "账号", base)

        for a in self.accounts():
            lb.insert("end", row_text(a))

        def field(label, hint=""):
            tk.Label(right, text=label, bg="#FFFFFF", fg=C_TEXT, font=fam).pack(anchor="w",
                                                                               pady=(6, 0))
            if hint:
                tk.Label(right, text=hint, bg="#FFFFFF", fg=C_SUB,
                         font=small).pack(anchor="w")
            e = tk.Entry(right, font=("Consolas", 10), relief="solid", bd=1)
            e.pack(fill="x", ipady=3, pady=(2, 0))
            return e

        # 认证方式
        row_auth = tk.Frame(right, bg="#FFFFFF")
        row_auth.pack(fill="x")
        tk.Label(row_auth, text="认证方式", bg="#FFFFFF", fg=C_TEXT, font=fam).pack(side="left")
        var_auth = tk.StringVar(value="key")
        rb_kw = dict(variable=var_auth, bg="#FFFFFF", activebackground="#FFFFFF",
                     font=fam, highlightthickness=0, bd=0)
        tk.Radiobutton(row_auth, text="API Key", value="key", **rb_kw).pack(side="left", padx=(10, 0))
        tk.Radiobutton(row_auth, text="账号密码", value="login",
                       **rb_kw).pack(side="left", padx=(6, 0))
        tk.Radiobutton(row_auth, text="粘贴 Token", value="token",
                       **rb_kw).pack(side="left", padx=(6, 0))

        e_name = field("显示名称", "随便起，方便区分，比如「KK 主号」")
        e_base = field("站点地址", "例如 https://example.com（不用带 /v1）")

        lbl_user = tk.Label(right, text="登录账号（邮箱或用户名）", bg="#FFFFFF", fg=C_TEXT, font=fam)
        e_user = tk.Entry(right, font=("Consolas", 10), relief="solid", bd=1)

        lbl_secret = tk.Label(right, text="API Key", bg="#FFFFFF", fg=C_TEXT, font=fam)
        lbl_secret.pack(anchor="w", pady=(6, 0))
        lbl_hint = tk.Label(right, text="", bg="#FFFFFF", fg=C_SUB, font=small,
                            justify="left", wraplength=300)
        lbl_hint.pack(anchor="w")
        e_key = tk.Entry(right, font=("Consolas", 10), relief="solid", bd=1)
        e_key.pack(fill="x", ipady=3, pady=(2, 0))
        e_key.configure(show="•")

        row = tk.Frame(right, bg="#FFFFFF")
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="接口类型", bg="#FFFFFF", fg=C_TEXT, font=fam).pack(side="left")
        var_style = tk.StringVar(value="auto")
        cb = ttk.Combobox(row, textvariable=var_style, width=10, state="readonly",
                          values=("auto", "sub2api", "openai"), font=fam)
        cb.pack(side="left", padx=(8, 0))
        lbl_style_hint = tk.Label(row, text="auto = 自动探测", bg="#FFFFFF", fg=C_SUB,
                                  font=small)
        lbl_style_hint.pack(side="left", padx=(6, 0))

        show = tk.BooleanVar(value=False)
        tk.Checkbutton(right, text="显示内容", variable=show,
                       command=lambda: e_key.configure(show="" if show.get() else "•"),
                       bg="#FFFFFF", fg=C_SUB, font=small,
                       activebackground="#FFFFFF", highlightthickness=0, bd=0).pack(anchor="w",
                                                                                    pady=(6, 0))
        tip = tk.Label(right, text="", bg="#FFFFFF", fg=C_SUB, font=small,
                       justify="left", wraplength=310)
        tip.pack(anchor="w", pady=(12, 0))

        def sync_auth_ui(*_):
            mode = var_auth.get()
            if mode == "login":
                lbl_user.pack(anchor="w", pady=(6, 0), before=lbl_secret)
                e_user.pack(fill="x", ipady=3, pady=(2, 0), before=lbl_secret)
                lbl_secret.configure(text="登录密码")
                lbl_hint.configure(text="填面板的账号和密码（sub2api 这类填邮箱，"
                                        "new-api 站点填用户名），登录后自动续期")
                cb.configure(state="disabled")
                lbl_style_hint.configure(text="账号模式不适用")
                tip.configure(text="最省事：填一次邮箱密码，挂件自己登录并在过期前自动续期。\n"
                                   "凭证用Windows当前用户加密保存；换电脑或Windows用户需重新绑定。")
            if mode != "login":
                lbl_user.pack_forget()
                e_user.pack_forget()
            if mode == "token":
                lbl_secret.configure(text="登录 Token（JWT）")
                lbl_hint.configure(
                    text="浏览器登录站点 → F12 → Network 里点任意一个 /api/ 请求 → "
                         "复制请求头 Authorization 里 Bearer 后面那一整串"
                         "（sub2api 也可用控制台 copy(localStorage.getItem('auth_token'))）")
                cb.configure(state="disabled")
                lbl_style_hint.configure(text="Token 模式不适用")
                tip.configure(text="Token 模式读面板 /api/v1/user/profile，能拿到「钱包余额」，"
                                   "不受 Key 限额模式影响；Token 会过期，过期后挂件会提示重新粘贴。")
            elif mode == "key":
                lbl_secret.configure(text="API Key")
                lbl_hint.configure(text="在该站点的「密钥/令牌」页面复制，一般以 sk- 开头")
                cb.configure(state="readonly")
                lbl_style_hint.configure(text="auto = 自动探测")
                tip.configure(text="小技巧：单击挂件切换账号，右键菜单可看全部账号余额。\n"
                                   "只想看余额、不想暴露能花钱的 Key，用「账号密码」或「粘贴 Token」。")
            # 切模式后内容高度会变（多一行/少一行），等布局稳定再让窗口长高
            def _grow():
                try:
                    win.update_idletasks()
                    need = win.winfo_reqheight()
                    if win.winfo_width() > 100 and win.winfo_height() < need:
                        win.geometry("%dx%d" % (win.winfo_width(), need))
                except Exception:
                    pass

            win.after(30, _grow)

        var_auth.trace_add("write", sync_auth_ui)
        sync_auth_ui()

        # ---- 交互 ----
        editing = {"i": None}
        guard = {"busy": False}

        def fill(i):
            guard["busy"] = True
            try:
                editing["i"] = i
                a = self.accounts()[i]
                e_name.delete(0, "end"); e_name.insert(0, a.get("name") or "")
                e_base.delete(0, "end"); e_base.insert(0, a.get("base") or "")
                e_user.delete(0, "end"); e_user.insert(0, a.get("email") or "")
                secret = a.get("password") if (a.get("auth") or "key") == "login" else a.get("key")
                e_key.delete(0, "end"); e_key.insert(0, secret or ("（浏览器会话已绑定）" if a.get("auth") == "cookie" else ""))
                var_auth.set("token" if a.get("auth") == "cookie" else (a.get("auth") or "key"))
                var_style.set(a.get("style") or "auto")
                lb.selection_clear(0, "end")
                lb.selection_set(i)
                lb.see(i)
            finally:
                guard["busy"] = False
            sync_auth_ui()

        pending_binding = {}
        def commit(i, quiet=False):
            a = self.accounts()[i]
            old_base = a.get("base")
            a["name"] = e_name.get().strip() or "账号"
            b = e_base.get().strip()
            a["base"] = norm_base(b) if b else DEFAULT_BASE
            old_cookie = a.get("auth") == "cookie" and a.get("cookie") and old_base == norm_base(e_base.get())
            a["auth"] = "cookie" if old_cookie and var_auth.get() == "token" else (var_auth.get() or "key")
            a["style"] = var_style.get() or "auto"
            if a["auth"] == "login":
                a["email"] = e_user.get().strip()
                a["password"] = e_key.get().strip()
                a["key"] = ""
            else:
                a["key"] = "" if a["auth"] == "cookie" else e_key.get().strip()
                a["password"] = ""
                a["email"] = e_user.get().strip()
            if pending_binding.get("index") == i and pending_binding.get("base") == a["base"]:
                a.update(pending_binding["fields"])
                pending_binding.clear()
            elif a["auth"] not in ("login", "token", "cookie"):
                a["refresh_token"] = ""
                a["cookie"] = ""
            guard["busy"] = True
            try:
                lb.delete(i)
                lb.insert(i, row_text(a))
                lb.selection_clear(0, "end")
                lb.selection_set(i)
            finally:
                guard["busy"] = False
            editing["i"] = i
            if not quiet:
                status.configure(text="已保存「%s」" % a["name"], fg=C_OK)
            return a

        def on_pick(evt=None):
            if guard["busy"]:
                return
            sel = lb.curselection()
            if sel and sel[0] != editing["i"]:
                fill(sel[0])

        lb.bind("<<ListboxSelect>>", on_pick)

        def do_add():
            if editing["i"] is not None:
                commit(editing["i"], quiet=True)
            self.accounts().append(dict(NEW_ACCOUNT))
            lb.insert("end", row_text(NEW_ACCOUNT))
            fill(len(self.accounts()) - 1)
            e_name.focus_set()
            e_name.select_range(0, "end")
            status.configure(text="已新增一行：填好后点「保存此账号」，再点「保存并刷新」让它生效",
                             fg=C_ACCENT)

        def do_save_one():
            """只保存当前这个账号，不关窗口"""
            i = editing["i"]
            if i is None:
                status.configure(text="先在左边选一个账号", fg=C_WARN)
                return
            if not e_key.get().strip():
                status.configure(text="凭证还空着：%s" % (
                    "登录密码" if var_auth.get() == "login" else "API Key / Token"), fg=C_ERR)
                return
            if var_auth.get() == "login" and not e_user.get().strip():
                status.configure(text="账号密码模式还要填用户名", fg=C_ERR)
                return
            a = commit(i)
            save_conf(self.conf)
            extra = "（Token 模式，读钱包余额）" if a.get("auth") == "token" else ""
            status.configure(text="已保存「%s」%s" % (a["name"], extra), fg=C_OK)
            if int(self.conf.get("active") or 0) == i:
                self.refresh()

        def do_del():
            if len(self.accounts()) <= 1:
                status.configure(text="至少留一个账号", fg=C_WARN)
                return
            i = editing["i"] if editing["i"] is not None else (lb.curselection() or (0,))[0]
            name = self.accounts()[i].get("name")
            del self.accounts()[i]
            lb.delete(i)
            if self.conf.get("active") == i:
                self.conf["active"] = 0
            self.conf["active"] = min(int(self.conf.get("active") or 0), len(self.accounts()) - 1)
            editing["i"] = None
            fill(min(i, len(self.accounts()) - 1))
            status.configure(text="已删除「%s」" % name, fg=C_WARN)

        test_state = {"busy": False}
        def do_test():
            if test_state["busy"]:
                status.configure(text="连接测试正在进行，请稍候…", fg=C_SUB)
                return
            k = e_key.get().strip()
            mode = var_auth.get() or "key"
            if not k:
                status.configure(text="先填 %s" % {"login": "登录密码", "token": "登录 Token"}
                                 .get(mode, "API Key"), fg=C_ERR)
                return
            if mode == "login" and not e_user.get().strip():
                status.configure(text="账号密码模式还要填用户名", fg=C_ERR)
                return
            acc = {"name": e_name.get(), "base": norm_base(e_base.get()),
                   "key": k, "auth": mode, "style": var_style.get() or "auto"}
            if mode == "login":
                acc["email"] = e_user.get().strip()
                acc["password"] = k
                acc["key"] = ""
            index = editing["i"]
            original = self.accounts()[index] if index is not None else None
            original_identity = self._account_identity(original) if original is not None else None
            if original is not None and norm_base(original.get("base")) == acc["base"]:
                for field in ("flavor", "detected_style", "detected_base", "cookie", "user_id"):
                    if field in original:
                        acc[field] = original[field]
            test_state["busy"] = True
            if pending_binding.get("index") == index and pending_binding.get("base") == acc["base"]:
                acc.update(pending_binding["fields"])
            elif original is not None and original.get("auth") == "cookie" and original.get("base") == acc["base"] and k == "（浏览器会话已绑定）":
                acc.update(auth="cookie", cookie=original.get("cookie"), user_id=original.get("user_id"), key="")
            status.configure(text="正在连接 %s …（单次请求超时 6 秒）" % acc["base"], fg=C_SUB)

            def run():
                started = time.monotonic()
                try:
                    d, raw, style = fetch_account(acc, timeout=6)
                    self.last_raw = raw
                    txt = "连接成功（%s）：%s 剩余 %s" % (
                        style, d.get("plan") or "OK",
                        "∞" if d.get("unlimited") else fmt_money(d.get("remaining"), d.get("unit")))
                    txt += " · %.1f 秒" % (time.monotonic() - started)
                    def finish_test(text=txt):
                        if (original is not None and any(a is original for a in self.accounts())
                                and self._account_identity(original) == original_identity
                                and norm_base(original.get("base")) == acc["base"]):
                            for field in ("flavor", "detected_style", "detected_base", "cookie", "user_id"):
                                if field in acc:
                                    original[field] = acc[field]
                            save_conf(self.conf)
                        if win.winfo_exists():
                            status.configure(text=text, fg=C_OK)
                    self.ui_q.put(finish_test)
                except Exception as e:
                    msg = "%s · %.1f 秒" % (connection_error(e)[:160], time.monotonic() - started)
                    self.ui_q.put(lambda text=msg: status.configure(text=text, fg=C_ERR))
                finally:
                    test_state["busy"] = False

            threading.Thread(target=run, daemon=True).start()

        bind_state = {"busy": False}
        def do_browser_bind():
            if bind_state["busy"]:
                return
            index = editing["i"]
            base = norm_base(e_base.get())
            remember = var_remember.get()
            bind_state["busy"] = True
            status.configure(text="请在独立浏览器中登录，完成后自动填入；3分钟内有效。", fg=C_SUB)
            def worker():
                try:
                    import browser_binding
                    credentials = browser_binding.bind(base, remember=remember)
                    fields = {"key": credentials.get("access_token") or "",
                              "refresh_token": credentials.get("refresh_token") or "",
                              "cookie": credentials.get("cookie") or "",
                              "user_id": str(credentials.get("user_id") or ""),
                              "flavor": credentials.get("flavor") or "sub2api",
                              "detected_base": base,
                              "auth": "cookie" if credentials.get("cookie") else "token"}
                    candidate = dict(fields, base=base)
                    data, raw, style = fetch_account(candidate, timeout=6)
                    if data.get("remaining") is None:
                        raise RuntimeError("登录成功，但没有读到余额，未保存绑定")
                    def finish():
                        if not win.winfo_exists():
                            return
                        if editing["i"] != index or norm_base(e_base.get()) != base:
                            status.configure(text="当前账号或地址已变化，请重新绑定。", fg=C_WARN)
                            return
                        var_auth.set("token")
                        e_key.delete(0, "end")
                        e_key.insert(0, fields["key"] or "（浏览器会话已绑定）")
                        pending_binding.update(index=index, base=base, fields=fields)
                        sync_auth_ui()
                        status.configure(text="绑定已验证，请保存此账号。支持刷新令牌时将自动续期；Cookie失效需重绑。", fg=C_OK)
                    self.ui_q.put(finish)
                except Exception as error:
                    message = connection_error(error)
                    self.ui_q.put(lambda message=message: status.configure(text=message, fg=C_ERR))
                finally:
                    bind_state["busy"] = False
            threading.Thread(target=worker, daemon=True).start()

        def show_raw():
            rw = tk.Toplevel(win)
            rw.title("连接诊断（已隐藏凭证）")
            rw.geometry("700x480")
            def redact(value):
                if isinstance(value, dict):
                    return {k: ("[已隐藏]" if any(s in k.lower() for s in
                            ("token", "password", "secret", "cookie", "authorization", "key")) else redact(v))
                            for k, v in value.items()}
                if isinstance(value, list):
                    return [redact(v) for v in value]
                return value
            try:
                body = json.dumps(redact(json.loads(self.last_raw or "{}")), indent=2, ensure_ascii=False)
            except Exception:
                body = "响应不是JSON，已省略原文以免泄露凭证。"
            text = "状态：" + str(status.cget("text")) + "\n\n" + body
            for secret in (e_key.get().strip(), e_user.get().strip()):
                if secret:
                    text = text.replace(secret, "[已隐藏]")
            for account in self.accounts():
                for field in ("key", "password", "refresh_token", "cookie"): 
                    secret = account.get(field)
                    if secret:
                        text = text.replace(str(secret), "[已隐藏]")
            t = tk.Text(rw, wrap="word", font=("Consolas", 10))
            t.pack(fill="both", expand=True)
            t.insert("1.0", text)
            def copy_diagnostic():
                rw.clipboard_clear()
                rw.clipboard_append(text)
            tk.Button(rw, text="复制脱敏诊断", command=copy_diagnostic).pack(pady=8)

        def do_save_all():
            if editing["i"] is not None:
                commit(editing["i"], quiet=True)
            try:
                self.conf["interval"] = max(15, min(3600, int(var_iv.get())))
            except Exception:
                self.conf["interval"] = 60
            self.conf["topmost"] = bool(var_top2.get())
            self.conf["x"], self.conf["y"] = int(self.x), int(self.y)
            save_conf(self.conf)
            self.root.wm_attributes("-topmost", self.conf["topmost"])
            self.var_top.set(self.conf["topmost"])
            win.destroy()
            self.refresh()

        def on_close():
            try:
                if win.winfo_width() > 100:
                    self.conf["settings_size"] = [win.winfo_width(), win.winfo_height()]
                    save_conf(self.conf)
            except Exception:
                pass
            win.destroy()

        # 左列按钮
        tk.Button(lb_btn, text="新增", command=do_add, width=6, font=fam).pack(side="left")
        tk.Button(lb_btn, text="保存此账号", command=do_save_one, width=10, font=fam,
                  bg="#EFEDFF", relief="flat").pack(side="left", padx=(4, 0))
        tk.Button(lb_btn, text="删除", command=do_del, width=6, font=fam).pack(side="left",
                                                                               padx=(4, 0))

        # 底部：全局设置
        tk.Label(bottom, text="刷新间隔", bg="#FFFFFF", fg=C_TEXT, font=fam).pack(side="left")
        var_iv = tk.IntVar(value=int(self.conf.get("interval") or 60))
        tk.Spinbox(bottom, from_=15, to=3600, increment=15, width=6,
                   textvariable=var_iv, font=("Consolas", 10),
                   relief="solid", bd=1).pack(side="left", padx=(8, 4))
        tk.Label(bottom, text="秒", bg="#FFFFFF", fg=C_SUB, font=fam).pack(side="left")
        var_top2 = tk.BooleanVar(value=bool(self.conf.get("topmost", True)))
        tk.Checkbutton(bottom, text="窗口置顶", variable=var_top2, bg="#FFFFFF", fg=C_TEXT,
                       font=fam, activebackground="#FFFFFF", highlightthickness=0,
                       bd=0).pack(side="left", padx=(16, 0))

        # 底部：操作按钮
        tk.Button(bar, text="关闭", command=on_close, width=8, font=fam).pack(side="right")
        tk.Button(bar, text="保存并刷新", command=do_save_all, width=11, font=fam,
                  bg=C_ACCENT, fg="#FFFFFF", relief="flat",
                  activebackground="#5A4BD1", activeforeground="#FFFFFF").pack(side="right",
                                                                              padx=(6, 0))
        var_remember = tk.BooleanVar(value=False)
        tk.Checkbutton(right, text="保留此站点的专用登录环境（本机）", variable=var_remember,
                       bg="#FFFFFF", font=small).pack(anchor="w", pady=(4, 0))
        tk.Button(right, text="用默认浏览器登录绑定", command=do_browser_bind,
                  font=fam).pack(fill="x", pady=(8, 0))
        tk.Button(bar, text="测试连接", command=do_test, width=9, font=fam).pack(side="right",
                                                                                padx=(6, 0))
        tk.Button(bar, text="连接诊断", command=show_raw, width=9, font=fam).pack(side="right",
                                                                                padx=(6, 0))

        fill(int(self.conf.get("active") or 0))
        if first_run:
            if not self.accounts()[0].get("key"):
                status.configure(text="首次使用：选认证方式 → 填地址和凭证 → 保存此账号 → 保存并刷新",
                                 fg=C_ACCENT)
            else:
                status.configure(text="检测到已有配置", fg=C_SUB)
        e_key.focus_set()
        win.protocol("WM_DELETE_WINDOW", on_close)

        # 按内容实际需要的尺寸开窗（高分屏字体更大也不会被裁），并记住上次拉的尺寸
        win.update_idletasks()
        need_w = max(660, win.winfo_reqwidth())
        need_h = max(560, win.winfo_reqheight())
        saved = self.conf.get("settings_size")
        w, h = need_w, need_h
        if isinstance(saved, list) and len(saved) == 2:
            try:
                w = max(need_w, int(saved[0]))
                h = max(need_h, int(saved[1]))
            except Exception:
                pass
        w = min(w, win.winfo_screenwidth() - 60)
        h = min(h, win.winfo_screenheight() - 90)
        win.minsize(min(need_w, w), min(need_h, h))
        win.geometry("%dx%d+%d+%d" % (w, h, win.winfo_x(), win.winfo_y()))
        win.lift()

    # ---------------- 退出 ----------------
    def quit(self):
        self.conf["x"], self.conf["y"] = int(self.x), int(self.y)
        self.conf["alpha"] = round(self.wheel_alpha, 2)
        save_conf(self.conf)
        self.root.destroy()


# --------------------------------------------------------------------------
def main():
    demo = "--demo" in sys.argv
    if "--check" in sys.argv:
        conf = load_conf()
        if not conf["accounts"]:
            print("还没有配置账号，先运行一次界面填写")
            return
        for a in conf["accounts"]:
            print("=" * 60)
            print(a.get("name"), a.get("base"))
            try:
                d, raw, style = fetch_account(a)
                print("style:", style)
                print(raw[:800])
                print("解析：", json.dumps(d, ensure_ascii=False, indent=2))
            except Exception as e:
                print("失败：", e)
        return

    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    if not instance_lock.acquire(CONF_PATH):
        messagebox.showinfo("挂件已在运行", "已有一个余额挂件正在运行，请使用现有窗口。")
        return
    try:
        conf = load_conf()
    except Exception:
        messagebox.showerror("配置读取失败", "配置损坏或无法由当前Windows用户解密。原文件未修改，请检查备份或联系排查。")
        return
    root = tk.Tk()
    def report_callback_error(kind, value, traceback):
        if "配置保存失败" not in str(value):
            messagebox.showerror("操作未完成", connection_error(value), parent=root)
    root.report_callback_exception = report_callback_error
    Bot(root, conf, demo=demo)
    root.mainloop()


if __name__ == "__main__":
    main()
