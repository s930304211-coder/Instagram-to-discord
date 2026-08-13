#!/usr/bin/env python3

# ============================================================
# Instagram → Discord Monitor
#
# Render Production Final Version
#
# Features:
#   - Multiple Instagram accounts
#   - Base64 Instagram Session support
#   - Instagram Session verification
#   - NO anonymous fallback
#   - 429 automatic global cooldown
#   - Exponential backoff
#   - Random jitter
#   - Persistent cooldown
#   - Persistent post state
#   - Discord Webhook
#   - Image / Video / Carousel
#   - Render Health Check
#   - Atomic state write
#   - Graceful shutdown
#   - Diagnostic startup logging
# ============================================================


# ============================================================
# BOOT
# ============================================================

print("BOOT: Python process started", flush=True)


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


print("BOOT: standard libraries imported", flush=True)


# ============================================================
# Third Party
# ============================================================

print("BOOT: importing instaloader...", flush=True)

import instaloader

print(
    "BOOT: instaloader imported "
    f"version={getattr(instaloader, '__version__', 'unknown')}",
    flush=True,
)


print("BOOT: importing requests...", flush=True)

import requests

print(
    "BOOT: requests imported "
    f"version={getattr(requests, '__version__', 'unknown')}",
    flush=True,
)


# ============================================================
# Environment
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
    ),
)

ACCOUNT_DELAY = int(
    os.environ.get(
        "ACCOUNT_DELAY",
        "120",
    ),
)


# ============================================================
# Instagram Rate Limit
# ============================================================

INITIAL_BACKOFF = int(
    os.environ.get(
        "INITIAL_BACKOFF",
        "1200",
    ),
)

MAX_BACKOFF = int(
    os.environ.get(
        "MAX_BACKOFF",
        "21600",
    ),
)

BACKOFF_JITTER_MIN = int(
    os.environ.get(
        "BACKOFF_JITTER_MIN",
        "30",
    ),
)

BACKOFF_JITTER_MAX = int(
    os.environ.get(
        "BACKOFF_JITTER_MAX",
        "120",
    ),
)


# ============================================================
# State
# ============================================================

STATE_FILE = os.environ.get(
    "STATE_FILE",
    "last_posts.json",
)

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
# Secret File:
#
# aderiii_1225.session.b64
#
# Contents:
# Base64 encoded Instaloader .session file
# ============================================================

IG_SESSION_USERNAME = os.environ.get(
    "IG_SESSION_USERNAME",
    "",
).strip()

IG_SESSION_FILE = os.environ.get(
    "IG_SESSION_FILE",
    "",
).strip()


# ============================================================
# Discord File Limit
# ============================================================

MAX_DISCORD_FILE_SIZE = int(
    os.environ.get(
        "MAX_DISCORD_FILE_SIZE",
        "9500000",
    ),
)

MAX_MEDIA_ITEMS = 9


# ============================================================
# Global Runtime State
# ============================================================

shutdown_requested = False

rate_limit_until = 0.0

backoff_seconds = INITIAL_BACKOFF


# ============================================================
# Locks
# ============================================================

STATE_LOCK = threading.Lock()

RATE_LIMIT_LOCK = threading.Lock()

CYCLE_LOCK = threading.Lock()


# ============================================================
# Custom 429 Exception
# ============================================================

class InstagramRateLimited(Exception):

    def __init__(
        self,
        message,
        wait_seconds=None,
    ):

        super().__init__(message)

        self.wait_seconds = wait_seconds


# ============================================================
# Custom Rate Controller
# ============================================================

