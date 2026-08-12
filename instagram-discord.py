#!/usr/bin/env python3

import os
import re
import time
import json
import random
import signal
import tempfile
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import instaloader


# =========================================================
# ENVIRONMENT VARIABLES
# =========================================================

RAW_USERNAMES = os.environ.get(
    "IG_USERNAME",
    ""
)

WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    ""
)

# ---------------------------------------------------------
# 正常輪詢間隔
# Render Free 建議 7200 = 2 小時
# ---------------------------------------------------------

TIME_INTERVAL = int(
    os.environ.get(
        "TIME_INTERVAL",
        "7200"
    )
)

# ---------------------------------------------------------
# 帳號之間的等待
# 目前 Render 建議 120 秒
# ---------------------------------------------------------

ACCOUNT_DELAY = int(
    os.environ.get(
        "ACCOUNT_DELAY",
        "120"
    )
)

# ---------------------------------------------------------
# 429 基本冷卻時間
# 1200 = 20 分鐘
# ---------------------------------------------------------

COOLDOWN_ON_429 = int(
    os.environ.get(
        "COOLDOWN_ON_429",
        "1200"
    )
)

# ---------------------------------------------------------
# 最大 429 冷卻時間
# 21600 = 6 小時
# ---------------------------------------------------------

MAX_COOLDOWN = int(
    os.environ.get(
        "MAX_COOLDOWN",
        "21600"
    )
)

# ---------------------------------------------------------
# Render 啟動後，先等多久才碰 Instagram
# 900 = 15 分鐘
# ---------------------------------------------------------

STARTUP_DELAY = int(
    os.environ.get(
        "STARTUP_DELAY",
        "900"
    )
)

# ---------------------------------------------------------
# State file
# Render Free 重啟後仍可能消失
# ---------------------------------------------------------

STATE_FILE = os.environ.get(
    "STATE_FILE",
    "last_posts.json"
)

# ---------------------------------------------------------
# Discord 單檔案保守限制
# 9.5 MB
# ---------------------------------------------------------

MAX_DISCORD_FILE_SIZE = int(
    os.environ.get(
        "MAX_DISCORD_FILE_SIZE",
        "9500000"
    )
)

# ---------------------------------------------------------
# Render PORT
# ---------------------------------------------------------

PORT = int(
    os.environ.get(
        "PORT",
        "10000"
    )
)


# =========================================================
# GLOBAL STATE
# =========================================================

shutdown_event = threading.Event()

rate_limit_until = 0

# 連續 429 次數
consecutive_429 = 0


# =========================================================
# HTTP SESSION
# =========================================================

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


# =========================================================
# INSTALOADER
# =========================================================

# 重要：
#
# fatal_status_codes=[429]
#
# 讓 429 不要由 Instaloader 自己等 666 秒再 retry。
# 我們自己控制 cooldown。
#
# max_connection_attempts=1
#
# 避免同一個 request 自己重試很多次。
# =========================================================

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

    fatal_status_codes=[429]
)


# =========================================================
# STATE
# =========================================================

def load_state():

    try:

        if not os.path.exists(
            STATE_FILE
        ):
            return {}

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(
            data,
            dict
        ):
            return data

    except Exception as e:

        print(
            f"[STATE] Load error: {e}"
        )

    return {}


def save_state(state):

    try:

        state_path = Path(
            STATE_FILE
        )

        if (
            str(state_path.parent)
            != "."
        ):

            state_path.parent.mkdir(
                parents=True,
                exist_ok=True
            )

        temp_path = (
            state_path.with_suffix(
                ".tmp"
            )
        )

        with open(
            temp_path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temp_path,
            state_path
        )

    except Exception as e:

        print(
            f"[STATE] Save error: {e}"
        )


STATE = load_state()


