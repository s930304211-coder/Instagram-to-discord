#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ============================================================
# Instagram → Discord Monitor
#
# Render Production Final Version
#
# Features:
#   - Multiple Instagram accounts
#   - Instagram Session support
#   - Base64 Session Secret support
#   - Startup random delay
#   - 429 global cooldown
#   - Exponential backoff
#   - Random jitter
#   - Post-cooldown retry delay
#   - Persistent cooldown
#   - Persistent post state
#   - Discord Webhook
#   - Image / Video / Carousel
#   - Render Health Check
#   - Atomic state write
#   - Graceful shutdown
#   - Authentication error detection
#   - 401 / 403 / 429 detection
#   - Diagnostic startup logging
#
# IMPORTANT SAFETY BEHAVIOR:
#
#   429:
#       Global cooldown
#       + exponential backoff
#       + jitter
#       + post-cooldown safety delay
#
#   401 / 403 / LoginRequired /
#   Checkpoint / Challenge / Session invalid:
#
#       SAFE STOP
#       ↓
#       停止所有 Instagram polling
#       ↓
#       Health Check 繼續運作
#       ↓
#       不會自動重新登入
#       ↓
#       不會繼續碰 Instagram
#       ↓
#       需要人工確認帳號恢復後再重新啟動
# ============================================================


# ============================================================
# BOOT
# ============================================================

print(
    "BOOT: Python process started",
    flush=True,
)


# ============================================================
# Standard Library
# ============================================================

import base64
import binascii
import json
import os
import random
import re
import signal
import sys
import tempfile
import threading
import time

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


print(
    "BOOT: standard libraries imported",
    flush=True,
)


# ============================================================
# Third Party
# ============================================================

print(
    "BOOT: importing instaloader...",
    flush=True,
)

import instaloader

print(
    "BOOT: instaloader imported "
    f"version={getattr(instaloader, '__version__', 'unknown')}",
    flush=True,
)


print(
    "BOOT: importing requests...",
    flush=True,
)

import requests

print(
    "BOOT: requests imported "
    f"version={getattr(requests, '__version__', 'unknown')}",
    flush=True,
)


# ============================================================
# Configuration
# ============================================================

RAW_USERNAMES = os.environ.get(
    "IG_USERNAME",
    "",
)


USERNAMES = [
    username.strip()
    for username in RAW_USERNAMES.split(",")
    if username.strip()
]


# ============================================================
# Discord
# ============================================================

WEBHOOK_URL = os.environ.get(
    "INSTAGRAM_POST_WEBHOOK",
    os.environ.get(
        "WEBHOOK_URL",
        "",
    ),
).strip()


# ============================================================
# Polling
# ============================================================

CHECK_INTERVAL = int(
    os.environ.get(
        "CHECK_INTERVAL",
        "7200",
    )
)


ACCOUNT_DELAY = int(
    os.environ.get(
        "ACCOUNT_DELAY",
        "120",
    )
)


# ============================================================
# Startup Delay
#
# 避免 Render restart 後立刻碰 Instagram
# ============================================================

STARTUP_DELAY_MIN = int(
    os.environ.get(
        "STARTUP_DELAY_MIN",
        "120",
    )
)


STARTUP_DELAY_MAX = int(
    os.environ.get(
        "STARTUP_DELAY_MAX",
        "300",
    )
)


# ============================================================
# Cooldown Retry Delay
#
# 429 cooldown 結束後，不立即再次查同一個帳號。
# 會額外等待這段時間。
# ============================================================

COOLDOWN_RETRY_DELAY_MIN = int(
    os.environ.get(
        "COOLDOWN_RETRY_DELAY_MIN",
        "180",
    )
)


COOLDOWN_RETRY_DELAY_MAX = int(
    os.environ.get(
        "COOLDOWN_RETRY_DELAY_MAX",
        "420",
    )
)


# ============================================================
# Backoff
# ============================================================

INITIAL_BACKOFF = int(
    os.environ.get(
        "INITIAL_BACKOFF",
        "1200",
    )
)


MAX_BACKOFF = int(
    os.environ.get(
        "MAX_BACKOFF",
        "21600",
    )
)


BACKOFF_JITTER_MIN = int(
    os.environ.get(
        "BACKOFF_JITTER_MIN",
        "30",
    )
)


BACKOFF_JITTER_MAX = int(
    os.environ.get(
        "BACKOFF_JITTER_MAX",
        "120",
    )
)


# ============================================================
# State
# ============================================================

STATE_FILE = os.environ.get(
    "STATE_FILE",
    "last_posts.json",
).strip()


PERSIST_COOLDOWN = (
    os.environ.get(
        "PERSIST_COOLDOWN",
        "true",
    ).lower()
    == "true"
)


# ============================================================
# Instagram Session
#
# Render:
#
# IG_SESSION_USERNAME=aderiii_1225
#
# IG_SESSION_FILE=/etc/secrets/aderiii_1225.session.b64
#
# IG_SESSION_BASE64=true
# ============================================================

IG_SESSION_FILE = os.environ.get(
    "IG_SESSION_FILE",
    "",
).strip()


IG_SESSION_USERNAME = os.environ.get(
    "IG_SESSION_USERNAME",
    "",
).strip()


IG_SESSION_BASE64 = (
    os.environ.get(
        "IG_SESSION_BASE64",
        "true",
    ).lower()
    == "true"
)


# ============================================================
# Discord
# ============================================================

MAX_DISCORD_FILE_SIZE = int(
    os.environ.get(
        "MAX_DISCORD_FILE_SIZE",
        "9500000",
    )
)


MAX_MEDIA_ITEMS = 9


# ============================================================
# Global Runtime State
# ============================================================

shutdown_requested = False

rate_limit_until = 0.0

retry_not_before = 0.0

backoff_seconds = INITIAL_BACKOFF


# ============================================================
# Authentication SAFE STOP
#
# 一旦 Instagram 回報：
#
#   401
#   403
#   LoginRequired
#   Checkpoint
#   Challenge
#   Session invalid
#
# 就設定為 True。
#
# 後續不再進行任何 Instagram polling。
#
# Health Check Server 不受影響。
# ============================================================

instagram_auth_blocked = False

instagram_auth_block_reason = ""


# ============================================================
# Locks
# ============================================================

STATE_LOCK = threading.Lock()

RATE_LIMIT_LOCK = threading.Lock()

AUTH_LOCK = threading.Lock()

CYCLE_LOCK = threading.Lock()


# ============================================================
# HTTP Session
# ============================================================

HTTP = requests.Session()

HTTP.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/151.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
)


# ============================================================
# Custom Exceptions
# ============================================================

class InstagramRateLimited(Exception):

    def __init__(
        self,
        message,
        wait_seconds=None,
    ):
        super().__init__(message)
        self.wait_seconds = wait_seconds


class InstagramAuthenticationError(Exception):
    pass


# ============================================================
# Instaloader Rate Controller
# ============================================================

class AbortOn429RateController(
    instaloader.RateController
):
    """
    不讓 Instaloader 自己長時間 retry。
    遇到 429 直接交給主程式處理 global cooldown。
    """

    def handle_429(
        self,
        query_type,
    ):

        raise InstagramRateLimited(
            "Instagram HTTP 429 Too Many Requests "
            f"(query_type={query_type})"
        )


# ============================================================
# Instaloader
# ============================================================

print(
    "BOOT: creating Instaloader instance...",
    flush=True,
)


L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    max_connection_attempts=1,
    request_timeout=60,
    rate_controller=lambda context:
        AbortOn429RateController(context),
)


print(
    "BOOT: Instaloader instance created",
    flush=True,
)


# ============================================================
# Utility
# ============================================================

