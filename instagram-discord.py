#!/usr/bin/env python3

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

import instaloader
import requests


# ============================================================
# Instagram → Discord Monitor
#
# Render
# Multiple Instagram Accounts
# Global 429 Cooldown
# Persistent State
# Discord Webhook
# Graceful Shutdown
# ============================================================


# ============================================================
# Environment Variables
# ============================================================

RAW_USERNAMES = os.environ.get("IG_USERNAME", "")

USERNAMES = [
    username.strip()
    for username in RAW_USERNAMES.split(",")
    if username.strip()
]


WEBHOOK_URL = os.environ.get(
    "INSTAGRAM_POST_WEBHOOK",
    os.environ.get("WEBHOOK_URL", "")
)


# ============================================================
# Polling
# ============================================================

# 每輪完整檢查之間等待多久
# 預設 2 小時
CHECK_INTERVAL = int(
    os.environ.get("CHECK_INTERVAL", "7200")
)


# 帳號與帳號之間等待多久
# 預設 3 分鐘
ACCOUNT_DELAY = int(
    os.environ.get("ACCOUNT_DELAY", "180")
)


# ============================================================
# 429 Backoff
# ============================================================

# 第一次 429
# 預設 20 分鐘
INITIAL_BACKOFF = int(
    os.environ.get("INITIAL_BACKOFF", "1200")
)


# 最大 cooldown
# 預設 6 小時
MAX_BACKOFF = int(
    os.environ.get("MAX_BACKOFF", "21600")
)


# 隨機 jitter
BACKOFF_JITTER_MIN = int(
    os.environ.get("BACKOFF_JITTER_MIN", "30")
)

BACKOFF_JITTER_MAX = int(
    os.environ.get("BACKOFF_JITTER_MAX", "120")
)


# ============================================================
# Persistent State
# ============================================================

STATE_FILE = os.environ.get(
    "STATE_FILE",
    "last_posts.json"
)


# Render restart 後是否保留 cooldown
PERSIST_COOLDOWN = (
    os.environ.get(
        "PERSIST_COOLDOWN",
        "true"
    ).lower()
    == "true"
)


# ============================================================
# Discord
# ============================================================

# 保守控制單檔大小
MAX_DISCORD_FILE_SIZE = int(
    os.environ.get(
        "MAX_DISCORD_FILE_SIZE",
        "9500000"
    )
)


# 最多處理 9 個 media
MAX_MEDIA_ITEMS = 9


# ============================================================
# Global State
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
    )
})


# ============================================================
# Custom 429 Exception
# ============================================================