# =========================================================
# HEALTH SERVER
# =========================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        if self.path in (
            "/",
            "/health",
            "/healthz"
        ):

            body = (
                b"Instagram Discord Monitor OK"
            )

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(
                body
            )

        else:

            self.send_response(
                404
            )

            self.end_headers()

    def log_message(
        self,
        format,
        *args
    ):

        # 不讓 health check 洗掉 Render Logs
        return


def start_health_server():

    try:

        server = HTTPServer(
            (
                "0.0.0.0",
                PORT
            ),
            HealthHandler
        )

        print(
            f"[WEB] Health server "
            f"listening on port {PORT}"
        )

        while not shutdown_event.is_set():

            server.timeout = 1

            server.handle_request()

        server.server_close()

    except Exception as e:

        print(
            f"[WEB] Health server error: {e}"
        )


# =========================================================
# RATE LIMIT
# =========================================================

def is_rate_limited():

    return (
        time.time()
        < rate_limit_until
    )


def extract_wait_seconds(
    error_text
):

    if not error_text:
        return None

    # -----------------------------------------------------
    # Seconds
    # -----------------------------------------------------

    second_patterns = [

        r"wait\s+(\d+)\s+seconds",

        r"(\d+)\s+seconds",

        r"retry.*?(\d+)\s+seconds",

        r"try again.*?(\d+)\s+seconds"

    ]

    for pattern in second_patterns:

        match = re.search(
            pattern,
            error_text,
            re.IGNORECASE
        )

        if match:

            try:

                return int(
                    match.group(1)
                )

            except ValueError:

                pass

    # -----------------------------------------------------
    # Minutes
    # -----------------------------------------------------

    minute_patterns = [

        r"wait\s+(\d+)\s+minutes",

        r"(\d+)\s+minutes",

        r"retry.*?(\d+)\s+minutes",

        r"try again.*?(\d+)\s+minutes"

    ]

    for pattern in minute_patterns:

        match = re.search(
            pattern,
            error_text,
            re.IGNORECASE
        )

        if match:

            try:

                return (
                    int(
                        match.group(1)
                    )
                    * 60
                )

            except ValueError:

                pass

    return None


def trigger_backoff(
    error=None
):

    global rate_limit_until
    global consecutive_429

    consecutive_429 += 1

    error_text = (
        str(error)
        if error
        else ""
    )

    instagram_wait = (
        extract_wait_seconds(
            error_text
        )
    )

    # -----------------------------------------------------
    # Exponential Backoff
    #
    # 第一次：
    # 1200
    #
    # 第二次：
    # 2400
    #
    # 第三次：
    # 4800
    #
    # 第四次：
    # 9600
    #
    # ...
    # -----------------------------------------------------

    exponential_wait = min(
        COOLDOWN_ON_429
        * (
            2 ** (
                consecutive_429 - 1
            )
        ),
        MAX_COOLDOWN
    )

    # -----------------------------------------------------
    # Instagram 明確告知等待時間
    # -----------------------------------------------------

    if instagram_wait:

        # 加 60～180 秒安全緩衝
        jitter = random.randint(
            60,
            180
        )

        instagram_wait_with_buffer = (
            instagram_wait
            + jitter
        )

        wait_time = max(
            exponential_wait,
            instagram_wait_with_buffer
        )

    else:

        jitter = random.randint(
            60,
            180
        )

        wait_time = (
            exponential_wait
            + jitter
        )

    wait_time = min(
        wait_time,
        MAX_COOLDOWN
    )

    rate_limit_until = (
        time.time()
        + wait_time
    )

    print("")
    print("=" * 70)
    print("🚨 INSTAGRAM 429 RATE LIMIT")
    print("=" * 70)

    print(
        f"Consecutive 429: "
        f"{consecutive_429}"
    )

    if instagram_wait:

        print(
            "Instagram requested: "
            f"{instagram_wait} seconds"
        )

        print(
            "Added safety buffer: "
            f"{wait_time - instagram_wait} seconds"
        )

    else:

        print(
            "Instagram wait time "
            "could not be detected."
        )

    print(
        f"Cooldown: "
        f"{wait_time} seconds"
    )

    print(
        "Resume around: "
        f"{time.ctime(rate_limit_until)}"
    )

    print(
        "NO INSTAGRAM REQUESTS "
        "DURING COOLDOWN."
    )

    print("=" * 70)
    print("")