def format_seconds(seconds):

    seconds = max(
        0,
        int(seconds),
    )

    hours = seconds // 3600

    minutes = (
        (seconds % 3600)
        // 60
    )

    secs = seconds % 60

    if hours > 0:

        return (
            f"{hours}h "
            f"{minutes}m "
            f"{secs}s"
        )

    if minutes > 0:

        return (
            f"{minutes}m "
            f"{secs}s"
        )

    return f"{secs}s"


# ============================================================
# Interruptible Sleep
# ============================================================

def sleep_interruptible(seconds):

    global shutdown_requested

    remaining = max(
        0.0,
        float(seconds),
    )

    while (
        remaining > 0
        and not shutdown_requested
    ):

        chunk = min(
            remaining,
            1.0,
        )

        time.sleep(chunk)

        remaining -= chunk


# ============================================================
# Authentication SAFE STOP Helpers
# ============================================================

def is_auth_blocked():

    with AUTH_LOCK:

        return instagram_auth_blocked


def get_auth_block_reason():

    with AUTH_LOCK:

        return instagram_auth_block_reason


def trigger_auth_safe_stop(
    reason,
    error=None,
):

    global instagram_auth_blocked
    global instagram_auth_block_reason

    with AUTH_LOCK:

        if instagram_auth_blocked:

            return

        instagram_auth_blocked = True

        instagram_auth_block_reason = (
            str(reason)
        )

    print(
        "",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )

    print(
        "🛑 INSTAGRAM AUTHENTICATION SAFE STOP",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )

    print(
        "🔐 Instagram authentication 狀態異常。",
        flush=True,
    )

    print(
        "⚠️ 原因："
        f"{reason}",
        flush=True,
    )

    if error is not None:

        print(
            "⚠️ Error："
            f"{error}",
            flush=True,
        )

    print(
        "",
        flush=True,
    )

    print(
        "🛑 已停止所有 Instagram polling。",
        flush=True,
    )

    print(
        "🛑 不會自動重新登入。",
        flush=True,
    )

    print(
        "🛑 不會自動重新載入 Session。",
        flush=True,
    )

    print(
        "🛑 不會繼續下一個 Instagram 帳號。",
        flush=True,
    )

    print(
        "",
        flush=True,
    )

    print(
        "🌐 Health Check Server 仍會保持運作。",
        flush=True,
    )

    print(
        "👤 請人工確認 Instagram 帳號恢復正常。",
        flush=True,
    )

    print(
        "🔄 確認恢復後，再重新啟動 Render Service。",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )


# ============================================================
# State Load
# ============================================================