class AbortOn429RateController(
    instaloader.RateController
):
    """
    Immediately stop when Instagram returns HTTP 429.
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
# HTTP Session
# ============================================================

HTTP = requests.Session()

HTTP.headers.update({

    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/151.0 Safari/537.36"
    ),

    "Accept-Language":
        "en-US,en;q=0.9",

})


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

    rate_controller=(
        lambda context:
        AbortOn429RateController(context)
    ),

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
# State Load
# ============================================================

def load_state():

    global rate_limit_until
    global backoff_seconds

    print(
        f"[STATE] Loading: {STATE_FILE}",
        flush=True,
    )

    if not os.path.exists(STATE_FILE):

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

        # ----------------------------------------------------
        # New format
        # ----------------------------------------------------

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

                    saved_backoff = (
                        INITIAL_BACKOFF
                    )

                saved_backoff = min(
                    max(
                        saved_backoff,
                        INITIAL_BACKOFF,
                    ),
                    MAX_BACKOFF,
                )

                if saved_until > time.time():

                    with RATE_LIMIT_LOCK:

                        rate_limit_until = (
                            saved_until
                        )

                        backoff_seconds = (
                            saved_backoff
                        )

                    remaining = (
                        saved_until
                        - time.time()
                    )

                    print(
                        "[STATE] 恢復 Instagram cooldown。",
                        flush=True,
                    )

                    print(
                        "[STATE] 剩餘："
                        f"{format_seconds(remaining)}",
                        flush=True,
                    )

            posts_state = raw_data.get(
                "posts",
                {},
            )

        else:

            # ------------------------------------------------
            # Backward compatibility
            # ------------------------------------------------

            posts_state = raw_data

        if not isinstance(
            posts_state,
            dict,
        ):

            posts_state = {}

        print(
            "[STATE] 已載入 "
            f"{len(posts_state)} "
            "個帳號紀錄。",
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
            STATE_FILE,
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

                "backoff_seconds": (
                    current_backoff
                    if PERSIST_COOLDOWN
                    else INITIAL_BACKOFF
                ),

                "updated_at":
                    time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                    ),

            },

            "posts":
                dict(posts_state),

        }

        # ----------------------------------------------------
        # Atomic write
        # ----------------------------------------------------

        temp_path = state_path.with_name(
            state_path.name
            + ".tmp"
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
                file.fileno(),
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


# ============================================================
# Extract Wait Time
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
                    match.group(1),
                )

            except (
                TypeError,
                ValueError,
            ):

                pass

    return None


# ============================================================
# Trigger Backoff
# ============================================================

def trigger_backoff(
    error=None,
    override_wait=None,
):

    global rate_limit_until
    global backoff_seconds

    wait_time = override_wait

    if wait_time is None:

        wait_time = (
            extract_wait_seconds(
                error,
            )
        )

    if wait_time is None:

        jitter = random.randint(
            BACKOFF_JITTER_MIN,
            BACKOFF_JITTER_MAX,
        )

        wait_time = (
            backoff_seconds
            + jitter
        )

    try:

        wait_time = int(
            wait_time,
        )

    except (
        TypeError,
        ValueError,
    ):

        wait_time = (
            INITIAL_BACKOFF
        )

    wait_time = max(
        1,
        wait_time,
    )

    wait_time = min(
        wait_time,
        MAX_BACKOFF,
    )

    with RATE_LIMIT_LOCK:

        rate_limit_until = (
            time.time()
            + wait_time
        )

        backoff_seconds = min(

            max(
                INITIAL_BACKOFF,
                backoff_seconds * 2,
            ),

            MAX_BACKOFF,

        )

        cooldown_until = (
            rate_limit_until
        )

        current_backoff = (
            backoff_seconds
        )

    print("", flush=True)

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
        "🕐 預計恢復："
        + time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(
                cooldown_until,
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
        "🛑 立即停止目前 cycle。",
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

    print("", flush=True)

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

    # --------------------------------------------------------
    # Validate configuration
    # --------------------------------------------------------

    if not IG_SESSION_USERNAME:

        print(
            "❌ IG_SESSION_USERNAME 沒有設定。",
            flush=True,
        )

        return False

    if not IG_SESSION_FILE:

        print(
            "❌ IG_SESSION_FILE 沒有設定。",
            flush=True,
        )

        return False

    # --------------------------------------------------------
    # Check file
    # --------------------------------------------------------

    if not os.path.exists(
        IG_SESSION_FILE,
    ):

        print(
            "❌ INSTAGRAM SESSION ERROR",
            flush=True,
        )

        print(
            f"Session file 不存在："
            f"{IG_SESSION_FILE}",
            flush=True,
        )

        return False

    temp_session_path = None

    try:

        print(
            "🔐 正在讀取 Base64 Session...",
            flush=True,
        )

        with open(
            IG_SESSION_FILE,
            "r",
            encoding="utf-8",
        ) as file:

            encoded_session = file.read()

        encoded_session = (
            encoded_session.strip()
        )

        if not encoded_session:

            print(
                "❌ Session Base64 檔案是空的。",
                flush=True,
            )

            return False

        print(
            "🔐 Base64 長度："
            f"{len(encoded_session)}",
            flush=True,
        )

        # ----------------------------------------------------
        # Decode Base64
        # ----------------------------------------------------

        try:

            session_bytes = (
                base64.b64decode(
                    encoded_session,
                    validate=False,
                )
            )

        except (
            ValueError,
            binascii.Error,
        ) as error:

            print(
                "❌ Base64 decode 失敗："
                f"{error}",
                flush=True,
            )

            return False

        if not session_bytes:

            print(
                "❌ Base64 decode 後沒有資料。",
                flush=True,
            )

            return False

        print(
            "🔐 Decode 後 Session 大小："
            f"{len(session_bytes)} bytes",
            flush=True,
        )

        # ----------------------------------------------------
        # Create temporary binary session
        # ----------------------------------------------------

        temp_session = tempfile.NamedTemporaryFile(
            prefix="instagram_session_",
            suffix=".session",
            delete=False,
        )

        temp_session_path = (
            temp_session.name
        )

        try:

            temp_session.write(
                session_bytes
            )

            temp_session.flush()

            os.fsync(
                temp_session.fileno()
            )

        finally:

            temp_session.close()

        print(
            "🔐 Session 已還原至："
            f"{temp_session_path}",
            flush=True,
        )

        # ----------------------------------------------------
        # Load session
        # ----------------------------------------------------

        print(
            "🔐 正在載入 Instaloader Session...",
            flush=True,
        )

        L.load_session_from_file(
            IG_SESSION_USERNAME,
            temp_session_path,
        )

        print(
            "✅ Instaloader Session 載入成功。",
            flush=True,
        )

        # ----------------------------------------------------
        # Test login
        # ----------------------------------------------------

        print(
            "🔐 正在驗證 Instagram Login...",
            flush=True,
        )

        logged_in_username = (
            L.test_login()
        )

        if not logged_in_username:

            print(
                "❌ Session 載入成功，"
                "但 Instagram Login 驗證失敗。",
                flush=True,
            )

            return False

        print(
            "✅ Instagram Login 驗證成功。",
            flush=True,
        )

        print(
            f"👤 Logged in as: "
            f"{logged_in_username}",
            flush=True,
        )

        if (
            logged_in_username.lower()
            != IG_SESSION_USERNAME.lower()
        ):

            print(
                "⚠️ Session 登入帳號與 "
                "IG_SESSION_USERNAME 不一致。",
                flush=True,
            )

        return True

    except Exception as error:

        print(
            "❌ INSTAGRAM SESSION ERROR",
            flush=True,
        )

        print(
            f"Session 載入失敗：{error}",
            flush=True,
        )

        return False

    finally:

        if temp_session_path:

            try:

                if os.path.exists(
                    temp_session_path
                ):

                    os.remove(
                        temp_session_path
                    )

                    print(
                        "🔐 暫存 Session 已刪除。",
                        flush=True,
                    )

            except Exception as error:

                print(
                    "⚠️ 暫存 Session 清理失敗："
                    f"{error}",
                    flush=True,
                )


# ============================================================
# Get Latest Post
# ============================================================

def get_latest_post(username):

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

    print(
        f"[IG] 取得 @{username} 最新貼文...",
        flush=True,
    )

    posts = profile.get_posts()

    post = next(
        posts,
        None,
    )

    return post


# ============================================================
# Download File
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
                chunk_size=128 * 1024,
            ):

                if not chunk:

                    continue

                total_size += len(
                    chunk
                )

                if (
                    total_size
                    > MAX_DISCORD_FILE_SIZE
                ):

                    print(
                        "[MEDIA] 檔案超過 Discord "
                        "安全上限，跳過。",
                        flush=True,
                    )

                    try:

                        os.remove(
                            temp_path
                        )

                    except OSError:

                        pass

                    return None

                file.write(chunk)

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
                temp_path,
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

        if (
            post.typename
            == "GraphSidecar"
        ):

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

                    media.append({

                        "type":
                            "video",

                        "url":
                            child.video_url,

                        "index":
                            index,

                    })

                elif child.display_url:

                    media.append({

                        "type":
                            "image",

                        "url":
                            child.display_url,

                        "index":
                            index,

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

            media.append({

                "type":
                    "video",

                "url":
                    post.video_url,

                "index":
                    1,

            })

        # ----------------------------------------------------
        # Image
        # ----------------------------------------------------

        elif post.url:

            print(
                "[MEDIA] Type: IMAGE",
                flush=True,
            )

            media.append({

                "type":
                    "image",

                "url":
                    post.url,

                "index":
                    1,

            })

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

        embeds = []

        embeds.append({

            "title":
                f"📸 @{username} 發布了新貼文！",

            "url":
                post_url,

            "description":
                caption,

            "color":
                15467852,

            "footer": {
                "text":
                    "Instagram → Discord",
            },

        })

        # ----------------------------------------------------
        # Download media
        # ----------------------------------------------------

        for item in media:

            index = item["index"]

            media_type = item["type"]

            if media_type == "video":

                extension = ".mp4"

                filename = (
                    f"instagram_{index}.mp4"
                )

                mime_type = (
                    "video/mp4"
                )

            else:

                extension = ".jpg"

                filename = (
                    f"instagram_{index}.jpg"
                )

                mime_type = (
                    "image/jpeg"
                )

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

                embeds.append({

                    "url":
                        post_url,

                    "image": {

                        "url":
                            "attachment://"
                            f"{filename}",

                    },

                    "color":
                        15467852,

                })

            else:

                embeds.append({

                    "url":
                        post_url,

                    "description":
                        f"🎬 影片內容 #{index}",

                    "color":
                        15467852,

                })

        # Discord max 10 embeds

        embeds = embeds[:10]

        payload = {

            "username":
                "Instagram 通知",

            "embeds":
                embeds,

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
                "⚠️ [DISCORD] Webhook 遇到 429。",
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
                    file_path,
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

    if is_rate_limited():

        print(
            f"[{username}] Instagram cooldown 中。",
            flush=True,
        )

        return "RATE_LIMITED"

    try:

        print("", flush=True)

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
        # Latest post
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

            previous_shortcode = (
                state.get(
                    username
                )
            )

        print(
            f"[{username}] Latest: "
            f"{shortcode}",
            flush=True,
        )

        # ----------------------------------------------------
        # First run
        # ----------------------------------------------------

        if previous_shortcode is None:

            print(
                f"[{username}] FIRST RUN",
                flush=True,
            )

            print(
                f"[{username}] 記錄 "
                f"{shortcode}，"
                "不發 Discord。",
                flush=True,
            )

            with STATE_LOCK:

                state[
                    username
                ] = shortcode

            save_state(
                state
            )

            return "SUCCESS"

        # ----------------------------------------------------
        # No new post
        # ----------------------------------------------------

        if (
            previous_shortcode
            == shortcode
        ):

            print(
                f"[{username}] 沒有新貼文。",
                flush=True,
            )

            return "SUCCESS"

        # ----------------------------------------------------
        # New post
        # ----------------------------------------------------

        print("", flush=True)

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
        # Update state only after Discord success
        # ----------------------------------------------------

        if success:

            with STATE_LOCK:

                state[
                    username
                ] = shortcode

            if save_state(
                state
            ):

                print(
                    f"✅ [{username}] "
                    "State updated。",
                    flush=True,
                )

            else:

                print(
                    f"⚠️ [{username}] "
                    "Discord 已成功，"
                    "但 State 儲存失敗。",
                    flush=True,
                )

            return "SUCCESS"

        # ----------------------------------------------------
        # Discord failed
        # ----------------------------------------------------

        print(
            f"❌ [{username}] "
            "Discord 發送失敗。",
            flush=True,
        )

        print(
            f"⚠️ [{username}] "
            "State 不更新。",
            flush=True,
        )

        return "ERROR"

    # ========================================================
    # Custom 429
    # ========================================================

    except InstagramRateLimited as error:

        print("", flush=True)

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
            override_wait=(
                error.wait_seconds
            ),
        )

        return "RATE_LIMITED"

    # ========================================================
    # Native 429
    # ========================================================

    except (
        instaloader
        .exceptions
        .TooManyRequestsException
    ) as error:

        print("", flush=True)

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
    # Connection
    # ========================================================

    except (
        instaloader
        .exceptions
        .ConnectionException
    ) as error:

        error_text = str(error)

        lower_text = (
            error_text.lower()
        )

        if (

            "429"
            in error_text

            or "too many"
            in lower_text

            or "rate limit"
            in lower_text

            or "rate-limit"
            in lower_text

            or "rate limited"
            in lower_text

        ):

            print(
                f"🚨 [{username}] "
                "偵測到 Instagram Rate Limit。",
                flush=True,
            )

            trigger_backoff(
                error
            )

            return "RATE_LIMITED"

        print(
            f"❌ [{username}] "
            "Instagram connection error："
            f"{error_text}",
            flush=True,
        )

        return "ERROR"

    # ========================================================
    # Profile Not Exists
    # ========================================================

    except (
        instaloader
        .exceptions
        .ProfileNotExistsException
    ):

        print(
            f"⚠️ [{username}] "
            "Profile 不存在。",
            flush=True,
        )

        return "ERROR"

    # ========================================================
    # Other Instaloader error
    # ========================================================

    except (
        instaloader
        .exceptions
        .InstaloaderException
    ) as error:

        error_text = str(error)

        lower_text = (
            error_text.lower()
        )

        if (

            "429"
            in error_text

            or "too many"
            in lower_text

            or "rate limit"
            in lower_text

            or "rate-limit"
            in lower_text

            or "rate limited"
            in lower_text

        ):

            print(
                f"🚨 [{username}] "
                "Instaloader 回報 Rate Limit。",
                flush=True,
            )

            trigger_backoff(
                error
            )

            return "RATE_LIMITED"

        print(
            f"❌ [{username}] "
            "Instaloader error："
            f"{error_text}",
            flush=True,
        )

        return "ERROR"

    # ========================================================
    # Unexpected
    # ========================================================

    except Exception as error:

        error_text = str(error)

        lower_text = (
            error_text.lower()
        )

        if (

            "429"
            in error_text

            or "too many requests"
            in lower_text

            or "rate limit"
            in lower_text

            or "rate-limit"
            in lower_text

        ):

            print(
                f"🚨 [{username}] "
                "Unexpected error 但疑似 429。",
                flush=True,
            )

            trigger_backoff(
                error
            )

            return "RATE_LIMITED"

        print(
            f"❌ [{username}] "
            "Unexpected error："
            f"{error}",
            flush=True,
        )

        return "ERROR"


# ============================================================
# Health Check
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(
        self
    ):

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
                "text/plain; "
                "charset=utf-8",
            )

            self.end_headers()

            self.wfile.write(
                b"Instagram to Discord Bot is alive."
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
# Web Server
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
            "",
            flush=True,
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
# Shutdown
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
        f"🔐 Session Username: "
        f"{IG_SESSION_USERNAME}",
        flush=True,
    )

    print(
        f"🔐 Session File: "
        f"{IG_SESSION_FILE}",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
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
        "BOOT: entering main()",
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
            "❌ ERROR: IG_USERNAME 沒有設定。",
            flush=True,
        )

        sys.exit(1)

    # --------------------------------------------------------
    # Validate Discord
    # --------------------------------------------------------

    if not WEBHOOK_URL:

        print(
            "❌ ERROR: Discord Webhook 沒有設定。",
            flush=True,
        )

        sys.exit(1)

    # --------------------------------------------------------
    # Validate Session configuration
    # --------------------------------------------------------

    if not IG_SESSION_USERNAME:

        print(
            "❌ ERROR: IG_SESSION_USERNAME "
            "沒有設定。",
            flush=True,
        )

        sys.exit(1)

    if not IG_SESSION_FILE:

        print(
            "❌ ERROR: IG_SESSION_FILE "
            "沒有設定。",
            flush=True,
        )

        sys.exit(1)

    # --------------------------------------------------------
    # Load Instagram Session
    # --------------------------------------------------------

    session_loaded = (
        load_instagram_session()
    )

    if not session_loaded:

        print(
            "",
            flush=True,
        )

        print(
            "=" * 70,
            flush=True,
        )

        print(
            "❌ Instagram Session 無法使用。",
            flush=True,
        )

        print(
            "❌ 不會退回匿名模式。",
            flush=True,
        )

        print(
            "❌ 為避免匿名模式造成 Instagram 429，",
            flush=True,
        )

        print(
            "❌ 程式停止。",
            flush=True,
        )

        print(
            "=" * 70,
            flush=True,
        )

        sys.exit(1)

    # --------------------------------------------------------
    # Start health server
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

    # ========================================================
    # Main Loop
    # ========================================================

    while not shutdown_requested:

        # ----------------------------------------------------
        # Global cooldown
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

            sleep_interruptible(
                min(
                    max(
                        remaining,
                        1,
                    ),
                    60,
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

            # ------------------------------------------------
            # Accounts
            # ------------------------------------------------

            for index, username in enumerate(
                USERNAMES
            ):

                if shutdown_requested:

                    break

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

                result = check_account(

                    username,

                    STATE,

                )

                # --------------------------------------------
                # Rate limited
                # --------------------------------------------

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

                # --------------------------------------------
                # Error
                # --------------------------------------------

                if result == "ERROR":

                    print(
                        f"⚠️ @{username} "
                        "本次檢查發生錯誤，"
                        "繼續下一個帳號。",
                        flush=True,
                    )

                # --------------------------------------------
                # Account delay
                # --------------------------------------------

                if (
                    index
                    < len(USERNAMES) - 1
                    and not shutdown_requested
                ):

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

            # ------------------------------------------------
            # Rate limit cycle
            # ------------------------------------------------

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
                    "🚨 本輪因 Instagram 429 停止。",
                    flush=True,
                )

                print(
                    "⏳ 等待 global cooldown。",
                    flush=True,
                )

                print(
                    "=" * 70,
                    flush=True,
                )

                continue

            # ------------------------------------------------
            # Cycle complete
            # ------------------------------------------------

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
                    f"{len(USERNAMES)} "
                    "個帳號。",
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
        "stopped safely.",
        flush=True,
    )


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":

    main()