def reset_backoff():

    global consecutive_429

    consecutive_429 = 0


# =========================================================
# INSTAGRAM
# =========================================================

def get_latest_post(
    username
):

    profile = (
        instaloader.Profile.from_username(
            L.context,
            username
        )
    )

    posts = profile.get_posts()

    return next(
        posts,
        None
    )


# =========================================================
# MEDIA DOWNLOAD
# =========================================================

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

        print(
            f"[MEDIA] Downloading "
            f"{extension}..."
        )

        response = HTTP.get(
            url,
            stream=True,
            timeout=60
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

                total_size += len(
                    chunk
                )

                # -----------------------------------------
                # 過大檔案
                # -----------------------------------------

                if (
                    total_size
                    > MAX_DISCORD_FILE_SIZE
                ):

                    print(
                        "[MEDIA] File exceeds "
                        "Discord size limit."
                    )

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
            "[MEDIA] Downloaded "
            f"{total_size / 1024 / 1024:.2f} MB"
        )

        return temp_path

    except Exception as e:

        print(
            f"[MEDIA] Download error: {e}"
        )

        try:

            os.remove(
                temp_path
            )

        except OSError:

            pass

        return None


# =========================================================
# GET POST MEDIA
# =========================================================

def get_post_media(
    post
):

    media = []

    # =====================================================
    # CAROUSEL
    # =====================================================

    if (
        post.typename
        == "GraphSidecar"
    ):

        try:

            children = list(
                post.get_sidecar_nodes()
            )

            print(
                f"[MEDIA] Carousel has "
                f"{len(children)} items"
            )

            for index, child in enumerate(
                children,
                start=1
            ):

                # -----------------------------
                # Video
                # -----------------------------

                if child.is_video:

                    if child.video_url:

                        media.append({

                            "type":
                                "video",

                            "url":
                                child.video_url,

                            "index":
                                index

                        })

                # -----------------------------
                # Image
                # -----------------------------

                else:

                    if child.display_url:

                        media.append({

                            "type":
                                "image",

                            "url":
                                child.display_url,

                            "index":
                                index

                        })

        except Exception as e:

            print(
                "[MEDIA] Carousel error: "
                f"{e}"
            )

    # =====================================================
    # SINGLE VIDEO
    # =====================================================

    elif post.is_video:

        if post.video_url:

            media.append({

                "type":
                    "video",

                "url":
                    post.video_url,

                "index":
                    1

            })

    # =====================================================
    # SINGLE IMAGE
    # =====================================================

    else:

        if post.url:

            media.append({

                "type":
                    "image",

                "url":
                    post.url,

                "index":
                    1

            })

    # Instagram Carousel 最多處理 10 個
    return media[:10]


# =========================================================
# DISCORD POST
# =========================================================

def discord_post(
    payload,
    files=None
):

    try:

        if files:

            response = HTTP.post(

                WEBHOOK_URL,

                data={
                    "payload_json":
                        json.dumps(
                            payload,
                            ensure_ascii=False
                        )
                },

                files=files,

                timeout=120
            )

        else:

            response = HTTP.post(

                WEBHOOK_URL,

                json=payload,

                timeout=30
            )

        response.raise_for_status()

        return True

    except requests.exceptions.HTTPError as e:

        print(
            f"[DISCORD] HTTP error: {e}"
        )

        if e.response is not None:

            print(
                "[DISCORD] Response: "
                f"{e.response.text[:1000]}"
            )

        return False

    except Exception as e:

        print(
            f"[DISCORD] Error: {e}"
        )

        return False