def load_state():

    global rate_limit_until
    global retry_not_before
    global backoff_seconds

    print(
        f"[STATE] Loading: {STATE_FILE}",
        flush=True,
    )

    if not os.path.exists(
        STATE_FILE
    ):

        print(
            f"[STATE] {STATE_FILE} 不存在。",
            flush=True,
        )

        print(
            "[STATE] 建立新的 State。",
            flush=True,
        )

        return {}

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as file:

            raw_data = json.load(file)

        if not isinstance(
            raw_data,
            dict,
        ):

            print(
                "[STATE] State 格式錯誤，重設。",
                flush=True,
            )

            return {}

        if "_meta" in raw_data:

            meta = raw_data.get(
                "_meta",
                {},
            )

            if not isinstance(
                meta,
                dict,
            ):

                meta = {}

            if PERSIST_COOLDOWN:

                try:

                    saved_until = float(
                        meta.get(
                            "rate_limit_until",
                            0.0,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):

                    saved_until = 0.0

                try:

                    saved_retry = float(
                        meta.get(
                            "retry_not_before",
                            0.0,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):

                    saved_retry = 0.0

                try:

                    saved_backoff = int(
                        meta.get(
                            "backoff_seconds",
                            INITIAL_BACKOFF,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):

                    saved_backoff = INITIAL_BACKOFF

                saved_backoff = min(
                    max(
                        saved_backoff,
                        INITIAL_BACKOFF,
                    ),
                    MAX_BACKOFF,
                )

                now = time.time()

                with RATE_LIMIT_LOCK:

                    if saved_until > now:

                        rate_limit_until = (
                            saved_until
                        )

                    if saved_retry > now:

                        retry_not_before = (
                            saved_retry
                        )

                    backoff_seconds = (
                        saved_backoff
                    )

                if saved_until > now:

                    print(
                        "[STATE] 恢復 Instagram cooldown。",
                        flush=True,
                    )

                    print(
                        "[STATE] Cooldown 剩餘："
                        f"{format_seconds(saved_until - now)}",
                        flush=True,
                    )

                if saved_retry > now:

                    print(
                        "[STATE] 恢復 cooldown 後等待。",
                        flush=True,
                    )

                    print(
                        "[STATE] 額外等待："
                        f"{format_seconds(saved_retry - now)}",
                        flush=True,
                    )

            posts_state = raw_data.get(
                "posts",
                {},
            )

        else:

            # 舊版本 State 相容
            posts_state = raw_data

        if not isinstance(
            posts_state,
            dict,
        ):

            posts_state = {}

        print(
            "[STATE] 已載入 "
            f"{len(posts_state)} 個帳號紀錄。",
            flush=True,
        )

        return posts_state

    except Exception as error:

        print(
            "[STATE] 讀取失敗："
            f"{error}",
            flush=True,
        )

        return {}


# ============================================================
# State Save
# ============================================================

def save_state(posts_state):

    try:

        state_path = Path(
            STATE_FILE
        )

        parent = state_path.parent

        if str(parent) not in (
            "",
            ".",
        ):

            parent.mkdir(
                parents=True,
                exist_ok=True,
            )

        with RATE_LIMIT_LOCK:

            current_until = (
                rate_limit_until
            )

            current_retry = (
                retry_not_before
            )

            current_backoff = (
                backoff_seconds
            )

        data = {
            "_meta": {
                "rate_limit_until": (
                    current_until
                    if PERSIST_COOLDOWN
                    else 0.0
                ),
                "retry_not_before": (
                    current_retry
                    if PERSIST_COOLDOWN
                    else 0.0
                ),
                "backoff_seconds": (
                    current_backoff
                    if PERSIST_COOLDOWN
                    else INITIAL_BACKOFF
                ),
                "updated_at": time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            },
            "posts": dict(posts_state),
        }

        temp_path = state_path.with_name(
            state_path.name + ".tmp"
        )

        with open(
            temp_path,
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )

            file.flush()

            os.fsync(
                file.fileno()
            )

        os.replace(
            temp_path,
            state_path,
        )

        print(
            "[STATE] State 已儲存。",
            flush=True,
        )

        return True

    except Exception as error:

        print(
            "[STATE] 儲存失敗："
            f"{error}",
            flush=True,
        )

        return False


# ============================================================
# Load State
# ============================================================

STATE = load_state()


# ============================================================
# Rate Limit Helpers
# ============================================================

def is_rate_limited():

    with RATE_LIMIT_LOCK:

        return (
            time.time()
            < rate_limit_until
        )


def get_remaining_cooldown():

    with RATE_LIMIT_LOCK:

        remaining = (
            rate_limit_until
            - time.time()
        )

    if remaining <= 0:

        return 0

    return max(
        1,
        int(
            remaining + 0.999
        ),
    )


def is_retry_delayed():

    with RATE_LIMIT_LOCK:

        return (
            time.time()
            < retry_not_before
        )


def get_remaining_retry_delay():

    with RATE_LIMIT_LOCK:

        remaining = (
            retry_not_before
            - time.time()
        )

    if remaining <= 0:

        return 0

    return max(
        1,
        int(
            remaining + 0.999
        ),
    )


# ============================================================
# Extract Wait Seconds
# ============================================================

def extract_wait_seconds(error):

    if not error:

        return None

    text = str(error)

    patterns = [
        r"wait\s+(\d+)\s+seconds",
        r"wait\s+(\d+)\s+second",
        r"try again in\s+(\d+)",
        r"in\s+(\d+)\s+seconds",
        r"in\s+(\d+)\s+second",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:

            try:

                return int(
                    match.group(1)
                )

            except (
                TypeError,
                ValueError,
            ):

                pass

    return None


# ============================================================
# Detect Authentication / Rate Limit Errors
# ============================================================

def classify_instagram_error(error):

    text = str(
        error or ""
    )

    lower = text.lower()

    # --------------------------------------------------------
    # 429
    # --------------------------------------------------------

    if (
        "429" in lower
        or "too many requests" in lower
        or "too many" in lower
        or "rate limit" in lower
        or "rate-limit" in lower
        or "rate limited" in lower
    ):

        return "RATE_LIMITED"

    # --------------------------------------------------------
    # 401
    # --------------------------------------------------------

    if (
        "401" in lower
        or "unauthorized" in lower
        or "login required" in lower
        or "login_required" in lower
        or "please log in" in lower
        or "please login" in lower
        or "not logged in" in lower
        or "session is invalid" in lower
        or "session invalid" in lower
        or "invalid session" in lower
    ):

        return "AUTH_401"

    # --------------------------------------------------------
    # Challenge / Checkpoint
    # --------------------------------------------------------

    if (
        "checkpoint" in lower
        or "challenge_required" in lower
        or "challenge required" in lower
        or "/challenge/" in lower
        or "challenge/" in lower
        or "suspicious login" in lower
        or "security code" in lower
        or "security checkpoint" in lower
        or "confirm your identity" in lower
        or "verify your identity" in lower
    ):

        return "CHECKPOINT"

    # --------------------------------------------------------
    # 403
    # --------------------------------------------------------

    if (
        "403" in lower
        or "forbidden" in lower
        or "access denied" in lower
    ):

        return "AUTH_403"

    return "OTHER"


# ============================================================
# Trigger Backoff
# ============================================================

def trigger_backoff(
    error=None,
    override_wait=None,
):

    global rate_limit_until
    global retry_not_before
    global backoff_seconds

    wait_time = override_wait

    if wait_time is None:

        wait_time = extract_wait_seconds(
            error
        )

    # --------------------------------------------------------
    # 如果 Instagram 沒提供等待時間
    # 使用目前 backoff + jitter
    # --------------------------------------------------------

    if wait_time is None:

        jitter = random.randint(
            BACKOFF_JITTER_MIN,
            BACKOFF_JITTER_MAX,
        )

        wait_time = (
            backoff_seconds
            + jitter
        )

    else:

        # 即使 Instagram 有提供等待時間
        # 仍加一點 jitter，避免固定時間重試

        jitter = random.randint(
            BACKOFF_JITTER_MIN,
            BACKOFF_JITTER_MAX,
        )

        wait_time += jitter

    try:

        wait_time = int(
            wait_time
        )

    except (
        TypeError,
        ValueError,
    ):

        wait_time = INITIAL_BACKOFF

    wait_time = max(
        1,
        wait_time,
    )

    wait_time = min(
        wait_time,
        MAX_BACKOFF,
    )

    # --------------------------------------------------------
    # Exponential Backoff
    #
    # 20m
    # 40m
    # 1h20m
    # 2h40m
    # 5h20m
    # max 6h
    # --------------------------------------------------------

    with RATE_LIMIT_LOCK:

        now = time.time()

        rate_limit_until = (
            now + wait_time
        )

        next_backoff = min(
            max(
                INITIAL_BACKOFF,
                backoff_seconds * 2,
            ),
            MAX_BACKOFF,
        )

        backoff_seconds = (
            next_backoff
        )

        # ----------------------------------------------------
        # Cooldown 結束後再多等一段時間
        # ----------------------------------------------------

        extra_delay = random.randint(
            COOLDOWN_RETRY_DELAY_MIN,
            COOLDOWN_RETRY_DELAY_MAX,
        )

        retry_not_before = (
            rate_limit_until
            + extra_delay
        )

        cooldown_until = (
            rate_limit_until
        )

        retry_time = (
            retry_not_before
        )

        current_backoff = (
            backoff_seconds
        )

    print(
        "",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )

    print(
        "🚨 INSTAGRAM 429 RATE LIMIT",
        flush=True,
    )

    print(
        "⏳ Cooldown："
        f"{format_seconds(wait_time)}",
        flush=True,
    )

    print(
        "🕐 Cooldown 結束："
        + time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(
                cooldown_until
            ),
        ),
        flush=True,
    )

    print(
        "🛡️ Cooldown 後額外等待："
        f"{format_seconds(extra_delay)}",
        flush=True,
    )

    print(
        "🔄 預計再次嘗試："
        + time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(
                retry_time
            ),
        ),
        flush=True,
    )

    print(
        "📈 下一次 Backoff："
        f"{format_seconds(current_backoff)}",
        flush=True,
    )

    print(
        "🛑 本輪立即停止。",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )

    save_state(STATE)


# ============================================================
# Reset Backoff
# ============================================================

def reset_backoff():

    global backoff_seconds

    with RATE_LIMIT_LOCK:

        backoff_seconds = (
            INITIAL_BACKOFF
        )


# ============================================================
# Instagram Session
# ============================================================

def load_instagram_session():

    print(
        "",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )

    print(
        "🔐 Instagram Session 初始化",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )

    if not IG_SESSION_FILE:

        print(
            "[IG] IG_SESSION_FILE 沒有設定。",
            flush=True,
        )

        print(
            "[IG] 使用匿名模式。",
            flush=True,
        )

        return False

    if not IG_SESSION_USERNAME:

        print(
            "[IG] IG_SESSION_USERNAME 沒有設定。",
            flush=True,
        )

        print(
            "[IG] 使用匿名模式。",
            flush=True,
        )

        return False

    print(
        f"🔐 Session Username："
        f"{IG_SESSION_USERNAME}",
        flush=True,
    )

    print(
        f"🔐 Session File："
        f"{IG_SESSION_FILE}",
        flush=True,
    )

    if not os.path.exists(
        IG_SESSION_FILE
    ):

        print(
            "❌ INSTAGRAM SESSION ERROR",
            flush=True,
        )

        print(
            "Session file 不存在："
            f"{IG_SESSION_FILE}",
            flush=True,
        )

        return False

    try:

        # ----------------------------------------------------
        # Read Secret
        # ----------------------------------------------------

        with open(
            IG_SESSION_FILE,
            "rb",
        ) as file:

            raw_data = file.read()

        print(
            "[IG] Session source size："
            f"{len(raw_data)} bytes",
            flush=True,
        )

        session_path = (
            IG_SESSION_FILE
        )

        # ----------------------------------------------------
        # Base64
        # ----------------------------------------------------

        if IG_SESSION_BASE64:

            print(
                "[IG] Base64 Session mode ENABLED。",
                flush=True,
            )

            try:

                encoded = re.sub(
                    rb"\s+",
                    b"",
                    raw_data,
                )

                decoded = base64.b64decode(
                    encoded,
                    validate=True,
                )

                if not decoded:

                    raise ValueError(
                        "Decoded session is empty"
                    )

                print(
                    "[IG] Base64 decoded size："
                    f"{len(decoded)} bytes",
                    flush=True,
                )

                temp_session = (
                    "/tmp/"
                    "instagram_render_session.session"
                )

                with open(
                    temp_session,
                    "wb",
                ) as file:

                    file.write(
                        decoded
                    )

                session_path = (
                    temp_session
                )

                print(
                    "[IG] Decoded session path："
                    f"{session_path}",
                    flush=True,
                )

            except (
                binascii.Error,
                ValueError,
            ) as error:

                print(
                    "❌ Base64 Session decode failed："
                    f"{error}",
                    flush=True,
                )

                return False

        # ----------------------------------------------------
        # Load Instaloader Session
        # ----------------------------------------------------

        print(
            "[IG] 正在載入 Instaloader Session...",
            flush=True,
        )

        L.load_session_from_file(
            IG_SESSION_USERNAME,
            session_path,
        )

        # ----------------------------------------------------
        # Verify Context Username
        # ----------------------------------------------------

        actual_username = getattr(
            L.context,
            "username",
            None,
        )

        if actual_username:

            print(
                "✅ INSTAGRAM SESSION LOADED",
                flush=True,
            )

            print(
                "🔐 Instagram 登入帳號："
                f"@{actual_username}",
                flush=True,
            )

        else:

            print(
                "✅ INSTAGRAM SESSION LOADED",
                flush=True,
            )

            print(
                "🔐 Instagram authenticated mode ENABLED。",
                flush=True,
            )

        print(
            "🔐 Instagram authenticated mode ENABLED。",
            flush=True,
        )

        return True

    except Exception as error:

        category = classify_instagram_error(
            error
        )

        print(
            "❌ INSTAGRAM SESSION ERROR",
            flush=True,
        )

        print(
            "Session 載入失敗："
            f"{error}",
            flush=True,
        )

        # ----------------------------------------------------
        # 如果 Session 本身已經被判定為認證異常
        # 直接 SAFE STOP。
        # ----------------------------------------------------

        if category in (
            "AUTH_401",
            "AUTH_403",
            "CHECKPOINT",
        ):

            trigger_auth_safe_stop(
                category,
                error,
            )

            return False

        print(
            "[IG] Session 載入失敗。",
            flush=True,
        )

        print(
            "[IG] 目前將使用匿名模式。",
            flush=True,
        )

        return False


# ============================================================
# Get Latest Post
# ============================================================

def get_latest_post(username):

    if is_auth_blocked():

        raise InstagramAuthenticationError(
            "Instagram authentication SAFE STOP active"
        )

    print(
        f"[IG] 取得 @{username} profile...",
        flush=True,
    )

    profile = (
        instaloader.Profile
        .from_username(
            L.context,
            username,
        )
    )

    print(
        f"[IG] @{username} profile 已取得。",
        flush=True,
    )

    posts = profile.get_posts()

    print(
        f"[IG] 取得 @{username} 最新貼文...",
        flush=True,
    )

    post = next(
        posts,
        None,
    )

    return post


# ============================================================
# Download Media
# ============================================================

def download_file(
    url,
    extension,
    prefix,
):

    temp_file = tempfile.NamedTemporaryFile(
        delete=False,
        prefix=prefix,
        suffix=extension,
    )

    temp_path = temp_file.name

    temp_file.close()

    try:

        print(
            "[MEDIA] 下載 "
            f"{extension}...",
            flush=True,
        )

        response = HTTP.get(
            url,
            stream=True,
            timeout=90,
        )

        response.raise_for_status()

        total_size = 0

        with open(
            temp_path,
            "wb",
        ) as file:

            for chunk in response.iter_content(
                chunk_size=128 * 1024
            ):

                if not chunk:

                    continue

                total_size += len(chunk)

                if (
                    total_size
                    > MAX_DISCORD_FILE_SIZE
                ):

                    print(
                        "[MEDIA] 檔案超過 Discord 安全上限，"
                        "跳過。",
                        flush=True,
                    )

                    try:

                        os.remove(
                            temp_path
                        )

                    except OSError:

                        pass

                    return None

                file.write(
                    chunk
                )

        print(
            "[MEDIA] 下載完成："
            f"{total_size / 1024 / 1024:.2f} MB",
            flush=True,
        )

        return temp_path

    except Exception as error:

        print(
            "[MEDIA] 下載失敗："
            f"{error}",
            flush=True,
        )

        try:

            os.remove(
                temp_path
            )

        except OSError:

            pass

        return None


# ============================================================
# Get Post Media
# ============================================================

def get_post_media(post):

    media = []

    try:

        # ----------------------------------------------------
        # Carousel
        # ----------------------------------------------------

        if post.typename == "GraphSidecar":

            print(
                "[MEDIA] Type: CAROUSEL",
                flush=True,
            )

            children = list(
                post.get_sidecar_nodes()
            )

            print(
                "[MEDIA] Carousel items: "
                f"{len(children)}",
                flush=True,
            )

            for index, child in enumerate(
                children[:MAX_MEDIA_ITEMS],
                start=1,
            ):

                if (
                    child.is_video
                    and child.video_url
                ):

                    media.append(
                        {
                            "type": "video",
                            "url": child.video_url,
                            "index": index,
                        }
                    )

                elif child.display_url:

                    media.append(
                        {
                            "type": "image",
                            "url": child.display_url,
                            "index": index,
                        }
                    )

        # ----------------------------------------------------
        # Video
        # ----------------------------------------------------

        elif (
            post.is_video
            and post.video_url
        ):

            print(
                "[MEDIA] Type: VIDEO",
                flush=True,
            )

            media.append(
                {
                    "type": "video",
                    "url": post.video_url,
                    "index": 1,
                }
            )

        # ----------------------------------------------------
        # Image
        # ----------------------------------------------------

        elif post.url:

            print(
                "[MEDIA] Type: IMAGE",
                flush=True,
            )

            media.append(
                {
                    "type": "image",
                    "url": post.url,
                    "index": 1,
                }
            )

    except Exception as error:

        print(
            "[MEDIA] 解析媒體失敗："
            f"{error}",
            flush=True,
        )

    return media[:MAX_MEDIA_ITEMS]


# ============================================================
# Discord Notification
# ============================================================

def send_discord_notification(
    username,
    post,
):

    if not WEBHOOK_URL:

        print(
            "[DISCORD] Webhook 未設定。",
            flush=True,
        )

        return False

    post_url = (
        "https://www.instagram.com/p/"
        f"{post.shortcode}/"
    )

    caption = (
        post.caption
        if post.caption
        else ""
    )

    if len(caption) > 1800:

        caption = (
            caption[:1800]
            + "\n\n..."
        )

    media = get_post_media(
        post
    )

    print(
        "[DISCORD] 準備傳送 "
        f"{len(media)} 個 media。",
        flush=True,
    )

    temp_files = []

    file_handles = []

    attachments = []

    try:

        embeds = [
            {
                "title":
                    f"📸 @{username} 發布了新貼文！",
                "url":
                    post_url,
                "description":
                    caption,
                "color":
                    15467852,
                "footer":
                    {
                        "text":
                            "Instagram → Discord"
                    },
            }
        ]

        for item in media:

            index = item["index"]

            media_type = item["type"]

            if media_type == "video":

                extension = ".mp4"

                filename = (
                    f"instagram_{index}.mp4"
                )

                mime_type = "video/mp4"

            else:

                extension = ".jpg"

                filename = (
                    f"instagram_{index}.jpg"
                )

                mime_type = "image/jpeg"

            print(
                "[DISCORD] 處理 media #"
                f"{index} ({media_type})",
                flush=True,
            )

            file_path = download_file(
                item["url"],
                extension,
                "ig_",
            )

            if not file_path:

                print(
                    "[DISCORD] 跳過 media #"
                    f"{index}",
                    flush=True,
                )

                continue

            temp_files.append(
                file_path
            )

            file_object = open(
                file_path,
                "rb",
            )

            file_handles.append(
                file_object
            )

            attachments.append(
                (
                    "files[]",
                    (
                        filename,
                        file_object,
                        mime_type,
                    ),
                )
            )

            if media_type == "image":

                embeds.append(
                    {
                        "url": post_url,
                        "image":
                            {
                                "url":
                                    "attachment://"
                                    f"{filename}"
                            },
                        "color":
                            15467852,
                    }
                )

            else:

                embeds.append(
                    {
                        "url": post_url,
                        "description":
                            f"🎬 影片內容 #{index}",
                        "color":
                            15467852,
                    }
                )

        embeds = embeds[:10]

        payload = {
            "username": "Instagram 通知",
            "embeds": embeds,
        }

        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
        )

        print(
            "[DISCORD] Uploading "
            f"{len(attachments)} files...",
            flush=True,
        )

        response = HTTP.post(
            WEBHOOK_URL,
            data={
                "payload_json":
                    payload_json,
            },
            files=attachments,
            timeout=180,
        )

        print(
            "[DISCORD] HTTP "
            f"{response.status_code}",
            flush=True,
        )

        if response.status_code in (
            200,
            204,
        ):

            print(
                "✅ [DISCORD] 推送成功。",
                flush=True,
            )

            return True

        if response.status_code == 429:

            print(
                "⚠️ [DISCORD] Discord Webhook 429。",
                flush=True,
            )

            print(
                response.text[:2000],
                flush=True,
            )

        else:

            print(
                "❌ [DISCORD] Webhook 失敗 "
                f"(HTTP {response.status_code})",
                flush=True,
            )

            print(
                response.text[:2000],
                flush=True,
            )

        return False

    except requests.exceptions.RequestException as error:

        print(
            "❌ [DISCORD] Request error："
            f"{error}",
            flush=True,
        )

        return False

    except Exception as error:

        print(
            "❌ [DISCORD] Unexpected error："
            f"{error}",
            flush=True,
        )

        return False

    finally:

        for file_object in file_handles:

            try:

                file_object.close()

            except Exception:

                pass

        for file_path in temp_files:

            try:

                os.remove(
                    file_path
                )

            except OSError:

                pass


# ============================================================
# Check Account
# ============================================================

def check_account(
    username,
    state,
):

    # --------------------------------------------------------
    # Authentication SAFE STOP
    # --------------------------------------------------------

    if is_auth_blocked():

        print(
            f"[{username}] "
            "Instagram authentication SAFE STOP 中。",
            flush=True,
        )

        return "AUTH_ERROR"

    # --------------------------------------------------------
    # 429 Global Cooldown
    # --------------------------------------------------------

    if is_rate_limited():

        print(
            f"[{username}] Instagram cooldown 中。",
            flush=True,
        )

        return "RATE_LIMITED"

    # --------------------------------------------------------
    # Post Cooldown Delay
    # --------------------------------------------------------

    if is_retry_delayed():

        remaining = (
            get_remaining_retry_delay()
        )

        print(
            f"[{username}] "
            "Cooldown 已結束，但還在安全等待期。",
            flush=True,
        )

        print(
            "⏳ 剩餘："
            f"{format_seconds(remaining)}",
            flush=True,
        )

        return "RATE_LIMITED"

    try:

        print(
            "",
            flush=True,
        )

        print(
            "=" * 60,
            flush=True,
        )

        print(
            f"🔍 檢查 @{username}",
            flush=True,
        )

        print(
            "=" * 60,
            flush=True,
        )

        # ----------------------------------------------------
        # Get latest post
        # ----------------------------------------------------

        post = get_latest_post(
            username
        )

        if not post:

            print(
                f"[{username}] 沒有找到貼文。",
                flush=True,
            )

            return "SUCCESS"

        shortcode = post.shortcode

        with STATE_LOCK:

            previous_shortcode = state.get(
                username
            )

        print(
            f"[{username}] Latest: "
            f"{shortcode}",
            flush=True,
        )

        # ----------------------------------------------------
        # First Run
        # ----------------------------------------------------

        if previous_shortcode is None:

            print(
                f"[{username}] FIRST RUN",
                flush=True,
            )

            print(
                f"[{username}] 記錄 "
                f"{shortcode}，不發 Discord。",
                flush=True,
            )

            with STATE_LOCK:

                state[username] = (
                    shortcode
                )

            save_state(
                state
            )

            return "SUCCESS"

        # ----------------------------------------------------
        # No New Post
        # ----------------------------------------------------

        if previous_shortcode == shortcode:

            print(
                f"[{username}] 沒有新貼文。",
                flush=True,
            )

            return "SUCCESS"

        # ----------------------------------------------------
        # New Post
        # ----------------------------------------------------

        print(
            "",
            flush=True,
        )

        print(
            "🚨 NEW POST "
            f"@{username}",
            flush=True,
        )

        print(
            f"New: {shortcode}",
            flush=True,
        )

        print(
            f"Old: {previous_shortcode}",
            flush=True,
        )

        # ----------------------------------------------------
        # Discord
        # ----------------------------------------------------

        success = send_discord_notification(
            username,
            post,
        )

        # ----------------------------------------------------
        # Only update state after Discord success
        # ----------------------------------------------------

        if success:

            with STATE_LOCK:

                state[username] = (
                    shortcode
                )

            if save_state(
                state
            ):

                print(
                    f"✅ [{username}] State updated。",
                    flush=True,
                )

            else:

                print(
                    f"⚠️ [{username}] Discord 已成功，"
                    "但 State 儲存失敗。",
                    flush=True,
                )

            return "SUCCESS"

        print(
            f"❌ [{username}] Discord 發送失敗。",
            flush=True,
        )

        print(
            f"⚠️ [{username}] State 不更新。",
            flush=True,
        )

        return "ERROR"

    # ========================================================
    # Our 429
    # ========================================================

    except InstagramRateLimited as error:

        print(
            "",
            flush=True,
        )

        print(
            f"🚨 [{username}] "
            "Instagram 429 被攔截。",
            flush=True,
        )

        print(
            f"[429] {error}",
            flush=True,
        )

        trigger_backoff(
            error,
            override_wait=error.wait_seconds,
        )

        return "RATE_LIMITED"

    # ========================================================
    # Native TooManyRequestsException
    # ========================================================

    except (
        instaloader.exceptions.TooManyRequestsException
    ) as error:

        print(
            "",
            flush=True,
        )

        print(
            f"🚨 [{username}] "
            "Instagram 429 Too Many Requests。",
            flush=True,
        )

        trigger_backoff(
            error
        )

        return "RATE_LIMITED"

    # ========================================================
    # Login Required
    # ========================================================

    except (
        instaloader.exceptions.LoginRequiredException
    ) as error:

        print(
            "",
            flush=True,
        )

        print(
            f"🔐 [{username}] "
            "Instagram Login Required。",
            flush=True,
        )

        print(
            f"[AUTH] {error}",
            flush=True,
        )

        trigger_auth_safe_stop(
            "LOGIN_REQUIRED",
            error,
        )

        return "AUTH_ERROR"

    # ========================================================
    # Profile Not Exists
    # ========================================================

    except (
        instaloader.exceptions.ProfileNotExistsException
    ):

        print(
            f"⚠️ [{username}] "
            "Profile 不存在。",
            flush=True,
        )

        return "ERROR"

    # ========================================================
    # Connection Exception
    #
    # 不再把所有 ConnectionException 當 429。
    # ========================================================

    except (
        instaloader.exceptions.ConnectionException
    ) as error:

        category = classify_instagram_error(
            error
        )

        error_text = str(
            error
        )

        if category == "RATE_LIMITED":

            print(
                f"🚨 [{username}] "
                "ConnectionException 中明確偵測到 "
                "Instagram Rate Limit。",
                flush=True,
            )

            print(
                f"[429] {error_text}",
                flush=True,
            )

            trigger_backoff(
                error
            )

            return "RATE_LIMITED"

        if category == "AUTH_401":

            print(
                f"🔐 [{username}] "
                "Instagram 401 / Login Required。",
                flush=True,
            )

            print(
                f"[AUTH] {error_text}",
                flush=True,
            )

            trigger_auth_safe_stop(
                "AUTH_401",
                error,
            )

            return "AUTH_ERROR"

        if category == "AUTH_403":

            print(
                f"🔐 [{username}] "
                "Instagram 403 Forbidden。",
                flush=True,
            )

            print(
                f"[AUTH] {error_text}",
                flush=True,
            )

            trigger_auth_safe_stop(
                "AUTH_403",
                error,
            )

            return "AUTH_ERROR"

        if category == "CHECKPOINT":

            print(
                f"🔐 [{username}] "
                "Instagram Checkpoint / Challenge。",
                flush=True,
            )

            print(
                f"[CHECKPOINT] {error_text}",
                flush=True,
            )

            trigger_auth_safe_stop(
                "CHECKPOINT_OR_CHALLENGE",
                error,
            )

            return "AUTH_ERROR"

        print(
            f"❌ [{username}] "
            "Instagram Connection Error。",
            flush=True,
        )

        print(
            f"[CONNECTION] {error_text}",
            flush=True,
        )

        print(
            "ℹ️ 這次不會啟動 429 Backoff。",
            flush=True,
        )

        return "ERROR"

    # ========================================================
    # Other Instaloader Exception
    # ========================================================

    except (
        instaloader.exceptions.InstaloaderException
    ) as error:

        category = classify_instagram_error(
            error
        )

        error_text = str(
            error
        )

        if category == "RATE_LIMITED":

            print(
                f"🚨 [{username}] "
                "Instaloader 回報 Rate Limit。",
                flush=True,
            )

            trigger_backoff(
                error
            )

            return "RATE_LIMITED"

        if category == "AUTH_401":

            print(
                f"🔐 [{username}] "
                "Instagram 401 / Login Required。",
                flush=True,
            )

            print(
                f"[AUTH] {error_text}",
                flush=True,
            )

            trigger_auth_safe_stop(
                "AUTH_401",
                error,
            )

            return "AUTH_ERROR"

        if category == "AUTH_403":

            print(
                f"🔐 [{username}] "
                "Instagram 403 Forbidden。",
                flush=True,
            )

            print(
                f"[AUTH] {error_text}",
                flush=True,
            )

            trigger_auth_safe_stop(
                "AUTH_403",
                error,
            )

            return "AUTH_ERROR"

        if category == "CHECKPOINT":

            print(
                f"🔐 [{username}] "
                "Instagram Checkpoint / Challenge。",
                flush=True,
            )

            print(
                f"[CHECKPOINT] {error_text}",
                flush=True,
            )

            trigger_auth_safe_stop(
                "CHECKPOINT_OR_CHALLENGE",
                error,
            )

            return "AUTH_ERROR"

        print(
            f"❌ [{username}] "
            "Instaloader Error。",
            flush=True,
        )

        print(
            f"[INSTALOADER] {error_text}",
            flush=True,
        )

        return "ERROR"

    # ========================================================
    # Authentication Error
    # ========================================================

    except InstagramAuthenticationError as error:

        print(
            f"🛑 [{username}] "
            "Instagram Authentication SAFE STOP。",
            flush=True,
        )

        trigger_auth_safe_stop(
            "INSTAGRAM_AUTHENTICATION_ERROR",
            error,
        )

        return "AUTH_ERROR"

    # ========================================================
    # Unexpected Error
    # ========================================================

    except Exception as error:

        category = classify_instagram_error(
            error
        )

        error_text = str(
            error
        )

        if category == "RATE_LIMITED":

            print(
                f"🚨 [{username}] "
                "Unexpected error 但明確疑似 Instagram 429。",
                flush=True,
            )

            trigger_backoff(
                error
            )

            return "RATE_LIMITED"

        if category == "AUTH_401":

            print(
                f"🔐 [{username}] "
                "Unexpected error 但疑似 401/Login Required。",
                flush=True,
            )

            print(
                f"[AUTH] {error_text}",
                flush=True,
            )

            trigger_auth_safe_stop(
                "AUTH_401",
                error,
            )

            return "AUTH_ERROR"

        if category == "AUTH_403":

            print(
                f"🔐 [{username}] "
                "Unexpected error 但疑似 403。",
                flush=True,
            )

            print(
                f"[AUTH] {error_text}",
                flush=True,
            )

            trigger_auth_safe_stop(
                "AUTH_403",
                error,
            )

            return "AUTH_ERROR"

        if category == "CHECKPOINT":

            print(
                f"🔐 [{username}] "
                "Unexpected error 但疑似 Checkpoint / Challenge。",
                flush=True,
            )

            print(
                f"[CHECKPOINT] {error_text}",
                flush=True,
            )

            trigger_auth_safe_stop(
                "CHECKPOINT_OR_CHALLENGE",
                error,
            )

            return "AUTH_ERROR"

        print(
            f"❌ [{username}] "
            "Unexpected error："
            f"{error_text}",
            flush=True,
        )

        return "ERROR"


# ============================================================
# Health Check
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        if self.path in (
            "/",
            "/health",
            "/healthz",
        ):

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )

            self.end_headers()

            # ------------------------------------------------
            # 即使 Instagram SAFE STOP
            # Health Check 仍回傳 200。
            # ------------------------------------------------

            if is_auth_blocked():

                message = (
                    "Instagram to Discord Bot is alive. "
                    "Instagram polling is stopped "
                    "because authentication requires "
                    "manual attention."
                )

            else:

                message = (
                    "Instagram to Discord Bot is alive."
                )

            self.wfile.write(
                message.encode(
                    "utf-8"
                )
            )

            return

        self.send_response(
            404
        )

        self.end_headers()

    def log_message(
        self,
        format,
        *args,
    ):

        return