class InstagramRateLimited(Exception):
    """
    Instagram 429。

    收到 429 後直接交給外層 global cooldown。
    """

    def __init__(
        self,
        message,
        wait_seconds=None
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
    Instaloader 收到 429 時立即 raise。

    不讓 Instaloader 自己等待很久再 retry。
    """

    def handle_429(
        self,
        query_type
    ):
        raise InstagramRateLimited(
            "Instagram HTTP 429 Too Many Requests "
            f"(query_type={query_type})"
        )


# ============================================================
# Instaloader
# ============================================================

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

    rate_controller=lambda ctx:
        AbortOn429RateController(ctx),
)


# ============================================================
# Utility
# ============================================================

def format_seconds(seconds):
    seconds = max(0, int(seconds))

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
        float(seconds)
    )

    while (
        remaining > 0
        and not shutdown_requested
    ):
        time.sleep(
            min(remaining, 1.0)
        )

        remaining -= 1.0


# ============================================================
# State Load
# ============================================================

def load_state():
    global rate_limit_until
    global backoff_seconds

    if not os.path.exists(STATE_FILE):
        print(
            f"[STATE] {STATE_FILE} 不存在。"
        )
        print(
            "[STATE] 建立新的 State。"
        )
        return {}

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            raw_data = json.load(f)

        if not isinstance(
            raw_data,
            dict
        ):
            print(
                "[STATE] State 格式錯誤。"
            )
            return {}

        # ----------------------------------------------------
        # 新格式
        # ----------------------------------------------------

        if "_meta" in raw_data:

            meta = raw_data.get(
                "_meta",
                {}
            )

            if PERSIST_COOLDOWN:

                try:
                    saved_until = float(
                        meta.get(
                            "rate_limit_until",
                            0
                        )
                    )
                except (
                    TypeError,
                    ValueError
                ):
                    saved_until = 0

                if saved_until > time.time():

                    try:
                        saved_backoff = int(
                            meta.get(
                                "backoff_seconds",
                                INITIAL_BACKOFF
                            )
                        )
                    except (
                        TypeError,
                        ValueError
                    ):
                        saved_backoff = (
                            INITIAL_BACKOFF
                        )

                    with RATE_LIMIT_LOCK:

                        rate_limit_until = (
                            saved_until
                        )

                        backoff_seconds = min(
                            max(
                                saved_backoff,
                                INITIAL_BACKOFF
                            ),
                            MAX_BACKOFF
                        )

                    remaining_secs = rate_limit_until - time.time()
                    print(
                        "[STATE] "
                        "恢復之前的 Instagram cooldown。"
                    )
                    print(
                        f"[STATE] 剩餘：{format_seconds(remaining_secs)}"
                    )

            posts_state = raw_data.get(
                "posts",
                {}
            )

        else:

            # ------------------------------------------------
            # 相容舊版 state
            # ------------------------------------------------

            posts_state = raw_data

        if not isinstance(
            posts_state,
            dict
        ):
            posts_state = {}

        print(
            "[STATE] 已載入 "
            f"{len(posts_state)} "
            "個帳號紀錄。"
        )

        return posts_state

    except Exception as e:

        print(
            "[STATE] 讀取失敗："
            f"{e}"
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

        state_path.parent.mkdir(
            parents=True,
            exist_ok=True
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

                "rate_limit_until":
                    (
                        current_until
                        if PERSIST_COOLDOWN
                        else 0
                    ),

                "backoff_seconds":
                    (
                        current_backoff
                        if PERSIST_COOLDOWN
                        else INITIAL_BACKOFF
                    ),

                "updated_at":
                    time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
            },

            "posts":
                posts_state
        }

        # ----------------------------------------------------
        # Atomic write
        # ----------------------------------------------------

        temp_path = state_path.with_name(
            state_path.name + ".tmp"
        )

        with open(
            temp_path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

            f.flush()

            os.fsync(
                f.fileno()
            )

        os.replace(
            temp_path,
            state_path
        )

        print(
            "[STATE] State 已儲存。"
        )

        return True

    except Exception as e:

        print(
            "[STATE] 儲存失敗："
            f"{e}"
        )

        return False


# ============================================================
# Global State
# ============================================================

STATE = load_state()


# ============================================================
# Cooldown
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
        int(remaining + 0.999)
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
            re.IGNORECASE
        )

        if match:

            try:
                return int(
                    match.group(1)
                )
            except ValueError:
                pass

    return None


# ============================================================
# Trigger Backoff
# ============================================================

def trigger_backoff(
    error=None,
    override_wait=None
):

    global rate_limit_until
    global backoff_seconds

    wait_time = override_wait

    if wait_time is None:

        wait_time = (
            extract_wait_seconds(
                error
            )
        )

    # --------------------------------------------------------
    # 沒有 Instagram 指定等待時間
    # 使用 exponential backoff
    # --------------------------------------------------------

    if wait_time is None:

        jitter = random.randint(
            BACKOFF_JITTER_MIN,
            BACKOFF_JITTER_MAX
        )

        wait_time = (
            backoff_seconds
            + jitter
        )

    try:
        wait_time = int(
            wait_time
        )
    except (
        TypeError,
        ValueError
    ):
        wait_time = INITIAL_BACKOFF

    wait_time = max(
        1,
        wait_time
    )

    wait_time = min(
        wait_time,
        MAX_BACKOFF
    )

    # --------------------------------------------------------
    # Set cooldown
    # --------------------------------------------------------

    with RATE_LIMIT_LOCK:

        rate_limit_until = (
            time.time()
            + wait_time
        )

        backoff_seconds = min(
            max(
                INITIAL_BACKOFF,
                backoff_seconds * 2
            ),
            MAX_BACKOFF
        )

        cooldown_until = (
            rate_limit_until
        )

        next_backoff = (
            backoff_seconds
        )

    # --------------------------------------------------------
    # Log
    # --------------------------------------------------------

    print("")
    print("=" * 70)
    print(
        "🚨 INSTAGRAM 429 RATE LIMIT"
    )

    print(
        "⏳ Cooldown："
        f"{format_seconds(wait_time)}"
    )

    print(
        "🕐 預計恢復："
        + time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(
                cooldown_until
            )
        )
    )

    print(
        "📈 下一次 Backoff："
        f"{format_seconds(next_backoff)}"
    )

    print(
        "🛑 立即停止目前 cycle。"
    )

    print("=" * 70)
    print("")

    # --------------------------------------------------------
    # Persist
    # --------------------------------------------------------

    save_state(STATE)


def reset_backoff():

    global backoff_seconds

    with RATE_LIMIT_LOCK:

        backoff_seconds = (
            INITIAL_BACKOFF
        )


# ============================================================
# Instagram
# ============================================================

def get_latest_post(username):

    print(
        f"[IG] 取得 @{username} profile..."
    )

    profile = (
        instaloader.Profile
        .from_username(
            L.context,
            username
        )
    )

    posts = profile.get_posts()

    return next(
        posts,
        None
    )


# ============================================================
# Download Media
# ============================================================

def download_file(
    url,
    extension,
    prefix
):

    temp_file = tempfile.NamedTemporaryFile(
        delete=False,
        prefix=prefix,
        suffix=extension
    )

    temp_path = temp_file.name

    temp_file.close()

    try:

        response = HTTP.get(
            url,
            stream=True,
            timeout=90
        )

        response.raise_for_status()

        total_size = 0

        with open(
            temp_path,
            "wb"
        ) as f:

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
                        "[MEDIA] "
                        "檔案超過 Discord 限制。"
                    )

                    try:
                        os.remove(
                            temp_path
                        )
                    except OSError:
                        pass

                    return None

                f.write(chunk)

        print(
            "[MEDIA] "
            f"下載完成 "
            f"{total_size / 1024 / 1024:.2f} MB"
        )

        return temp_path

    except Exception as e:

        print(
            "[MEDIA] "
            f"下載失敗：{e}"
        )

        try:
            os.remove(
                temp_path
            )
        except OSError:
            pass

        return None


# ============================================================
# Post Media
# ============================================================

def get_post_media(post):

    media = []

    try:

        # ----------------------------------------------------
        # Carousel
        # ----------------------------------------------------

        if post.typename == "GraphSidecar":

            children = list(
                post.get_sidecar_nodes()
            )

            for index, child in enumerate(
                children[:MAX_MEDIA_ITEMS],
                start=1
            ):

                if (
                    child.is_video
                    and child.video_url
                ):

                    media.append({
                        "type": "video",
                        "url": child.video_url,
                        "index": index
                    })

                elif child.display_url:

                    media.append({
                        "type": "image",
                        "url": child.display_url,
                        "index": index
                    })

        # ----------------------------------------------------
        # Video
        # ----------------------------------------------------

        elif (
            post.is_video
            and post.video_url
        ):

            media.append({
                "type": "video",
                "url": post.video_url,
                "index": 1
            })

        # ----------------------------------------------------
        # Image
        # ----------------------------------------------------

        elif post.url:

            media.append({
                "type": "image",
                "url": post.url,
                "index": 1
            })

    except Exception as e:

        print(
            "[MEDIA] "
            f"解析失敗：{e}"
        )

    return media[:MAX_MEDIA_ITEMS]


# ============================================================
# Discord
# ============================================================

def send_discord_notification(
    username,
    post
):

    if not WEBHOOK_URL:

        print(
            "[DISCORD] "
            "Webhook 未設定。"
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

                "footer": {
                    "text":
                        "Instagram → Discord"
                }
            }

        ]

        # ----------------------------------------------------
        # Media
        # ----------------------------------------------------

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

            file_path = download_file(
                item["url"],
                extension,
                "ig_"
            )

            if not file_path:
                continue

            temp_files.append(
                file_path
            )

            file_object = open(
                file_path,
                "rb"
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
                        mime_type
                    )
                )
            )

            if media_type == "image":

                embeds.append({

                    "url":
                        post_url,

                    "image": {
                        "url":
                            "attachment://"
                            + filename
                    },

                    "color":
                        15467852
                })

            else:

                embeds.append({

                    "url":
                        post_url,

                    "description":
                        f"🎬 影片內容 #{index}",

                    "color":
                        15467852
                })

        # Discord 最多 10 embeds
        embeds = embeds[:10]

        payload = {

            "username":
                "Instagram 通知",

            "embeds":
                embeds
        }

        response = HTTP.post(

            WEBHOOK_URL,

            data={
                "payload_json":
                    json.dumps(
                        payload,
                        ensure_ascii=False
                    )
            },

            files=attachments,

            timeout=180
        )

        print(
            "[DISCORD] HTTP "
            f"{response.status_code}"
        )

        if response.status_code in (
            200,
            204
        ):

            print(
                "✅ [DISCORD] 推送成功。"
            )

            return True

        print(
            "❌ [DISCORD] Webhook 失敗："
            f"{response.status_code}"
        )

        print(
            response.text[:1000]
        )

        return False

    except Exception as e:

        print(
            "❌ [DISCORD] 發送失敗："
            f"{e}"
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
    state
):

    if is_rate_limited():

        return False

    try:

        print("")
        print(
            f"🔍 檢查 @{username}"
        )

        post = get_latest_post(
            username
        )

        if not post:

            print(
                f"[{username}] "
                "沒有找到貼文。"
            )

            return True

        shortcode = post.shortcode

        with STATE_LOCK:

            previous_shortcode = (
                state.get(username)
            )

        print(
            f"[{username}] "
            f"Latest: {shortcode}"
        )

        # ----------------------------------------------------
        # First Run
        # ----------------------------------------------------

        if previous_shortcode is None:

            print(
                f"[{username}] "
                "首次執行。"
            )

            print(
                f"[{username}] "
                "記錄目前貼文，不發 Discord。"
            )

            with STATE_LOCK:

                state[username] = (
                    shortcode
                )

            save_state(
                state
            )

            return True

        # ----------------------------------------------------
        # Same Post
        # ----------------------------------------------------

        if (
            previous_shortcode
            == shortcode
        ):

            print(
                f"[{username}] "
                "沒有新貼文。"
            )

            return True

        # ----------------------------------------------------
        # New Post
        # ----------------------------------------------------

        print(
            f"🚨 [{username}] "
            "發現新貼文！"
        )

        print(
            f"Old: {previous_shortcode}"
        )

        print(
            f"New: {shortcode}"
        )

        success = send_discord_notification(
            username,
            post
        )

        # ----------------------------------------------------
        # Discord 成功後才更新 State
        # ----------------------------------------------------

        if success:

            with STATE_LOCK:

                state[username] = (
                    shortcode
                )

            save_state(
                state
            )

            print(
                f"✅ [{username}] "
                "State 已更新。"
            )

        else:

            print(
                f"⚠️ [{username}] "
                "Discord 失敗，State 不更新。"
            )

        return True

    # ========================================================
    # Custom 429
    # ========================================================

    except InstagramRateLimited as e:

        print("")
        print(
            f"🚨 [{username}] "
            "Instagram 429。"
        )

        trigger_backoff(
            e,
            override_wait=e.wait_seconds
        )

        return False

    # ========================================================
    # Native 429
    # ========================================================

    except (
        instaloader.exceptions.TooManyRequestsException
    ) as e:

        print("")
        print(
            f"🚨 [{username}] "
            "Instagram 429 Too Many Requests。"
        )

        trigger_backoff(
            e
        )

        return False

    # ========================================================
    # Profile Not Exists
    # ========================================================

    except (
        instaloader.exceptions.ProfileNotExistsException
    ):

        print(
            f"⚠️ [{username}] "
            "Profile 不存在。"
        )

        return True

    # ========================================================
    # Connection Error
    # ========================================================

    except (
        instaloader.exceptions.ConnectionException
    ) as e:

        error_text = str(e)

        lower_text = (
            error_text.lower()
        )

        # 只偵測真正的 rate-limit 關鍵字
        is_429 = (

            "429" in lower_text

            or "too many requests"
            in lower_text

            or "too many"
            in lower_text

            or "rate limit"
            in lower_text

            or "rate-limit"
            in lower_text
        )

        if is_429:

            print(
                f"🚨 [{username}] "
                "偵測到 Instagram rate limit。"
            )

            trigger_backoff(
                e
            )

            return False

        print(
            f"❌ [{username}] "
            f"Connection error："
            f"{error_text}"
        )

        return True

    # ========================================================
    # Other Instaloader Error
    # ========================================================

    except (
        instaloader.exceptions.InstaloaderException
    ) as e:

        error_text = str(e)

        lower_text = (
            error_text.lower()
        )

        is_429 = (

            "429" in lower_text

            or "too many requests"
            in lower_text

            or "too many"
            in lower_text

            or "rate limit"
            in lower_text

            or "rate-limit"
            in lower_text
        )

        if is_429:

            print(
                f"🚨 [{username}] "
                "Instaloader Rate Limit。"
            )

            trigger_backoff(
                e
            )

            return False

        print(
            f"❌ [{username}] "
            f"Instaloader error："
            f"{error_text}"
        )

        return True

    # ========================================================
    # Unexpected Error
    # ========================================================

    except Exception as e:

        print(
            f"❌ [{username}] "
            f"Unexpected error："
            f"{e}"
        )

        return True


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
            "/healthz"
        ):

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/plain; "
                "charset=utf-8"
            )

            self.end_headers()

            self.wfile.write(
                b"Instagram Discord Bot is alive."
            )

            return

        self.send_response(
            404
        )

        self.end_headers()

    def log_message(
        self,
        format,
        *args
    ):

        return


# ============================================================
# Web Server
# ============================================================

def run_web_server():

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    server = HTTPServer(
        (
            "0.0.0.0",
            port
        ),
        HealthHandler
    )

    print(
        f"🌐 Health server "
        f"啟動於 Port {port}"
    )

    server.serve_forever()


# ============================================================
# Shutdown
# ============================================================

def handle_shutdown(
    signum,
    frame
):

    global shutdown_requested

    print("")
    print(
        "🛑 收到 Render shutdown signal。"
    )

    shutdown_requested = True


# ============================================================
# Configuration
# ============================================================

def print_configuration():

    print("")
    print("=" * 70)

    print(
        "Instagram → Discord Monitor"
    )

    print(
        "Render Production Version"
    )

    print("=" * 70)

    print(
        f"📋 Accounts: "
        f"{len(USERNAMES)}"
    )

    print(
        f"⏱️ Check interval: "
        f"{CHECK_INTERVAL}s "
        f"({CHECK_INTERVAL / 3600:.1f}h)"
    )

    print(
        f"⏳ Account delay: "
        f"{ACCOUNT_DELAY}s "
        f"({ACCOUNT_DELAY / 60:.1f}m)"
    )

    print(
        f"🚨 Initial backoff: "
        f"{INITIAL_BACKOFF}s"
    )

    print(
        f"🛑 Maximum backoff: "
        f"{MAX_BACKOFF}s"
    )

    print(
        f"💾 State file: "
        f"{STATE_FILE}"
    )

    print(
        f"💾 Persistent cooldown: "
        f"{PERSIST_COOLDOWN}"
    )

    print(
        f"📦 Max file size: "
        f"{MAX_DISCORD_FILE_SIZE / 1000000:.2f} MB"
    )

    print("=" * 70)


# ============================================================
# Main
# ============================================================

def main():

    global shutdown_requested

    signal.signal(
        signal.SIGTERM,
        handle_shutdown
    )

    signal.signal(
        signal.SIGINT,
        handle_shutdown
    )

    print_configuration()

    # --------------------------------------------------------
    # Validate
    # --------------------------------------------------------

    if not USERNAMES:

        print(
            "❌ IG_USERNAME 沒有設定。"
        )

        sys.exit(1)

    if not WEBHOOK_URL:

        print(
            "❌ Discord Webhook 沒有設定。"
        )

        sys.exit(1)

    # --------------------------------------------------------
    # Health Server
    # --------------------------------------------------------

    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    print(
        "✅ Health Check Server 已啟動。"
    )

    # --------------------------------------------------------
    # Main Loop
    # --------------------------------------------------------

    while not shutdown_requested:

        # ====================================================
        # Global Cooldown
        # ====================================================

        if is_rate_limited():

            remaining = (
                get_remaining_cooldown()
            )

            print("")
            print(
                "🛑 Instagram cooldown 中。"
            )

            print(
                f"⏳ 剩餘："
                f"{format_seconds(remaining)}"
            )

            sleep_interruptible(
                min(
                    max(remaining, 1),
                    60
                )
            )

            continue

        # ====================================================
        # Cycle Lock
        # ====================================================

        if not CYCLE_LOCK.acquire(
            blocking=False
        ):

            sleep_interruptible(
                5
            )

            continue

        try:

            print("")
            print("=" * 70)

            print(
                "⏰ 開始 Instagram 檢查循環"
            )

            print(
                time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )

            print(
                f"📋 帳號數："
                f"{len(USERNAMES)}"
            )

            print("=" * 70)

            hit_rate_limit = False

            # =================================================
            # Accounts
            # =================================================

            for index, username in enumerate(
                USERNAMES
            ):

                if shutdown_requested:
                    break

                if is_rate_limited():

                    hit_rate_limit = True

                    print(
                        "🛑 Global cooldown 啟動。"
                    )

                    break

                success = check_account(
                    username,
                    STATE
                )

                # ------------------------------------------------
                # 429
                # ------------------------------------------------

                if not success:

                    hit_rate_limit = True

                    print("")
                    print(
                        "🚨 Instagram rate limit。"
                    )

                    print(
                        "🛑 停止目前 cycle。"
                    )

                    print(
                        "🛑 後面的帳號本輪不再檢查。"
                    )

                    break

                # ------------------------------------------------
                # Account Delay
                # ------------------------------------------------

                if (
                    index
                    < len(USERNAMES) - 1
                ):

                    print(
                        f"⏳ 下一帳號等待 "
                        f"{ACCOUNT_DELAY}s "
                        f"({ACCOUNT_DELAY / 60:.1f}m)"
                    )

                    sleep_interruptible(
                        ACCOUNT_DELAY
                    )

            # =================================================
            # Rate Limit
            # =================================================

            if hit_rate_limit:

                print("")
                print("=" * 70)

                print(
                    "🚨 本輪因 Instagram 429 停止。"
                )

                print(
                    "⏳ 等待 global cooldown。"
                )

                print("=" * 70)

                continue

            # =================================================
            # Cycle Complete
            # =================================================

            if not shutdown_requested:

                reset_backoff()

                print("")
                print("=" * 70)

                print(
                    "✅ 本輪 Instagram 檢查完成。"
                )

                print(
                    f"📋 已檢查 "
                    f"{len(USERNAMES)} "
                    "個帳號。"
                )

                print(
                    f"😴 下一輪："
                    f"{CHECK_INTERVAL}s "
                    f"({CHECK_INTERVAL / 3600:.1f}h)"
                )

                print("=" * 70)

                sleep_interruptible(
                    CHECK_INTERVAL
                )

        finally:

            CYCLE_LOCK.release()

    print("")
    print(
        "👋 Instagram → Discord Bot "
        "已安全退出。"
    )


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    main()