# =========================================================
# SEND MEDIA BATCH
# =========================================================

def send_media_batch(
    media_items,
    post_url
):

    if not media_items:

        return True

    attachments = []

    embeds = []

    temp_files = []

    file_objects = []

    try:

        for item in media_items:

            index = item["index"]

            media_type = item["type"]

            # -------------------------------------------------
            # Filename
            # -------------------------------------------------

            if media_type == "image":

                extension = ".jpg"

                filename = (
                    f"instagram_{index}.jpg"
                )

            else:

                extension = ".mp4"

                filename = (
                    f"instagram_{index}.mp4"
                )

            # -------------------------------------------------
            # Download
            # -------------------------------------------------

            file_path = download_file(
                item["url"],
                extension,
                "ig_"
            )

            if file_path is None:

                print(
                    f"[MEDIA] Skipping "
                    f"media #{index}"
                )

                continue

            temp_files.append(
                file_path
            )

            # -------------------------------------------------
            # File object
            # -------------------------------------------------

            file_object = open(
                file_path,
                "rb"
            )

            file_objects.append(
                file_object
            )

            mime_type = (
                "video/mp4"
                if media_type == "video"
                else "image/jpeg"
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

            # -------------------------------------------------
            # Image Embed
            # -------------------------------------------------

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

            # -------------------------------------------------
            # Video
            #
            # Discord 會原生顯示 mp4 attachment
            # 不需要 image embed
            # -------------------------------------------------

        if not attachments:

            return False

        payload = {

            "embeds":
                embeds[:10]

        }

        print(
            "[DISCORD] Sending "
            f"{len(attachments)} media."
        )

        return discord_post(
            payload,
            attachments
        )

    finally:

        # 關閉檔案
        for file_object in file_objects:

            try:

                file_object.close()

            except Exception:

                pass

        # 刪除暫存
        for file_path in temp_files:

            try:

                os.remove(
                    file_path
                )

            except OSError:

                pass


# =========================================================
# SEND DISCORD
# =========================================================

def send_discord(
    post,
    username
):

    caption = (
        post.caption
        or ""
    )

    # -----------------------------------------------------
    # Discord description 限制保守控制
    # -----------------------------------------------------

    if len(caption) > 1900:

        caption = (
            caption[:1900]
            + "\n\n..."
        )

    post_url = (
        "https://www.instagram.com/p/"
        + post.shortcode
        + "/"
    )

    media = get_post_media(
        post
    )

    print(
        f"[DISCORD] Preparing "
        f"{len(media)} media items."
    )

    # =====================================================
    # 沒有 media
    # =====================================================

    if not media:

        payload = {

            "embeds": [{

                "title":
                    f"📸 @{username}",

                "url":
                    post_url,

                "description":
                    caption,

                "color":
                    15467852,

                "footer": {
                    "text":
                        "Instagram Monitor"
                }

            }]

        }

        return discord_post(
            payload
        )

    # =====================================================
    # 第一批
    #
    # 主 Embed + 最多 9 個 media
    #
    # Discord embed 上限 10
    # =====================================================

    first_batch = media[:9]

    remaining = media[9:]

    main_embed = {

        "title":
            f"📸 @{username}",

        "url":
            post_url,

        "description":
            caption,

        "color":
            15467852,

        "footer": {
            "text":
                "Instagram Monitor"
        }

    }

    embeds = [
        main_embed
    ]

    attachments = []

    temp_files = []

    file_objects = []

    skipped_media = 0

    try:

        for item in first_batch:

            index = item["index"]

            media_type = item["type"]

            # -------------------------------------------------
            # Filename
            # -------------------------------------------------

            if media_type == "image":

                extension = ".jpg"

                filename = (
                    f"instagram_{index}.jpg"
                )

            else:

                extension = ".mp4"

                filename = (
                    f"instagram_{index}.mp4"
                )

            # -------------------------------------------------
            # Download
            # -------------------------------------------------

            file_path = download_file(
                item["url"],
                extension,
                "ig_"
            )

            if file_path is None:

                skipped_media += 1

                continue

            temp_files.append(
                file_path
            )

            # -------------------------------------------------
            # Open
            # -------------------------------------------------

            file_object = open(
                file_path,
                "rb"
            )

            file_objects.append(
                file_object
            )

            mime_type = (
                "video/mp4"
                if media_type == "video"
                else "image/jpeg"
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

            # -------------------------------------------------
            # Image
            # -------------------------------------------------

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

            # -------------------------------------------------
            # Video
            #
            # Discord 會直接顯示 MP4
            # -------------------------------------------------

        # -----------------------------------------------------
        # 大檔案提示
        # -----------------------------------------------------

        if skipped_media:

            warning = (
                "\n\n⚠️ "
                f"{skipped_media} "
                "media file(s) were too large "
                "or unavailable."
            )

            if main_embed["description"]:

                main_embed["description"] += (
                    warning
                )

            else:

                main_embed["description"] = (
                    warning
                )

        # -----------------------------------------------------
        # Send first message
        # -----------------------------------------------------

        if attachments:

            print(
                "[DISCORD] Sending first "
                f"message with "
                f"{len(attachments)} files."
            )

            success = discord_post(

                {
                    "embeds":
                        embeds[:10]
                },

                attachments

            )

        else:

            success = discord_post(

                {
                    "embeds": [
                        main_embed
                    ]
                }

            )

        if not success:

            return False

    finally:

        for file_object in file_objects:

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

    # =====================================================
    # 第 10 張
    #
    # Discord 再開一則 message
    # =====================================================

    if remaining:

        print(
            "[DISCORD] Remaining media: "
            f"{len(remaining)}"
        )

        for start in range(
            0,
            len(remaining),
            10
        ):

            batch = remaining[
                start:start + 10
            ]

            success = send_media_batch(
                batch,
                post_url
            )

            if not success:

                return False

    return True


# =========================================================
# CHECK ACCOUNT
# =========================================================

def check_account(
    username
):

    # -----------------------------------------------------
    # 429 cooldown
    # -----------------------------------------------------

    if is_rate_limited():

        return False

    try:

        print("")
        print(
            f"[{username}] "
            "Checking latest post..."
        )

        post = get_latest_post(
            username
        )

        if not post:

            print(
                f"[{username}] "
                "No posts."
            )

            return True

        shortcode = (
            post.shortcode
        )

        previous_shortcode = (
            STATE.get(
                username
            )
        )

        print(
            f"[{username}] "
            f"Latest: {shortcode}"
        )

        # =================================================
        # First Run
        # =================================================

        if previous_shortcode is None:

            print(
                f"[{username}] "
                "FIRST RUN."
            )

            print(
                f"[{username}] "
                "Saving current post "
                "without Discord notification."
            )

            STATE[username] = (
                shortcode
            )

            save_state(
                STATE
            )

            return True

        # =================================================
        # No New Post
        # =================================================

        if (
            previous_shortcode
            == shortcode
        ):

            print(
                f"[{username}] "
                "No new post."
            )

            return True

        # =================================================
        # NEW POST
        # =================================================

        print("")
        print(
            "=" * 60
        )

        print(
            f"🚨 [{username}] "
            f"NEW POST: "
            f"{shortcode}"
        )

        print(
            "=" * 60
        )
        print("")

        success = send_discord(
            post,
            username
        )

        # =================================================
        # Discord 成功
        # =================================================

        if success:

            STATE[username] = (
                shortcode
            )

            save_state(
                STATE
            )

            print(
                f"[{username}] "
                "Discord sent successfully."
            )

            print(
                f"[{username}] "
                "State updated."
            )

        # =================================================
        # Discord 失敗
        # =================================================

        else:

            print(
                f"[{username}] "
                "Discord failed."
            )

            print(
                f"[{username}] "
                "State NOT updated."
            )

        return True

    # =====================================================
    # INSTAGRAM 429
    # =====================================================

    except instaloader.exceptions.TooManyRequestsException as e:

        print("")
        print(
            f"[{username}] "
            "Instagram returned 429."
        )

        trigger_backoff(
            e
        )

        return False

    # =====================================================
    # CONNECTION ERROR
    # =====================================================

    except instaloader.exceptions.ConnectionException as e:

        error_text = str(e)

        print(
            f"[{username}] "
            "Instagram connection error:"
        )

        print(
            error_text
        )

        lower_error = (
            error_text.lower()
        )

        # -------------------------------------------------
        # 某些版本可能將 429 包在 ConnectionException
        # -------------------------------------------------

        if (
            "429"
            in error_text

            or
            "too many requests"
            in lower_error

            or
            "rate limit"
            in lower_error

        ):

            trigger_backoff(
                e
            )

            return False

        # 一般 connection error
        return True

    # =====================================================
    # PROFILE NOT EXISTS
    # =====================================================

    except instaloader.exceptions.ProfileNotExistsException:

        print(
            f"[{username}] "
            "Profile does not exist."
        )

        return True

    # =====================================================
    # PRIVATE / LOGIN
    # =====================================================

    except instaloader.exceptions.LoginRequiredException:

        print(
            f"[{username}] "
            "Instagram requires login."
        )

        print(
            f"[{username}] "
            "Skipping account."
        )

        return True

    # =====================================================
    # UNEXPECTED
    # =====================================================

    except Exception as e:

        print(
            f"[{username}] "
            f"Unexpected error: {e}"
        )

        return True


# =========================================================
# SHUTDOWN
# =========================================================

def handle_shutdown(
    signum,
    frame
):

    print("")
    print(
        "[SYSTEM] "
        "Shutdown signal received."
    )

    shutdown_event.set()


signal.signal(
    signal.SIGTERM,
    handle_shutdown
)

signal.signal(
    signal.SIGINT,
    handle_shutdown
)


# =========================================================
# MAIN
# =========================================================

def main():

    # =====================================================
    # CHECK ENV
    # =====================================================

    if not RAW_USERNAMES:

        print(
            "ERROR: "
            "IG_USERNAME is missing."
        )

        return

    if not WEBHOOK_URL:

        print(
            "ERROR: "
            "WEBHOOK_URL is missing."
        )

        return

    # =====================================================
    # Parse usernames
    # =====================================================

    usernames = [

        username.strip()

        for username
        in RAW_USERNAMES.split(",")

        if username.strip()

    ]

    if not usernames:

        print(
            "ERROR: "
            "No Instagram accounts configured."
        )

        return

    # =====================================================
    # Startup Information
    # =====================================================

    print("")
    print("=" * 70)

    print(
        "Instagram → Discord Monitor"
    )

    print("=" * 70)

    print(
        f"Accounts: "
        f"{len(usernames)}"
    )

    print(
        f"Check interval: "
        f"{TIME_INTERVAL}s "
        f"({TIME_INTERVAL / 3600:.1f} hours)"
    )

    print(
        f"Account delay: "
        f"{ACCOUNT_DELAY}s"
    )

    print(
        f"429 cooldown: "
        f"{COOLDOWN_ON_429}s"
    )

    print(
        f"Max 429 cooldown: "
        f"{MAX_COOLDOWN}s"
    )

    print(
        f"Startup delay: "
        f"{STARTUP_DELAY}s"
    )

    print(
        f"State file: "
        f"{STATE_FILE}"
    )

    print(
        f"Discord file limit: "
        f"{MAX_DISCORD_FILE_SIZE / 1024 / 1024:.2f} MB"
    )

    print("=" * 70)
    print("")

    # =====================================================
    # Health Server
    # =====================================================

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True
    )

    health_thread.start()

    # =====================================================
    # STARTUP DELAY
    # =====================================================

    if STARTUP_DELAY > 0:

        print(
            f"[STARTUP] "
            f"Waiting {STARTUP_DELAY} seconds "
            "before contacting Instagram..."
        )

        print(
            "[STARTUP] "
            "This prevents immediate "
            "Instagram requests after Render restart."
        )

        shutdown_event.wait(
            STARTUP_DELAY
        )

    if shutdown_event.is_set():

        return

    print(
        "[STARTUP] "
        "Startup cooldown finished."
    )

    print(
        "[STARTUP] "
        "Starting Instagram monitor."
    )

    # =====================================================
    # MAIN LOOP
    # =====================================================

    while not shutdown_event.is_set():

        # =================================================
        # 429 COOLDOWN
        # =================================================

        if is_rate_limited():

            remaining = int(
                rate_limit_until
                - time.time()
            )

            remaining = max(
                remaining,
                1
            )

            print(
                f"[RATE LIMIT] "
                f"Instagram cooldown "
                f"remaining: "
                f"{remaining}s"
            )

            # 最多每次睡 60 秒
            # 方便 Render shutdown
            shutdown_event.wait(
                min(
                    remaining,
                    60
                )
            )

            continue

        # =================================================
        # Random account order
        # =================================================

        cycle_usernames = list(
            usernames
        )

        random.shuffle(
            cycle_usernames
        )

        print("")
        print("=" * 70)

        print(
            "Starting new Instagram cycle."
        )

        print(
            "Account order: "
            + ", ".join(
                cycle_usernames
            )
        )

        print("=" * 70)
        print("")

        rate_limited = False

        # =================================================
        # Check accounts
        # =================================================

        for index, username in enumerate(
            cycle_usernames
        ):

            if shutdown_event.is_set():

                break

            # -------------------------------------------------
            # 429
            # -------------------------------------------------

            if is_rate_limited():

                rate_limited = True

                break

            # -------------------------------------------------
            # Check
            # -------------------------------------------------

            success = check_account(
                username
            )

            # -------------------------------------------------
            # 429 / Rate limit
            # -------------------------------------------------

            if not success:

                rate_limited = True

                print("")
                print(
                    "[CYCLE] Instagram "
                    "rate limited us."
                )

                print(
                    "[CYCLE] Stopping "
                    "the entire cycle."
                )

                print(
                    "[CYCLE] No more "
                    "Instagram requests "
                    "will be made."
                )

                print("")

                break

            # -------------------------------------------------
            # Account delay
            # -------------------------------------------------

            if (
                index
                < len(
                    cycle_usernames
                ) - 1
            ):

                print(
                    f"[CYCLE] Waiting "
                    f"{ACCOUNT_DELAY}s "
                    "before next account..."
                )

                shutdown_event.wait(
                    ACCOUNT_DELAY
                )

        # =================================================
        # Rate limited
        # =================================================

        if rate_limited:

            # 不 reset backoff
            # 下一輪進入 cooldown
            continue

        # =================================================
        # Successful full cycle
        # =================================================

        reset_backoff()

        print("")
        print("=" * 70)

        print(
            f"Finished checking "
            f"{len(usernames)} accounts."
        )

        print(
            f"No Instagram 429 "
            f"in this cycle."
        )

        print(
            f"Next cycle in "
            f"{TIME_INTERVAL}s "
            f"({TIME_INTERVAL / 3600:.1f} hours)."
        )

        print("=" * 70)
        print("")

        shutdown_event.wait(
            TIME_INTERVAL
        )

    # =====================================================
    # Shutdown
    # =====================================================

    print("")
    print(
        "[SYSTEM] "
        "Instagram Discord Monitor stopped."
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    main()