# ============================================================
# Health Server
# ============================================================

def run_web_server():

    try:

        port = int(
            os.environ.get(
                "PORT",
                "10000",
            )
        )

        server = HTTPServer(
            (
                "0.0.0.0",
                port,
            ),
            HealthHandler,
        )

        print(
            f"🌐 Health server 啟動於 Port {port}",
            flush=True,
        )

        server.serve_forever()

    except Exception as error:

        print(
            "❌ Health Server error："
            f"{error}",
            flush=True,
        )


# ============================================================
# Graceful Shutdown
# ============================================================

def handle_shutdown(
    signum,
    frame,
):

    global shutdown_requested

    print(
        "",
        flush=True,
    )

    print(
        f"🛑 Received signal {signum}.",
        flush=True,
    )

    print(
        "🛑 Graceful shutdown requested.",
        flush=True,
    )

    shutdown_requested = True


# ============================================================
# Configuration
# ============================================================

def print_configuration():

    print(
        "",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )

    print(
        "Instagram → Discord Monitor",
        flush=True,
    )

    print(
        "Render Production Final Version",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )

    print(
        "📋 Accounts: "
        f"{len(USERNAMES)}",
        flush=True,
    )

    if USERNAMES:

        print(
            "👤 Accounts: "
            + ", ".join(USERNAMES),
            flush=True,
        )

    print(
        "⏱️ Check interval: "
        f"{CHECK_INTERVAL}s "
        f"({CHECK_INTERVAL / 3600:.1f}h)",
        flush=True,
    )

    print(
        "⏳ Account delay: "
        f"{ACCOUNT_DELAY}s "
        f"({ACCOUNT_DELAY / 60:.1f}m)",
        flush=True,
    )

    print(
        "🚀 Startup delay: "
        f"{STARTUP_DELAY_MIN}s - "
        f"{STARTUP_DELAY_MAX}s",
        flush=True,
    )

    print(
        "🛡️ Post-cooldown delay: "
        f"{COOLDOWN_RETRY_DELAY_MIN}s - "
        f"{COOLDOWN_RETRY_DELAY_MAX}s",
        flush=True,
    )

    print(
        "🚨 Initial backoff: "
        f"{INITIAL_BACKOFF}s",
        flush=True,
    )

    print(
        "🛑 Maximum backoff: "
        f"{MAX_BACKOFF}s",
        flush=True,
    )

    print(
        "🎲 Backoff jitter: "
        f"{BACKOFF_JITTER_MIN}s - "
        f"{BACKOFF_JITTER_MAX}s",
        flush=True,
    )

    print(
        "💾 State file: "
        f"{STATE_FILE}",
        flush=True,
    )

    print(
        "💾 Persistent cooldown: "
        f"{PERSIST_COOLDOWN}",
        flush=True,
    )

    print(
        "📦 Max file size: "
        f"{MAX_DISCORD_FILE_SIZE / 1000000:.2f} MB",
        flush=True,
    )

    print(
        "🛑 Authentication SAFE STOP: ENABLED",
        flush=True,
    )

    print(
        "🕐 429 cooldown log interval: "
        "300s maximum",
        flush=True,
    )

    if IG_SESSION_FILE:

        print(
            "🔐 Session Username: "
            f"{IG_SESSION_USERNAME or '(not set)'}",
            flush=True,
        )

        print(
            "🔐 Session File: "
            f"{IG_SESSION_FILE}",
            flush=True,
        )

        print(
            "🔐 Session Base64: "
            f"{IG_SESSION_BASE64}",
            flush=True,
        )

    else:

        print(
            "🔓 Instagram Session: DISABLED",
            flush=True,
        )

    print(
        "=" * 70,
        flush=True,
    )


