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
# Environment Variables
# =========================================================

RAW_USERNAMES = os.environ.get("IG_USERNAME", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

# 正常輪詢間隔
# 建議免費方案先用 3600 = 1 小時
TIME_INTERVAL = int(
    os.environ.get("TIME_INTERVAL", "3600")
)

# 每個 IG 帳號之間的等待
ACCOUNT_DELAY = int(
    os.environ.get("ACCOUNT_DELAY", "30")
)

# 第一次遇到 429 的等待時間
INITIAL_BACKOFF = int(
    os.environ.get("INITIAL_BACKOFF", "900")
)

# 最大 429 冷卻時間
MAX_BACKOFF = int(
    os.environ.get("MAX_BACKOFF", "21600")
)

# State 檔案
STATE_FILE = os.environ.get(
    "STATE_FILE",
    "last_posts.json"
)

# Render Web Service Port
PORT = int(
    os.environ.get("PORT", "10000")
)

# Discord 免費/一般附件限制保守值
# 不使用官方上限的邊界值
MAX_DISCORD_FILE_SIZE = int(
    os.environ.get(
        "MAX_DISCORD_FILE_SIZE",
        "9500000"
    )
)

# =========================================================
# Global State
# =========================================================

backoff_seconds = INITIAL_BACKOFF
rate_limit_until = 0

shutdown_event = threading.Event()


# =========================================================
# HTTP Session
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
# Instaloader
# =========================================================

L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False
)


# =========================================================
# State
# =========================================================

