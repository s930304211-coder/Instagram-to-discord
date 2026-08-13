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
# Render + Multiple Instagram Accounts
# 429 Auto Cooldown
# Discord Webhook
# Persistent State
# Graceful Shutdown
# ============================================================


# ============================================================
# Environment Variables
# ============================================================

RAW_USERNAMES = os.environ.get(
    "IG_USERNAME",
    ""
)

USERNAMES = [
    username.strip()
    for username in RAW_USERNAMES.split(",")
    if username.strip()
]


# ------------------------------------------------------------
# Discord Webhook
# ------------------------------------------------------------

WEBHOOK_URL = os.environ.get(
    "INSTAGRAM_POST_WEBHOOK",
    os.environ.get(
        "WEBHOOK_URL",
        ""
    )
)


# ============================================================
# Polling Settings
# ============================================================

# 每輪檢查之間的時間
#
# 7200 = 2 小時
#
CHECK_INTERVAL = int(
    os.environ.get(
        "CHECK_INTERVAL",
        "7200"
    )
)


# 每個 IG 帳號之間的等待時間
#
# 180 = 3 分鐘
#
ACCOUNT_DELAY = int(
    os.environ.get(
        "ACCOUNT_DELAY",
        "180"
    )
)


# ============================================================
# 429 Backoff
# ============================================================

# 第一次 429：
#
# 1200 = 20 分鐘
#
INITIAL_BACKOFF = int(
    os.environ.get(
        "INITIAL_BACKOFF",
        "1200"
    )
)


# 最大 cooldown：
#
# 21600 = 6 小時
#
MAX_BACKOFF = int(
    os.environ.get(
        "MAX_BACKOFF",
        "21600"
    )
)


# 隨機 jitter
#
# 避免每次完全固定時間重新開始
#
BACKOFF_JITTER_MIN = int(
    os.environ.get(
        "BACKOFF_JITTER_MIN",
        "30"
    )
)


BACKOFF_JITTER_MAX = int(
    os.environ.get(
        "BACKOFF_JITTER_MAX",
        "120"
    )
)


# ============================================================
# Persistent State
# ============================================================

STATE_FILE = os.environ.get(
    "STATE_FILE",
    "last_posts.json"
)


# 是否將 cooldown 存入 state
#
# true = Render restart 後仍記住 cooldown
#
PERSIST_COOLDOWN = (
    os.environ.get(
        "PERSIST_COOLDOWN",
        "true"
    ).lower()
    == "true"
)


# ============================================================
# Discord File Settings
# ============================================================

# 保守使用 9.5 MB
#
MAX_DISCORD_FILE_SIZE = int(
    os.environ.get(
        "MAX_DISCORD_FILE_SIZE",
        "9500000"
    )
)


# Carousel 最多處理幾個檔案
#
# 9 個 media + 1 個主 embed = 10 embeds
#
MAX_MEDIA_ITEMS = 9


# ============================================================
# Global Shutdown State
# ============================================================

shutdown_requested = False


# ============================================================
# Locks
# ============================================================

STATE_LOCK = threading.Lock()

RATE_LIMIT_LOCK = threading.Lock()

CYCLE_LOCK = threading.Lock()


# ============================================================
# Global Rate Limit State
# ============================================================

rate_limit_until = 0.0

backoff_seconds = INITIAL_BACKOFF


# ============================================================
# Custom 429 Exception
# ============================================================


class InstagramRateLimited(Exception):
    """
    Instagram 429 exception.

    讓我們可以直接離開 Instaloader，
    不讓 Instaloader 自己等待數百秒後重試。
    """

    def __init__(
        self,
        message,
        wait_seconds=None
    ):

        super().__init__(
            message
        )

        self.wait_seconds = wait_seconds


# ============================================================
# Custom Instaloader Rate Controller
# ============================================================