# ============================================================
# Startup Delay
# ============================================================

def startup_delay():

    if STARTUP_DELAY_MAX < STARTUP_DELAY_MIN:

        print(
            "⚠️ Startup delay 設定錯誤，"
            "使用最小值。",
            flush=True,
        )

        delay = max(
            0,
            STARTUP_DELAY_MIN,
        )

    else:

        delay = random.randint(
            STARTUP_DELAY_MIN,
            STARTUP_DELAY_MAX,
        )

    if delay <= 0:

        print(
            "🚀 Startup delay disabled。",
            flush=True,
        )

        return

    print(
        "",
        flush=True,
    )

    print(
        "🛡️ Render startup protection。",
        flush=True,
    )

    print(
        "⏳ 啟動後等待："
        f"{format_seconds(delay)}",
        flush=True,
    )

    print(
        "ℹ️ 避免 Render restart 後立即碰 Instagram。",
        flush=True,
    )

    sleep_interruptible(
        delay
    )


# ============================================================
# Authentication Safe Stop Loop
# ============================================================

def authentication_safe_stop_loop():

    print(
        "",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )

    print(
        "🛑 Instagram Authentication SAFE STOP ACTIVE",
        flush=True,
    )

    print(
        "🔐 Instagram polling 已完全停止。",
        flush=True,
    )

    print(
        "🌐 Health Check Server 繼續運作。",
        flush=True,
    )

    print(
        "⚠️ 請人工處理 Instagram 帳號。",
        flush=True,
    )

    print(
        "🔄 Instagram 恢復正常後，"
        "重新啟動 Render Service。",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )

    # --------------------------------------------------------
    # 不再自動碰 Instagram。
    #
    # 每 5 分鐘只輸出一次狀態，
    # 避免 Render Log 爆量。
    # --------------------------------------------------------

    while (
        not shutdown_requested
        and is_auth_blocked()
    ):

        reason = (
            get_auth_block_reason()
        )

        print(
            "",
            flush=True,
        )

        print(
            "🛑 Instagram SAFE STOP 中。",
            flush=True,
        )

        print(
            "🔐 Reason："
            f"{reason}",
            flush=True,
        )

        print(
            "🌐 Health Check：仍然運作。",
            flush=True,
        )

        print(
            "⏳ 不會自動重新嘗試 Instagram。",
            flush=True,
        )

        sleep_interruptible(
            300
        )