def load_state():

    try:

        if not os.path.exists(STATE_FILE):
            return {}

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

            if isinstance(data, dict):
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

        parent = state_path.parent

        if str(parent) != ".":
            parent.mkdir(
                parents=True,
                exist_ok=True
            )

        temp_path = state_path.with_suffix(
            ".tmp"
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
# Health Check Server
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path in (
            "/",
            "/health",
            "/healthz"
        ):

            body = b"Instagram Discord Monitor OK"

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(body)

        else:

            self.send_response(404)

            self.end_headers()

    def log_message(
        self,
        format,
        *args
    ):
        # 避免 Render Logs 被 health check 洗版
        return


def start_health_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
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


# =========================================================
# Rate Limit
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

    # seconds
    second_patterns = [

        r"wait\s+(\d+)\s+seconds",

        r"(\d+)\s+seconds",

        r"try again in\s+(\d+)"
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

    # minutes
    minute_patterns = [

        r"wait\s+(\d+)\s+minutes",

        r"(\d+)\s+minutes",

        r"try again in\s+(\d+)\s+minutes"
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
                    int(match.group(1))
                    * 60
                )

            except ValueError:
                pass

    return None


def trigger_backoff(
    error=None
):

    global backoff_seconds
    global rate_limit_until

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

    if instagram_wait:

        wait_time = (
            instagram_wait
            + 30
        )

    else:

        jitter = random.randint(
            30,
            120
        )

        wait_time = (
            backoff_seconds
            + jitter
        )

    wait_time = min(
        wait_time,
        MAX_BACKOFF
    )

    rate_limit_until = (
        time.time()
        + wait_time
    )

    print("")
    print("=" * 60)
    print("🚨 INSTAGRAM RATE LIMIT")
    print(
        f"Cooldown: {wait_time} seconds"
    )
    print(
        "Resume around: "
        f"{time.ctime(rate_limit_until)}"
    )
    print("=" * 60)
    print("")

    backoff_seconds = min(
        backoff_seconds * 2,
        MAX_BACKOFF
    )


def reset_backoff():

    global backoff_seconds

    backoff_seconds = (
        INITIAL_BACKOFF
    )


# =========================================================
# Instagram
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
# Media
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

                if (
                    total_size
                    > MAX_DISCORD_FILE_SIZE
                ):

                    print(
                        "[MEDIA] File exceeds "
                        "Discord upload limit."
                    )

                    # 重要：
                    # 超過大小時立即刪除
                    try:
                        os.remove(
                            temp_path
                        )
                    except OSError:
                        pass

                    return None

                f.write(chunk)

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
# Get Post Media
# =========================================================

def get_post_media(
    post
):

    media = []

    # -----------------------------------------------------
    # Carousel
    # -----------------------------------------------------

    if post.typename == "GraphSidecar":

        try:

            children = list(
                post.get_sidecar_nodes()
            )

            print(
                f"[MEDIA] Carousel: "
                f"{len(children)} items"
            )

            for index, child in enumerate(
                children,
                start=1
            ):

                if child.is_video:

                    if child.video_url:

                        media.append({
                            "type": "video",
                            "url": child.video_url,
                            "index": index
                        })

                else:

                    if child.display_url:

                        media.append({
                            "type": "image",
                            "url": child.display_url,
                            "index": index
                        })

        except Exception as e:

            print(
                "[MEDIA] Carousel error: "
                f"{e}"
            )

    # -----------------------------------------------------
    # Single Video
    # -----------------------------------------------------

    elif post.is_video:

        if post.video_url:

            media.append({
                "type": "video",
                "url": post.video_url,
                "index": 1
            })

    # -----------------------------------------------------
    # Single Image
    # -----------------------------------------------------

    else:

        if post.url:

            media.append({
                "type": "image",
                "url": post.url,
                "index": 1
            })

    return media[:10]


# =========================================================
# Discord Helpers
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
                    "payload_json": json.dumps(
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
# Send Media Batch
# =========================================================

def send_media_batch(
    media_items,
    post_url
):

    """
    一次最多處理 9 個媒體。

    第一個 message 會有 caption。
    """

    if not media_items:

        return True

    embeds = []

    attachments = []

    temp_files = []

    file_objects = []

    failed_media = []

    try:

        for item in media_items:

            index = item["index"]

            media_type = item["type"]

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

            file_path = download_file(
                item["url"],
                extension,
                "ig_"
            )

            if file_path is None:

                failed_media.append(
                    item
                )

                continue

            temp_files.append(
                file_path
            )

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

            # -----------------------------------------
            # Image
            # -----------------------------------------

            if media_type == "image":

                embeds.append({

                    "url": post_url,

                    "image": {
                        "url":
                            "attachment://"
                            + filename
                    },

                    "color":
                        15467852

                })

            # -----------------------------------------
            # Video
            # -----------------------------------------

            else:

                # Discord 對影片附件會直接顯示
                # 不使用 image embed
                pass

        if not attachments:

            return False

        payload = {
            "embeds": embeds[:10]
        }

        print(
            "[DISCORD] Sending "
            f"{len(attachments)} media..."
        )

        success = discord_post(
            payload,
            attachments
        )

        return success

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


# =========================================================
# Send Caption / Notification
# =========================================================

def send_discord(
    post,
    username
):

    caption = post.caption or ""

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
        f"[DISCORD] "
        f"{len(media)} media items."
    )

    # =====================================================
    # Case 1: No media
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
    # Case 2: Images / Videos
    # =====================================================

    # Discord webhook 一個 message 不塞超過 9 個媒體
    first_batch = media[:9]

    remaining = media[9:]

    # -----------------------------------------------------
    # Caption embed
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # Prepare first batch
    # -----------------------------------------------------

    embeds = [
        main_embed
    ]

    attachments = []

    temp_files = []

    file_objects = []

    skipped_large = []

    try:

        for item in first_batch:

            index = item["index"]

            media_type = item["type"]

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

            file_path = download_file(
                item["url"],
                extension,
                "ig_"
            )

            if file_path is None:

                skipped_large.append(
                    item
                )

                continue

            temp_files.append(
                file_path
            )

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

            # Photo Embed
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

    except Exception as e:

        print(
            "[DISCORD] Media preparation "
            f"error: {e}"
        )

        return False

    # -----------------------------------------------------
    # Add skipped media information
    # -----------------------------------------------------

    if skipped_large:

        text = "\n\n⚠️ "

        text += (
            f"{len(skipped_large)} "
            "video/media file(s) "
            "were too large for Discord."
        )

        main_embed["description"] = (
            caption
            + text
        )

    # -----------------------------------------------------
    # Send first message
    # -----------------------------------------------------

    try:

        if attachments:

            print(
                "[DISCORD] Sending first "
                f"message with "
                f"{len(attachments)} files."
            )

            success = discord_post(
                {
                    "embeds": embeds[:10]
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
    # Remaining media
    # =====================================================

    if remaining:

        print(
            "[DISCORD] Sending "
            f"remaining "
            f"{len(remaining)} media."
        )

        # 每個額外 message 最多 10 個附件
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
# Check Account
# =========================================================

def check_account(
    username
):

    if is_rate_limited():
        return False

    try:

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

        shortcode = post.shortcode

        previous_shortcode = (
            STATE.get(username)
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
                "First run."
            )

            print(
                f"[{username}] "
                "Saving current post "
                "without notification."
            )

            STATE[username] = shortcode

            save_state(
                STATE
            )

            return True

        # =================================================
        # No New Post
        # =================================================

        if previous_shortcode == shortcode:

            print(
                f"[{username}] "
                "No new post."
            )

            return True

        # =================================================
        # New Post
        # =================================================

        print("")
        print(
            f"🚨 [{username}] "
            f"NEW POST: {shortcode}"
        )
        print("")

        success = send_discord(
            post,
            username
        )

        if success:

            STATE[username] = (
                shortcode
            )

            save_state(
                STATE
            )

            print(
                f"[{username}] "
                "State updated."
            )

        else:

            print(
                f"[{username}] "
                "Discord failed. "
                "State NOT updated."
            )

        return True

    # =====================================================
    # 429
    # =====================================================

    except instaloader.exceptions.TooManyRequestsException as e:

        print(
            f"[{username}] "
            "Instagram 429."
        )

        trigger_backoff(
            e
        )

        return False

    # =====================================================
    # Connection Error
    # =====================================================

    except instaloader.exceptions.ConnectionException as e:

        error_text = str(e)

        print(
            f"[{username}] "
            "Instagram connection error: "
            f"{error_text}"
        )

        lower_error = (
            error_text.lower()
        )

        if (
            "429" in error_text
            or "too many requests"
            in lower_error
            or "rate limit"
            in lower_error
        ):

            trigger_backoff(
                e
            )

            return False

        return True

    # =====================================================
    # Profile Not Exists
    # =====================================================

    except instaloader.exceptions.ProfileNotExistsException:

        print(
            f"[{username}] "
            "Profile does not exist."
        )

        return True

    # =====================================================
    # Unexpected
    # =====================================================

    except Exception as e:

        print(
            f"[{username}] "
            f"Unexpected error: {e}"
        )

        return True


# =========================================================
# Shutdown
# =========================================================

def handle_shutdown(
    signum,
    frame
):

    print(
        "[SYSTEM] Shutdown signal received."
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
# Main
# =========================================================

def main():

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

    usernames = [

        username.strip()

        for username
        in RAW_USERNAMES.split(",")

        if username.strip()

    ]

    print("=" * 60)

    print(
        "Instagram → Discord Monitor"
    )

    print(
        f"Accounts: "
        f"{len(usernames)}"
    )

    print(
        f"Check interval: "
        f"{TIME_INTERVAL}s"
    )

    print(
        f"Account delay: "
        f"{ACCOUNT_DELAY}s"
    )

    print(
        f"Initial backoff: "
        f"{INITIAL_BACKOFF}s"
    )

    print(
        f"Max backoff: "
        f"{MAX_BACKOFF}s"
    )

    print(
        f"State file: "
        f"{STATE_FILE}"
    )

    print("=" * 60)

    # -----------------------------------------------------
    # Health Server
    # -----------------------------------------------------

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True
    )

    health_thread.start()

    # -----------------------------------------------------
    # Main Loop
    # -----------------------------------------------------

    while not shutdown_event.is_set():

        # ================================================
        # Rate Limited
        # ================================================

        if is_rate_limited():

            remaining = int(
                rate_limit_until
                - time.time()
            )

            print(
                "[RATE LIMIT] "
                f"Still cooling down: "
                f"{remaining}s"
            )

            shutdown_event.wait(
                min(
                    max(remaining, 1),
                    60
                )
            )

            continue

        rate_limited = False

        # ================================================
        # Check accounts
        # ================================================

        for index, username in enumerate(
            usernames
        ):

            if shutdown_event.is_set():
                break

            if is_rate_limited():

                rate_limited = True

                break

            success = check_account(
                username
            )

            # ---------------------------------------------
            # 429
            # ---------------------------------------------

            if not success:

                rate_limited = True

                print(
                    "Stopping current cycle "
                    "because Instagram "
                    "rate limited us."
                )

                break

            # ---------------------------------------------
            # Account delay
            # ---------------------------------------------

            if (
                index
                < len(usernames) - 1
            ):

                shutdown_event.wait(
                    ACCOUNT_DELAY
                )

        # ================================================
        # Rate limited
        # ================================================

        if rate_limited:

            continue

        # ================================================
        # Successful full cycle
        # ================================================

        reset_backoff()

        print("")
        print(
            f"--- Finished checking "
            f"{len(usernames)} accounts ---"
        )

        print(
            f"--- Sleeping "
            f"{TIME_INTERVAL} seconds ---"
        )

        print("")

        shutdown_event.wait(
            TIME_INTERVAL
        )

    print(
        "[SYSTEM] "
        "Monitor stopped."
    )


# =========================================================
# Start
# =========================================================

if __name__ == "__main__":

    main()