class AbortOn429RateController(
    instaloader.RateController
):
    """
    自訂 Instaloader RateController。

    官方 RateController.handle_429()
    接收的是 query_type，而不是 response。

    因此這裡不嘗試從 query.response
    讀 Retry-After。

    收到 429 時直接 raise，
    交給外層 global cooldown 處理。
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

    # 非常重要：
    # 不要讓單一 request 無限重試
    max_connection_attempts=1,

    request_timeout=60,

    # 自訂 429 行為
    rate_controller=lambda ctx:
        AbortOn429RateController(ctx),
)


# ============================================================
# Utility
# ============================================================


def format_seconds(
    seconds
):

    seconds = max(
        0,
        int(seconds)
    )

    hours = (
        seconds // 3600
    )

    minutes = (
        (seconds % 3600)
        // 60
    )

    secs = (
        seconds % 60
    )

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

    return (
        f"{secs}s"
    )


# ============================================================
# Interruptible Sleep
# ============================================================


def sleep_interruptible(
    seconds
):

    global shutdown_requested

    remaining = max(
        0.0,
        float(seconds)
    )

    while (
        remaining > 0
        and not shutdown_requested
    ):

        chunk = min(
            remaining,
            1.0
        )

        time.sleep(
            chunk
        )

        remaining -= chunk


# ============================================================
# State Handling
# ============================================================


def load_state():

    global rate_limit_until
    global backoff_seconds

    if not os.path.exists(
        STATE_FILE
    ):

        print(
            f"[STATE] "
            f"{STATE_FILE} 不存在，"
            "建立新的 State。"
        )

        return {}


    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            raw_data = json.load(
                f
            )


        if not isinstance(
            raw_data,
            dict
        ):

            print(
                "[STATE] "
                "State 格式不正確，重設。"
            )

            return {}


        # ----------------------------------------------------
        # New state format
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
                            0.0
                        )
                    )

                except (
                    TypeError,
                    ValueError
                ):

                    saved_until = 0.0


                if (
                    saved_until
                    > time.time()
                ):

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


                    print(
                        "[STATE] "
                        "恢復 Instagram cooldown。"
                    )

                    print(
                        "[STATE] "
                        f"剩餘："
                        f"{format_seconds("
                            rate_limit_until
                            - time.time()
                        )}"
                    )


            posts_state = raw_data.get(
                "posts",
                {}
            )

        else:

            # ------------------------------------------------
            # Backward compatibility
            # 舊版 state：
            #
            # {
            #   "account1": "shortcode"
            # }
            # ------------------------------------------------

            posts_state = raw_data


        if not isinstance(
            posts_state,
            dict
        ):

            posts_state = {}


        print(
            "[STATE] "
            f"已載入 "
            f"{len(posts_state)} "
            "個帳號紀錄。"
        )


        return posts_state


    except Exception as e:

        print(
            f"[STATE] "
            f"讀取失敗：{e}"
        )

        return {}


# ============================================================
# Save State
# ============================================================


def save_state(
    posts_state
):

    try:

        state_path = Path(
            STATE_FILE
        )


        # Render Persistent Disk
        # 或其他指定資料夾
        #
        if state_path.parent:

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


        data_to_save = {

            "_meta": {

                "rate_limit_until":
                    (
                        current_until
                        if PERSIST_COOLDOWN
                        else 0.0
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

        temp_path = (
            state_path.with_name(
                state_path.name
                + ".tmp"
            )
        )


        with open(
            temp_path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data_to_save,
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
            "[STATE] "
            "State 已安全儲存。"
        )


        return True


    except Exception as e:

        print(
            f"[STATE] "
            f"儲存失敗：{e}"
        )

        return False


# ============================================================
# Load Global State
# ============================================================

STATE = load_state()


# ============================================================
# Rate Limit Controller
# ============================================================


def is_rate_limited():

    with RATE_LIMIT_LOCK:

        return (
            time.time()
            < rate_limit_until
        )


# ============================================================


def get_remaining_cooldown():

    with RATE_LIMIT_LOCK:

        remaining = (
            rate_limit_until
            - time.time()
        )


    # 使用 ceil 避免：
    #
    # 1.2 秒
    # int() → 1
    # 0.2 秒
    # int() → 0
    #
    # 導致：
    #
    # cooldown 0s
    #
    # 所以這裡至少回傳 1。
    #
    if remaining <= 0:

        return 0


    return max(
        1,
        int(remaining + 0.999)
    )


# ============================================================
# Extract Instagram Wait Time
# ============================================================


def extract_wait_seconds(
    error
):

    if not error:

        return None


    text = str(
        error
    )


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

            except (
                ValueError
            ):

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


    # --------------------------------------------------------
    # Instagram explicit wait time
    # --------------------------------------------------------

    wait_time = (
        override_wait
    )


    if wait_time is None:

        wait_time = (
            extract_wait_seconds(
                error
            )
        )


    # --------------------------------------------------------
    # If Instagram did not provide wait time
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

        wait_time = (
            INITIAL_BACKOFF
        )


    # 不允許 0 秒 cooldown
    #
    wait_time = max(
        1,
        wait_time
    )


    # 最大 cooldown
    #
    wait_time = min(
        wait_time,
        MAX_BACKOFF
    )


    # --------------------------------------------------------
    # Set global cooldown
    # --------------------------------------------------------

    with RATE_LIMIT_LOCK:

        rate_limit_until = (
            time.time()
            + wait_time
        )


        # Exponential backoff
        #
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


        current_backoff = (
            backoff_seconds
        )


    # --------------------------------------------------------
    # Log
    # --------------------------------------------------------

    print("")
    print(
        "=" * 70
    )

    print(
        "🚨 INSTAGRAM 429 RATE LIMIT"
    )

    print(
        f"⏳ Cooldown："
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
        f"📈 下一次 Backoff："
        f"{format_seconds(current_backoff)}"
    )

    print(
        "🛑 本輪 Instagram 檢查將立即停止。"
    )

    print(
        "=" * 70
    )

    print("")


    # --------------------------------------------------------
    # Persist cooldown
    # --------------------------------------------------------

    save_state(
        STATE
    )


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
# Instagram
# ============================================================


def get_latest_post(
    username
):

    print(
        f"[IG] "
        f"正在取得 @{username} profile..."
    )


    profile = (
        instaloader.Profile
        .from_username(
            L.context,
            username
        )
    )


    posts = (
        profile.get_posts()
    )


    # 真正 request 通常在
    # iterator next() 時發生
    #
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

    temp_file = (
        tempfile.NamedTemporaryFile(
            delete=False,
            prefix=prefix,
            suffix=extension
        )
    )


    temp_path = (
        temp_file.name
    )


    temp_file.close()


    try:

        print(
            f"[MEDIA] "
            f"下載 {extension}..."
        )


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


                total_size += (
                    len(chunk)
                )


                # ------------------------------------------------
                # Discord file size limit
                # ------------------------------------------------

                if (
                    total_size
                    > MAX_DISCORD_FILE_SIZE
                ):

                    print(
                        "[MEDIA] "
                        "檔案超出 Discord 上限，"
                        "跳過。"
                    )


                    # 非常重要：
                    # 立刻刪除暫存檔
                    #
                    try:

                        os.remove(
                            temp_path
                        )

                    except OSError:

                        pass


                    return None


                f.write(
                    chunk
                )


        print(
            "[MEDIA] "
            f"下載完成："
            f"{total_size / 1024 / 1024:.2f} MB"
        )


        return temp_path


    except Exception as e:

        print(
            f"[MEDIA] "
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
# Get Post Media
# ============================================================


def get_post_media(
    post
):

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
                "[MEDIA] "
                "Type: CAROUSEL"
            )


            children = list(
                post.get_sidecar_nodes()
            )


            print(
                "[MEDIA] "
                f"Carousel items: "
                f"{len(children)}"
            )


            for index, child in enumerate(

                children[
                    :MAX_MEDIA_ITEMS
                ],

                start=1

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
                            index

                    })


                elif (
                    child.display_url
                ):

                    media.append({

                        "type":
                            "image",

                        "url":
                            child.display_url,

                        "index":
                            index

                    )


        # ----------------------------------------------------
        # Single Video
        # ----------------------------------------------------

        elif (
            post.is_video
            and post.video_url
        ):

            print(
                "[MEDIA] "
                "Type: VIDEO"
            )


            media.append({

                "type":
                    "video",

                "url":
                    post.video_url,

                "index":
                    1

            })


        # ----------------------------------------------------
        # Single Image
        # ----------------------------------------------------

        elif post.url:

            print(
                "[MEDIA] "
                "Type: IMAGE"
            )


            media.append({

                "type":
                    "image",

                "url":
                    post.url,

                "index":
                    1

            })


    except Exception as e:

        print(
            "[MEDIA] "
            f"解析媒體失敗：{e}"
        )


    return media[
        :MAX_MEDIA_ITEMS
    ]


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
            "Webhook URL 未設定。"
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


    # Discord embed description
    # 最多保守控制
    #
    if len(caption) > 1800:

        caption = (
            caption[:1800]
            + "\n\n..."
        )


    media = get_post_media(
        post
    )


    print(
        "[DISCORD] "
        f"準備傳送 "
        f"{len(media)} "
        "個 media。"
    )


    temp_files = []

    file_handles = []

    attachments = []


    try:

        # ----------------------------------------------------
        # Main Embed
        # ----------------------------------------------------

        embeds = []


        main_embed = {

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


        embeds.append(
            main_embed
        )


        # ----------------------------------------------------
        # Download Media
        # ----------------------------------------------------

        for item in media:

            index = (
                item["index"]
            )

            media_type = (
                item["type"]
            )


            if media_type == "video":

                extension = (
                    ".mp4"
                )

                filename = (
                    f"instagram_{index}.mp4"
                )

                mime_type = (
                    "video/mp4"
                )


            else:

                extension = (
                    ".jpg"
                )

                filename = (
                    f"instagram_{index}.jpg"
                )

                mime_type = (
                    "image/jpeg"
                )


            print(
                "[DISCORD] "
                f"處理 media #{index} "
                f"({media_type})"
            )


            file_path = (
                download_file(

                    item["url"],

                    extension,

                    "ig_"

                )
            )


            if not file_path:

                print(
                    "[DISCORD] "
                    f"跳過 media #{index}"
                )

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


            # ------------------------------------------------
            # Image Embed
            # ------------------------------------------------

            if media_type == "image":

                embeds.append({

                    "url":
                        post_url,

                    "image": {

                        "url":
                            "attachment://"
                            f"{filename}"

                    },

                    "color":
                        15467852

                })


            # ------------------------------------------------
            # Video
            # ------------------------------------------------

            else:

                embeds.append({

                    "url":
                        post_url,

                    "description":
                        f"🎬 影片內容 #{index}",

                    "color":
                        15467852

                })


        # Discord:
        #
        # 10 embeds max
        #
        embeds = embeds[:10]


        payload = {

            "username":
                "Instagram 通知",

            "embeds":
                embeds

        }


        payload_json = json.dumps(

            payload,

            ensure_ascii=False

        )


        print("")

        print(
            "[DISCORD] "
            f"Uploading "
            f"{len(attachments)} "
            "files..."
        )


        response = HTTP.post(

            WEBHOOK_URL,

            data={

                "payload_json":
                    payload_json

            },

            files=attachments,

            timeout=180

        )


        print(
            "[DISCORD] "
            f"HTTP "
            f"{response.status_code}"
        )


        if response.status_code in (
            200,
            204
        ):

            print(
                "✅ [DISCORD] "
                "推送成功。"
            )

            return True


        print(
            "❌ [DISCORD] "
            f"Webhook 失敗 "
            f"(HTTP {response.status_code})"
        )


        print(
            response.text[:2000]
        )


        return False


    except requests.exceptions.RequestException as e:

        print(
            "❌ [DISCORD] "
            f"Request error：{e}"
        )

        return False


    except Exception as e:

        print(
            "❌ [DISCORD] "
            f"Unexpected error：{e}"
        )

        return False


    finally:

        # ----------------------------------------------------
        # Close file handles
        # ----------------------------------------------------

        for file_object in file_handles:

            try:

                file_object.close()

            except Exception:

                pass


        # ----------------------------------------------------
        # Delete temp files
        # ----------------------------------------------------

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

    # --------------------------------------------------------
    # Global cooldown
    # --------------------------------------------------------

    if is_rate_limited():

        print(
            f"[{username}] "
            "Instagram cooldown 中。"
        )

        return False


    try:

        print("")
        print(
            "=" * 60
        )

        print(
            f"🔍 Checking @{username}"
        )


        # ----------------------------------------------------
        # Get latest post
        # ----------------------------------------------------

        post = get_latest_post(
            username
        )


        if not post:

            print(
                f"[{username}] "
                "沒有找到貼文。"
            )

            return True


        shortcode = (
            post.shortcode
        )


        with STATE_LOCK:

            previous_shortcode = (
                state.get(
                    username
                )
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
                "FIRST RUN"
            )


            print(
                f"[{username}] "
                f"記錄 {shortcode}，"
                "不發 Discord 通知。"
            )


            with STATE_LOCK:

                state[
                    username
                ] = shortcode


            save_state(
                state
            )


            return True


        # ----------------------------------------------------
        # No New Post
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

        print("")
        print(
            "🚨 "
            f"NEW POST @{username}"
        )


        print(
            f"New: {shortcode}"
        )


        print(
            f"Old: {previous_shortcode}"
        )


        # ----------------------------------------------------
        # Discord
        # ----------------------------------------------------

        success = (
            send_discord_notification(

                username,

                post

            )
        )


        # ----------------------------------------------------
        # Only update state after
        # Discord succeeds
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
                    "State updated."
                )

            else:

                print(
                    f"⚠️ [{username}] "
                    "Discord 已成功，"
                    "但 State 儲存失敗。"
                )


            return True


        # ----------------------------------------------------
        # Discord failed
        # ----------------------------------------------------

        print(
            f"❌ [{username}] "
            "Discord 發送失敗。"
        )


        print(
            f"⚠️ [{username}] "
            "State 不更新。"
        )


        return True


    # ========================================================
    # Our custom 429
    # ========================================================

    except InstagramRateLimited as e:

        print("")
        print(
            f"🚨 [{username}] "
            "Instagram 429 被攔截。"
        )


        print(
            f"[429] {e}"
        )


        trigger_backoff(
            e,
            override_wait=e.wait_seconds
        )


        return False


    # ========================================================
    # Instaloader native 429
    # ========================================================

    except (
        instaloader
        .exceptions
        .TooManyRequestsException
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
    # Connection Error
    # ========================================================

    except (
        instaloader
        .exceptions
        .ConnectionException
    ) as e:

        error_text = str(
            e
        )


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

            or "rate" 
            in lower_text

        ):

            print(
                f"🚨 [{username}] "
                f"偵測到 Rate Limit："
                f"{error_text}"
            )


            trigger_backoff(
                e
            )


            return False


        print(
            f"❌ [{username}] "
            f"Instagram connection error："
            f"{error_text}"
        )


        return True


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
            "Profile 不存在。"
        )


        return True


    # ========================================================
    # Other Instaloader Error
    # ========================================================

    except (
        instaloader
        .exceptions
        .InstaloaderException
    ) as e:

        error_text = str(
            e
        )


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

        ):

            print(
                f"🚨 [{username}] "
                "Instaloader 回報 Rate Limit。"
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
# Health Check Server
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
        *args
    ):

        # 不顯示 HTTP request log
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


    print("")
    print(
        f"🌐 Health Check Server "
        f"啟動於 Port {port}"
    )


    server.serve_forever()


# ============================================================
# Graceful Shutdown
# ============================================================


def handle_shutdown(
    signum,
    frame
):

    global shutdown_requested


    print("")
    print(
        "🛑 收到 Render termination signal。"
    )


    print(
        "🛑 準備優雅關閉程序..."
    )


    shutdown_requested = True


# ============================================================
# Print Configuration
# ============================================================


def print_configuration():

    print("")
    print(
        "=" * 70
    )

    print(
        "Instagram → Discord Monitor"
    )

    print(
        "Production / Render Version"
    )

    print(
        "=" * 70
    )


    print(
        f"📋 Instagram Accounts: "
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
        f"📦 Max Discord file: "
        f"{MAX_DISCORD_FILE_SIZE / 1000000:.2f} MB"
    )


    print(
        "=" * 70
    )


# ============================================================
# Main
# ============================================================


def main():

    global shutdown_requested


    # --------------------------------------------------------
    # Signal
    # --------------------------------------------------------

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
            "❌ ERROR: "
            "IG_USERNAME 沒有設定。"
        )


        sys.exit(
            1
        )


    if not WEBHOOK_URL:

        print(
            "❌ ERROR: "
            "Discord Webhook 沒有設定。"
        )


        sys.exit(
            1
        )


    # --------------------------------------------------------
    # Start Health Server
    # --------------------------------------------------------

    server_thread = threading.Thread(

        target=run_web_server,

        daemon=True

    )


    server_thread.start()


    print(
        "✅ Health Check Server 已啟動。"
    )


    # --------------------------------------------------------
    # Main Loop
    # --------------------------------------------------------

    while not shutdown_requested:


        # ====================================================
        # GLOBAL COOLDOWN
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


            # ------------------------------------------------
            # 非常重要
            #
            # 即使 remaining == 0，
            # 也不能直接開始 cycle。
            #
            # 避免：
            #
            # cooldown 0s
            #
            # 然後馬上重新 request。
            # ------------------------------------------------

            if remaining <= 0:

                sleep_interruptible(
                    1
                )

            else:

                sleep_interruptible(

                    min(
                        remaining,
                        60
                    )

                )


            continue


        # ====================================================
        # Start Cycle
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
            print(
                "=" * 70
            )


            print(
                "⏰ 開始 Instagram 檢查循環"
            )


            print(
                time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )


            print(
                f"📋 本輪帳號數："
                f"{len(USERNAMES)}"
            )


            print(
                "=" * 70
            )


            hit_rate_limit = False


            # =================================================
            # Check every account
            # =================================================

            for index, username in enumerate(
                USERNAMES
            ):


                # ------------------------------------------------
                # Shutdown
                # ------------------------------------------------

                if shutdown_requested:

                    break


                # ------------------------------------------------
                # Global cooldown
                # ------------------------------------------------

                if is_rate_limited():

                    hit_rate_limit = True


                    print(
                        "🛑 Instagram cooldown "
                        "已啟動。"
                    )


                    print(
                        "🛑 立即停止目前 cycle。"
                    )


                    break


                # ------------------------------------------------
                # Account
                # ------------------------------------------------

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
                        "🛑 不再檢查後面的帳號。"
                    )


                    break


                # ------------------------------------------------
                # Account delay
                # ------------------------------------------------

                if (

                    index
                    < len(USERNAMES) - 1

                ):

                    print(
                        ""
                    )


                    print(
                        f"⏳ 下一個帳號前等待 "
                        f"{ACCOUNT_DELAY}s "
                        f"({ACCOUNT_DELAY / 60:.1f} 分鐘)..."
                    )


                    sleep_interruptible(
                        ACCOUNT_DELAY
                    )


            # =================================================
            # Rate Limited
            # =================================================

            if hit_rate_limit:

                print("")
                print(
                    "=" * 70
                )


                print(
                    "🚨 本輪檢查因 Instagram "
                    "rate limit 而停止。"
                )


                print(
                    "⏳ 等待 global cooldown。"
                )


                print(
                    "=" * 70
                )


                # 不在這裡 sleep 固定時間
                #
                # 交給下一輪 while
                # 根據 rate_limit_until 控制
                #
                continue


            # =================================================
            # Cycle Success
            # =================================================

            if not shutdown_requested:

                reset_backoff()


                print("")
                print(
                    "=" * 70
                )


                print(
                    f"✅ 本輪完成。"
                    f"已檢查 "
                    f"{len(USERNAMES)} "
                    "個帳號。"
                )


                print(
                    f"😴 下一輪等待 "
                    f"{CHECK_INTERVAL}s "
                    f"({CHECK_INTERVAL / 3600:.1f} 小時)"
                )


                print(
                    "=" * 70
                )


                sleep_interruptible(
                    CHECK_INTERVAL
                )


        finally:

            CYCLE_LOCK.release()


    # ========================================================
    # Shutdown
    # ========================================================

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