# ============================================================
# Main
# ============================================================

def main():

    global shutdown_requested

    print(
        "",
        flush=True,
    )

    print(
        "🚀 main() started",
        flush=True,
    )

    # --------------------------------------------------------
    # Signals
    # --------------------------------------------------------

    signal.signal(
        signal.SIGTERM,
        handle_shutdown,
    )

    signal.signal(
        signal.SIGINT,
        handle_shutdown,
    )

    # --------------------------------------------------------
    # Configuration
    # --------------------------------------------------------

    print_configuration()

    # --------------------------------------------------------
    # Validate accounts
    # --------------------------------------------------------

    if not USERNAMES:

        print(
            "❌ ERROR: "
            "IG_USERNAME 沒有設定。",
            flush=True,
        )

        sys.exit(1)

    # --------------------------------------------------------
    # Validate Discord
    # --------------------------------------------------------

    if not WEBHOOK_URL:

        print(
            "❌ ERROR: "
            "Discord Webhook 沒有設定。",
            flush=True,
        )

        sys.exit(1)

    # --------------------------------------------------------
    # Load Session
    # --------------------------------------------------------

    session_loaded = (
        load_instagram_session()
    )

    if session_loaded:

        print(
            "✅ Instagram Session MODE ENABLED",
            flush=True,
        )

    else:

        # ----------------------------------------------------
        # 如果 Session 載入本身已經觸發 SAFE STOP
        # 不允許繼續進入 polling。
        # ----------------------------------------------------

        if is_auth_blocked():

            print(
                "🛑 Instagram Session "
                "Authentication SAFE STOP。",
                flush=True,
            )

        else:

            print(
                "⚠️ Instagram Session MODE DISABLED",
                flush=True,
            )

    # --------------------------------------------------------
    # Health Server
    # --------------------------------------------------------

    print(
        "BOOT: starting Health Check Server...",
        flush=True,
    )

    server_thread = threading.Thread(
        target=run_web_server,
        daemon=True,
    )

    server_thread.start()

    print(
        "✅ Health Check Server 已啟動。",
        flush=True,
    )

    print(
        "🚀 Instagram → Discord Bot READY。",
        flush=True,
    )

    # --------------------------------------------------------
    # 如果 Session 載入時就已經出現認證問題
    # 直接 SAFE STOP。
    # --------------------------------------------------------

    if is_auth_blocked():

        authentication_safe_stop_loop()

        print(
            "",
            flush=True,
        )

        print(
            "👋 Instagram → Discord Bot "
            "已安全退出。",
            flush=True,
        )

        return

    # --------------------------------------------------------
    # Startup Delay
    # --------------------------------------------------------

    startup_delay()

    if shutdown_requested:

        return

    # ========================================================
    # Main Loop
    # ========================================================

    while not shutdown_requested:

        # ----------------------------------------------------
        # Authentication SAFE STOP
        # ----------------------------------------------------

        if is_auth_blocked():

            authentication_safe_stop_loop()

            break

        # ----------------------------------------------------
        # Global cooldown
        #
        # IMPORTANT:
        #
        # 原本每 60 秒 wake up 一次。
        #
        # 現在改成最多 300 秒。
        #
        # 因此 Render Log 不會每分鐘刷一次。
        # ----------------------------------------------------

        if is_rate_limited():

            remaining = (
                get_remaining_cooldown()
            )

            print(
                "",
                flush=True,
            )

            print(
                "🛑 Instagram cooldown 中。",
                flush=True,
            )

            print(
                "⏳ 剩餘："
                f"{format_seconds(remaining)}",
                flush=True,
            )

            # ------------------------------------------------
            # 每 5 分鐘更新一次 Log
            # 收到 SIGTERM 可以快速退出
            # ------------------------------------------------

            sleep_interruptible(
                min(
                    max(
                        remaining,
                        1,
                    ),
                    300,
                )
            )

            continue

        # ----------------------------------------------------
        # Post cooldown retry delay
        # ----------------------------------------------------

        if is_retry_delayed():

            remaining = (
                get_remaining_retry_delay()
            )

            print(
                "",
                flush=True,
            )

            print(
                "🛡️ Instagram cooldown "
                "已結束。",
                flush=True,
            )

            print(
                "⏳ 安全等待期剩餘："
                f"{format_seconds(remaining)}",
                flush=True,
            )

            sleep_interruptible(
                min(
                    max(
                        remaining,
                        1,
                    ),
                    300,
                )
            )

            continue

        # ----------------------------------------------------
        # Cycle lock
        # ----------------------------------------------------

        if not CYCLE_LOCK.acquire(
            blocking=False
        ):

            sleep_interruptible(
                5
            )

            continue

        try:

            print(
                "",
                flush=True,
            )

            print(
                "=" * 70,
                flush=True,
            )

            print(
                "⏰ 開始 Instagram 檢查循環",
                flush=True,
            )

            print(
                time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                flush=True,
            )

            print(
                "📋 帳號數："
                f"{len(USERNAMES)}",
                flush=True,
            )

            print(
                "=" * 70,
                flush=True,
            )

            hit_rate_limit = False

            hit_auth_error = False

            # =================================================
            # Accounts
            # =================================================

            for index, username in enumerate(
                USERNAMES
            ):

                if shutdown_requested:

                    break

                # ------------------------------------------------
                # Authentication SAFE STOP
                # ------------------------------------------------

                if is_auth_blocked():

                    hit_auth_error = True

                    print(
                        "🛑 Instagram Authentication "
                        "SAFE STOP 已啟動。",
                        flush=True,
                    )

                    print(
                        "🛑 立即停止目前 cycle。",
                        flush=True,
                    )

                    break

                # ------------------------------------------------
                # Global rate limit
                # ------------------------------------------------

                if is_rate_limited():

                    hit_rate_limit = True

                    print(
                        "🛑 Instagram cooldown "
                        "已啟動。",
                        flush=True,
                    )

                    print(
                        "🛑 立即停止目前 cycle。",
                        flush=True,
                    )

                    break

                # ------------------------------------------------
                # Retry delay
                # ------------------------------------------------

                if is_retry_delayed():

                    hit_rate_limit = True

                    print(
                        "🛡️ Cooldown 後安全等待期 "
                        "已啟動。",
                        flush=True,
                    )

                    print(
                        "🛑 停止目前 cycle。",
                        flush=True,
                    )

                    break

                # ------------------------------------------------
                # Check account
                # ------------------------------------------------

                result = check_account(
                    username,
                    STATE,
                )

                # ------------------------------------------------
                # Authentication error
                # ------------------------------------------------

                if result == "AUTH_ERROR":

                    hit_auth_error = True

                    print(
                        "",
                        flush=True,
                    )

                    print(
                        "🛑 Instagram "
                        "Authentication 問題。",
                        flush=True,
                    )

                    print(
                        "🛑 不再檢查後面的帳號。",
                        flush=True,
                    )

                    print(
                        "🛑 本輪立即停止。",
                        flush=True,
                    )

                    break

                # ------------------------------------------------
                # Rate limit
                # ------------------------------------------------

                if result == "RATE_LIMITED":

                    hit_rate_limit = True

                    print(
                        "",
                        flush=True,
                    )

                    print(
                        "🚨 Instagram rate limit。",
                        flush=True,
                    )

                    print(
                        "🛑 停止目前 cycle。",
                        flush=True,
                    )

                    print(
                        "🛑 後面的帳號本輪不再檢查。",
                        flush=True,
                    )

                    break

                # ------------------------------------------------
                # Other error
                # ------------------------------------------------

                if result == "ERROR":

                    print(
                        f"⚠️ @{username} "
                        "本次檢查發生錯誤，"
                        "繼續下一個帳號。",
                        flush=True,
                    )

                # ------------------------------------------------
                # Account delay
                # ------------------------------------------------

                if (
                    index
                    < len(USERNAMES) - 1
                    and not shutdown_requested
                ):

                    # ------------------------------------------------
                    # 如果中途出現 SAFE STOP
                    # 不要再等待後繼續碰 Instagram。
                    # ------------------------------------------------

                    if is_auth_blocked():

                        hit_auth_error = True

                        print(
                            "🛑 Authentication SAFE STOP "
                            "已啟動。",
                            flush=True,
                        )

                        break

                    if is_rate_limited():

                        hit_rate_limit = True

                        break

                    print(
                        "",
                        flush=True,
                    )

                    print(
                        "⏳ 下一個帳號前等待 "
                        f"{ACCOUNT_DELAY}s "
                        f"({ACCOUNT_DELAY / 60:.1f} 分鐘)...",
                        flush=True,
                    )

                    sleep_interruptible(
                        ACCOUNT_DELAY
                    )

            # =================================================
            # Authentication SAFE STOP
            # =================================================

            if (
                hit_auth_error
                or is_auth_blocked()
            ):

                print(
                    "",
                    flush=True,
                )

                print(
                    "=" * 70,
                    flush=True,
                )

                print(
                    "🛑 本輪因 Instagram "
                    "Authentication / Challenge "
                    "停止。",
                    flush=True,
                )

                print(
                    "🛑 Instagram polling 已停止。",
                    flush=True,
                )

                print(
                    "🌐 Health Check 繼續運作。",
                    flush=True,
                )

                print(
                    "🔐 Reason："
                    f"{get_auth_block_reason()}",
                    flush=True,
                )

                print(
                    "=" * 70,
                    flush=True,
                )

                # ------------------------------------------------
                # 進入永久 SAFE STOP
                # ------------------------------------------------

                authentication_safe_stop_loop()

                break

            # =================================================
            # Rate Limited
            # =================================================

            if hit_rate_limit:

                print(
                    "",
                    flush=True,
                )

                print(
                    "=" * 70,
                    flush=True,
                )

                print(
                    "🚨 本輪因 Instagram "
                    "rate limit 停止。",
                    flush=True,
                )

                print(
                    "⏳ 等待 global cooldown "
                    "以及後續安全等待。",
                    flush=True,
                )

                print(
                    "=" * 70,
                    flush=True,
                )

                continue

            # =================================================
            # Cycle complete
            # =================================================

            if not shutdown_requested:

                reset_backoff()

                print(
                    "",
                    flush=True,
                )

                print(
                    "=" * 70,
                    flush=True,
                )

                print(
                    "✅ 本輪完成。",
                    flush=True,
                )

                print(
                    "📋 已處理 "
                    f"{len(USERNAMES)} 個帳號。",
                    flush=True,
                )

                print(
                    "😴 下一輪等待 "
                    f"{CHECK_INTERVAL}s "
                    f"({CHECK_INTERVAL / 3600:.1f} 小時)。",
                    flush=True,
                )

                print(
                    "=" * 70,
                    flush=True,
                )

                sleep_interruptible(
                    CHECK_INTERVAL
                )

        finally:

            CYCLE_LOCK.release()

    # ========================================================
    # Shutdown
    # ========================================================

    print(
        "",
        flush=True,
    )

    print(
        "👋 Instagram → Discord Bot "
        "已安全退出。",
        flush=True,
    )


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":

    print(
        "BOOT: entering main()",
        flush=True,
    )

    main()
