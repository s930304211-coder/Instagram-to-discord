```python
#!/usr/bin/env python3

import os
import re
import time
import json
import random
import tempfile
import signal
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests
import instaloader


# ============================================================
# Environment Variables
# ============================================================

RAW_USERNAMES = os.environ.get("IG_USERNAME", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

# 每一輪檢查完所有帳號後等待多久
# Render 建議：7200 = 2 小時
TIME_INTERVAL = int(
    os.environ.get("TIME_INTERVAL", "7200")
)

# 帳號與帳號之間的等待
ACCOUNT_DELAY = int(
    os.environ.get("ACCOUNT_DELAY", "120")
)

# 每次帳號間延遲加入 0 ~ ACCOUNT_JITTER 秒隨機值
ACCOUNT_JITTER = int(
    os.environ.get("ACCOUNT_JITTER", "30")
)

# Render 啟動後先不要馬上碰 Instagram
STARTUP_COOLDOWN = int(
    os.environ.get("STARTUP_COOLDOWN", "900")
)

# 429 預設冷卻時間：20 分鐘
INITIAL_BACKOFF = int(
    os.environ.get("INITIAL_BACKOFF", "1200")
)

# 429 最大冷卻時間：6 小時
MAX_BACKOFF = int(
    os.environ.get("MAX_BACKOFF", "21600")
)

# State file
STATE_FILE = os.environ.get(
    "STATE_FILE",
    "last_posts.json"
)

# Discord 單檔案保守限制：約 9.5 MB
MAX_DISCORD_FILE_SIZE = int(
    os.environ.get(
        "MAX_DISCORD_FILE_SIZE",
        "9500000"
    )
)

# Discord 一次 Webhook 上傳總大小：約 9 MB
MAX_DISCORD_TOTAL_SIZE = int(
    os.environ.get(
        "MAX_DISCORD_TOTAL_SIZE",
        "9000000"
    )
)

# 每篇 Instagram 貼文最多處理 10 個 media
MAX_MEDIA = 10


# ============================================================
# Global State
# ============================================================

backoff_seconds = INITIAL_BACKOFF
rate_limit_until = 0

shutdown_requested = False


# ============================================================
# HTTP Health Check Server for Render
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"OK - Instagram Discord Monitor Running"
        )

    def log_message(self, format, *args):
        # 避免 Health Check 一直刷 Render Logs
        pass


def start_health_server():

    # Render 會提供 PORT
    # 沒有 PORT 時 fallback 到 10000
    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    server = None

    try:

        server = HTTPServer(
            ("0.0.0.0", port),
            HealthCheckHandler
        )

        print(
            f"[SYSTEM] Health server "
            f"listening on port {port}"
        )

        server.serve_forever()

    except Exception as e:

        print(
            f"[SYSTEM] Health server error: {e}"
        )

    finally:

        if server is not None:

            try:
                server.server_close()

            except Exception:
                pass


# ============================================================
# Signal Handling
# ============================================================

def handle_shutdown(signum, frame):

    global shutdown_requested

    shutdown_requested = True

    print("")
    print("=" * 70)
    print("[SYSTEM] Shutdown signal received.")
    print("[SYSTEM] Finishing current operation safely.")
    print("=" * 70)


signal.signal(
    signal.SIGTERM,
    handle_shutdown
)

signal.signal(
    signal.SIGINT,
    handle_shutdown
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
        "Chrome/151.0.0.0 Safari/537.36"
    ),

    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,"
        "image/webp,*/*;q=0.8"
    ),

    "Accept-Language":
        "en-US,en;q=0.9",
})


# ============================================================
# Instaloader
# ============================================================

L = instaloader.Instaloader(

    # 不讓 Instaloader 自己下載圖片/影片
    # 媒體由 requests 直接下載
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,

    download_geotags=False,
    download_comments=False,

    save_metadata=False,
    compress_json=False,

    post_metadata_txt_pattern="",
)


# ============================================================
# State
# ============================================================

def load_state():

    try:

        if not os.path.exists(STATE_FILE):

            print(
                "[STATE] No state file found."
            )

            return {}

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        if not isinstance(state, dict):

            print(
                "[STATE] Invalid state format."
            )

            return {}

        print(
            f"[STATE] Loaded "
            f"{len(state)} account states."
        )

        return state

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

        state_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        # Atomic write
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


# ============================================================
# Rate Limit
# ============================================================

def is_rate_limited():

    return (
        time.time()
        <
        rate_limit_until
    )


def extract_wait_seconds(error_text):

    if not error_text:
        return None

    patterns = [

        # retry in 666 seconds
        r"retry.*?(\d+)\s+seconds",

        # wait 666 seconds
        r"wait.*?(\d+)\s+seconds",

        # 666 seconds
        r"(\d+)\s+seconds",

        # after 666
        r"after\s+(\d+)",

    ]

    for pattern in patterns:

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

    return None


def trigger_backoff(error=None):

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

    # --------------------------------------------------------
    # Instagram 明確告訴我們等待時間
    # --------------------------------------------------------

    if instagram_wait:

        # 多加 30 秒 buffer
        wait_time = (
            instagram_wait
            + 30
        )

    else:

        # 沒有明確時間
        jitter = random.randint(
            30,
            120
        )

        wait_time = (
            backoff_seconds
            + jitter
        )

    # 最大不超過 MAX_BACKOFF
    wait_time = min(
        wait_time,
        MAX_BACKOFF
    )

    rate_limit_until = (
        time.time()
        + wait_time
    )

    print("")
    print("=" * 70)
    print("🚨 INSTAGRAM RATE LIMIT DETECTED")
    print("=" * 70)

    print(
        f"Cooldown: "
        f"{wait_time} seconds"
    )

    print(
        f"Cooldown minutes: "
        f"{wait_time / 60:.1f}"
    )

    print(
        "NO INSTAGRAM REQUESTS "
        "WILL BE MADE DURING THIS PERIOD."
    )

    print(
        "Resume around:",
        time.ctime(
            rate_limit_until
        )
    )

    print("=" * 70)
    print("")

    # Exponential backoff
    backoff_seconds = min(
        backoff_seconds * 2,
        MAX_BACKOFF
    )


def reset_backoff():

    global backoff_seconds

    backoff_seconds = (
        INITIAL_BACKOFF
    )


def wait_for_rate_limit():

    while (
        is_rate_limited()
        and not shutdown_requested
    ):

        remaining = int(
            rate_limit_until
            - time.time()
        )

        if remaining <= 0:
            break

        print(
            f"[RATE LIMIT] "
            f"Cooling down... "
            f"{remaining}s remaining."
        )

        # 每次最多睡 60 秒
        time.sleep(
            min(
                remaining,
                60
            )
        )


# ============================================================
# Instagram
# ============================================================

def get_latest_post(username):

    print(
        f"[{username}] "
        "Loading profile..."
    )

    profile = (
        instaloader.Profile
        .from_username(
            L.context,
            username
        )
    )

    print(
        f"[{username}] "
        "Reading latest post..."
    )

    posts = profile.get_posts()

    latest_post = next(
        posts,
        None
    )

    return latest_post


# ============================================================
# Media Download
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

        print(
            f"[MEDIA] Downloading "
            f"{extension}..."
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
                chunk_size=1024 * 128
            ):

                if not chunk:
                    continue

                total_size += len(chunk)

                # 單檔案限制
                if (
                    total_size
                    >
                    MAX_DISCORD_FILE_SIZE
                ):

                    print(
                        "[MEDIA] File exceeds "
                        "Discord file limit."
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
            f"[MEDIA] Downloaded "
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


# ============================================================
# Build Instagram Media List
# ============================================================

def get_post_media(post):

    media = []

    try:

        # ----------------------------------------------------
        # Carousel
        # ----------------------------------------------------

        if post.typename == "GraphSidecar":

            print(
                "[MEDIA] "
                "Instagram Carousel detected."
            )

            children = list(
                post.get_sidecar_nodes()
            )

            print(
                f"[MEDIA] Carousel contains "
                f"{len(children)} items."
            )

            for index, child in enumerate(
                children[:MAX_MEDIA],
                start=1
            ):

                try:

                    # Video
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

                    # Image
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
                        f"[MEDIA] "
                        f"Could not read "
                        f"carousel item "
                        f"#{index}: {e}"
                    )

        # ----------------------------------------------------
        # Single Video
        # ----------------------------------------------------

        elif post.is_video:

            print(
                "[MEDIA] "
                "Single video detected."
            )

            if post.video_url:

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

        else:

            print(
                "[MEDIA] "
                "Single image detected."
            )

            if post.url:

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
            f"[MEDIA] "
            f"Media extraction error: {e}"
        )

    return media[:MAX_MEDIA]


# ============================================================
# Discord
# ============================================================

def send_discord(
    post,
    username
):

    caption = (
        post.caption
        or ""
    )

    # Discord Embed description max 4096
    # 保守使用 1900
    if len(caption) > 1900:

        caption = (
            caption[:1900]
            + "\n\n..."
        )

    post_url = (
        f"https://www.instagram.com/p/"
        f"{post.shortcode}/"
    )

    media = get_post_media(
        post
    )

    print(
        f"[DISCORD] "
        f"Preparing "
        f"{len(media)} media items."
    )

    temp_files = []
    file_handles = []
    attachments = []

    total_download_size = 0

    try:

        embeds = []

        # ----------------------------------------------------
        # Main Embed
        # ----------------------------------------------------

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

        embeds.append(
            main_embed
        )

        # ----------------------------------------------------
        # Download Media
        # ----------------------------------------------------

        for item in media:

            if shutdown_requested:

                print(
                    "[DISCORD] "
                    "Shutdown requested."
                )

                return False

            index = item["index"]

            media_type = item["type"]

            if media_type == "video":

                extension = ".mp4"

            else:

                extension = ".jpg"

            filename = (
                f"instagram_{index}"
                f"{extension}"
            )

            file_path = download_file(
                item["url"],
                extension,
                "ig_"
            )

            if file_path is None:

                print(
                    f"[DISCORD] "
                    f"Skipping media #{index}"
                )

                continue

            # ------------------------------------------------
            # Check downloaded size
            # ------------------------------------------------

            try:

                file_size = os.path.getsize(
                    file_path
                )

            except OSError:

                file_size = 0

            # ------------------------------------------------
            # Total upload limit
            # ------------------------------------------------

            if (
                total_download_size
                + file_size
                >
                MAX_DISCORD_TOTAL_SIZE
            ):

                print(
                    "[DISCORD] "
                    "Total upload size would "
                    "exceed safe limit."
                )

                try:

                    os.remove(
                        file_path
                    )

                except OSError:
                    pass

                break

            total_download_size += (
                file_size
            )

            temp_files.append(
                file_path
            )

            # ------------------------------------------------
            # Open file
            # ------------------------------------------------

            f_obj = open(
                file_path,
                "rb"
            )

            file_handles.append(
                f_obj
            )

            if media_type == "video":

                mime_type = (
                    "video/mp4"
                )

            else:

                mime_type = (
                    "image/jpeg"
                )

            attachments.append(
                (
                    "files[]",
                    (
                        filename,
                        f_obj,
                        mime_type
                    )
                )
            )

            # ------------------------------------------------
            # Embed attachment
            # ------------------------------------------------

            # Discord Embed 的 image 可以顯示圖片附件。
            # 對影片仍然保留 attachment，
            # 讓 Discord 收到原始 MP4。
            if media_type == "image":

                embeds.append({

                    "url":
                        post_url,

                    "image": {
                        "url":
                            f"attachment://"
                            f"{filename}"
                    },

                    "color":
                        15467852
                })

        # Discord 最多 10 embeds
        embeds = embeds[:10]

        # ----------------------------------------------------
        # Payload
        # ----------------------------------------------------

        payload_json = {

            "embeds":
                embeds

        }

        payload = {

            "payload_json":
                json.dumps(
                    payload_json,
                    ensure_ascii=False
                )

        }

        print(
            f"[DISCORD] "
            f"Uploading "
            f"{len(attachments)} files..."
        )

        print(
            f"[DISCORD] "
            f"Total size: "
            f"{total_download_size / 1024 / 1024:.2f} MB"
        )

        # ----------------------------------------------------
        # Discord Request
        # ----------------------------------------------------

        response = HTTP.post(

            WEBHOOK_URL,

            data=payload,

            files=attachments,

            timeout=180

        )

        # ----------------------------------------------------
        # Discord Rate Limit
        # ----------------------------------------------------

        if response.status_code == 429:

            print(
                "[DISCORD] "
                "Discord webhook rate limited."
            )

            try:

                retry_data = (
                    response.json()
                )

                retry_after = float(
                    retry_data.get(
                        "retry_after",
                        10
                    )
                )

            except Exception:

                retry_after = 10

            print(
                f"[DISCORD] "
                f"Retry after "
                f"{retry_after}s."
            )

            # Discord 限流不是 Instagram
            time.sleep(
                min(
                    retry_after + 2,
                    120
                )
            )

            return False

        response.raise_for_status()

        print(
            "[DISCORD] "
            "Successfully sent."
        )

        return True

    except requests.exceptions.HTTPError as e:

        print(
            f"[DISCORD] "
            f"HTTP error: {e}"
        )

        if e.response is not None:

            print(
                "[DISCORD] "
                f"Response: "
                f"{e.response.text[:1000]}"
            )

        return False

    except Exception as e:

        print(
            f"[DISCORD] "
            f"Error: {e}"
        )

        return False

    finally:

        # ----------------------------------------------------
        # Close file handles
        # ----------------------------------------------------

        for f_obj in file_handles:

            try:

                f_obj.close()

            except Exception:
                pass

        # ----------------------------------------------------
        # Remove temp files
        # ----------------------------------------------------

        for file_path in temp_files:

            try:

                os.remove(
                    file_path
                )

            except OSError:
                pass


# ============================================================
# Detect Instagram 429
# ============================================================

def is_instagram_rate_limit_error(
    error
):

    error_text = str(
        error
    ).lower()

    rate_limit_keywords = [

        "429",

        "too many requests",

        "rate limit",

        "rate_limit",

        "please wait",

        "retry in",

    ]

    return any(
        keyword in error_text
        for keyword
        in rate_limit_keywords
    )


# ============================================================
# Check Account
# ============================================================

def check_account(
    username
):

    if shutdown_requested:

        return False

    # 如果已經被 429
    # 絕對不要再碰 Instagram
    if is_rate_limited():

        print(
            f"[{username}] "
            "Skipped because "
            "Instagram is cooling down."
        )

        return False

    try:

        print("")
        print(
            f"[{username}] "
            "Checking latest post..."
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
                "No posts found."
            )

            return True

        shortcode = (
            post.shortcode
        )

        previous_shortcode = (
            STATE.get(username)
        )

        print(
            f"[{username}] "
            f"Latest post: "
            f"{shortcode}"
        )

        # ----------------------------------------------------
        # First Run
        # ----------------------------------------------------

        if previous_shortcode is None:

            print(
                f"[{username}] "
                "First run."
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

        # ----------------------------------------------------
        # No New Post
        # ----------------------------------------------------

        if (
            previous_shortcode
            ==
            shortcode
        ):

            print(
                f"[{username}] "
                "No new post."
            )

            return True

        # ----------------------------------------------------
        # New Post
        # ----------------------------------------------------

        print("")
        print(
            "=" * 70
        )

        print(
            f"🚨 [{username}] "
            f"NEW POST: "
            f"{shortcode}"
        )

        print(
            "=" * 70
        )

        # ----------------------------------------------------
        # Send Discord
        # ----------------------------------------------------

        success = send_discord(
            post,
            username
        )

        # ----------------------------------------------------
        # ONLY update state after Discord success
        # ----------------------------------------------------

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
                "Discord notification failed."
            )

            print(
                f"[{username}] "
                "State NOT updated."
            )

        return True

    # ========================================================
    # Explicit Instaloader 429
    # ========================================================

    except (
        instaloader.exceptions
        .TooManyRequestsException
    ) as e:

        print("")
        print(
            f"[{username}] "
            "Instagram 429 detected."
        )

        print(
            f"[{username}] "
            f"Error: {e}"
        )

        trigger_backoff(
            e
        )

        return False

    # ========================================================
    # Connection Exception
    # ========================================================

    except (
        instaloader.exceptions
        .ConnectionException
    ) as e:

        error_text = str(e)

        print(
            f"[{username}] "
            "Instagram connection error:"
        )

        print(
            error_text
        )

        # Instagram 429 有時會被 Instaloader
        # 包裝成 ConnectionException
        if is_instagram_rate_limit_error(
            e
        ):

            print("")
            print(
                f"[{username}] "
                "Instagram 429 detected "
                "inside ConnectionException."
            )

            trigger_backoff(
                e
            )

            return False

        return True

    # ========================================================
    # Profile does not exist
    # ========================================================

    except (
        instaloader.exceptions
        .ProfileNotExistsException
    ):

        print(
            f"[{username}] "
            "Profile does not exist."
        )

        return True

    # ========================================================
    # Other Instaloader errors
    # ========================================================

    except (
        instaloader.exceptions
        .InstaloaderException
    ) as e:

        error_text = str(e)

        print(
            f"[{username}] "
            f"Instaloader error: "
            f"{error_text}"
        )

        if is_instagram_rate_limit_error(
            e
        ):

            print(
                f"[{username}] "
                "Rate limit detected "
                "inside InstaloaderException."
            )

            trigger_backoff(
                e
            )

            return False

        return True

    # ========================================================
    # Generic Exception
    # ========================================================

    except Exception as e:

        error_text = str(e)

        print(
            f"[{username}] "
            "Unexpected error:"
        )

        print(
            error_text
        )

        # Instagram 429 有時會落到 generic Exception
        if is_instagram_rate_limit_error(
            e
        ):

            print("")
            print(
                f"[{username}] "
                "Instagram 429 detected "
                "inside generic exception."
            )

            trigger_backoff(
                e
            )

            return False

        return True


# ============================================================
# Startup Cooldown
# ============================================================

def startup_cooldown():

    if STARTUP_COOLDOWN <= 0:

        return

    print("")
    print("=" * 70)

    print(
        "[STARTUP] "
        "Render restart detected."
    )

    print(
        f"[STARTUP] "
        f"Waiting "
        f"{STARTUP_COOLDOWN}s "
        f"before Instagram requests."
    )

    print(
        "[STARTUP] "
        "This prevents immediate requests "
        "after a Render restart."
    )

    print("=" * 70)
    print("")

    remaining = (
        STARTUP_COOLDOWN
    )

    while (
        remaining > 0
        and not shutdown_requested
    ):

        print(
            f"[STARTUP] "
            f"Cooldown remaining: "
            f"{remaining}s"
        )

        sleep_time = min(
            remaining,
            60
        )

        time.sleep(
            sleep_time
        )

        remaining -= (
            sleep_time
        )

    print(
        "[STARTUP] "
        "Startup cooldown finished."
    )


# ============================================================
# Main Loop
# ============================================================

def main():

    # --------------------------------------------------------
    # Start HTTP Health Check Server
    # --------------------------------------------------------

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True
    )

    health_thread.start()

    print("")
    print("=" * 70)
    print(
        "Instagram → Discord Monitor"
    )
    print("=" * 70)

    # --------------------------------------------------------
    # Environment Check
    # --------------------------------------------------------

    if not RAW_USERNAMES:

        print(
            "[ERROR] "
            "IG_USERNAME is missing."
        )

        return

    if not WEBHOOK_URL:

        print(
            "[ERROR] "
            "WEBHOOK_URL is missing."
        )

        return

    # --------------------------------------------------------
    # Parse usernames
    # --------------------------------------------------------

    usernames = [

        username.strip()

        for username
        in RAW_USERNAMES.split(",")

        if username.strip()

    ]

    if not usernames:

        print(
            "[ERROR] "
            "No Instagram accounts configured."
        )

        return

    # --------------------------------------------------------
    # Configuration
    # --------------------------------------------------------

    print(
        f"Accounts: "
        f"{len(usernames)}"
    )

    print(
        f"Check interval: "
        f"{TIME_INTERVAL}s "
        f"({TIME_INTERVAL / 3600:.2f} hours)"
    )

    print(
        f"Account delay: "
        f"{ACCOUNT_DELAY}s"
    )

    print(
        f"Account jitter: "
        f"0 ~ {ACCOUNT_JITTER}s"
    )

    print(
        f"Startup cooldown: "
        f"{STARTUP_COOLDOWN}s"
    )

    print(
        f"Initial 429 backoff: "
        f"{INITIAL_BACKOFF}s"
    )

    print(
        f"Maximum 429 backoff: "
        f"{MAX_BACKOFF}s"
    )

    print(
        f"State file: "
        f"{STATE_FILE}"
    )

    print("=" * 70)

    # --------------------------------------------------------
    # Startup cooldown
    # --------------------------------------------------------

    startup_cooldown()

    if shutdown_requested:

        print(
            "[SYSTEM] "
            "Shutdown during startup."
        )

        return

    print(
        "[STARTUP] "
        "Starting Instagram monitor."
    )

    # ========================================================
    # Main Loop
    # ========================================================

    while not shutdown_requested:

        # ----------------------------------------------------
        # Wait if currently rate limited
        # ----------------------------------------------------

        if is_rate_limited():

            wait_for_rate_limit()

            if shutdown_requested:
                break

            print(
                "[RATE LIMIT] "
                "Cooldown finished."
            )

            continue

        # ----------------------------------------------------
        # Randomize account order
        # ----------------------------------------------------

        cycle_usernames = list(
            usernames
        )

        random.shuffle(
            cycle_usernames
        )

        print("")
        print("=" * 70)

        print(
            "[CYCLE] "
            "Starting new Instagram cycle."
        )

        print(
            f"[CYCLE] "
            f"Account order: "
            f"{', '.join(cycle_usernames)}"
        )

        print("=" * 70)
        print("")

        cycle_rate_limited = False

        checked_count = 0

        # ----------------------------------------------------
        # Check every account
        # ----------------------------------------------------

        for index, username in enumerate(
            cycle_usernames
        ):

            if shutdown_requested:
                break

            # 429 happened
            if is_rate_limited():

                cycle_rate_limited = True

                print(
                    "[CYCLE] "
                    "Instagram is rate limited."
                )

                print(
                    "[CYCLE] "
                    "Stopping current cycle."
                )

                break

            # ------------------------------------------------
            # Check account
            # ------------------------------------------------

            success = check_account(
                username
            )

            checked_count += 1

            # ------------------------------------------------
            # 429 / rate limit
            # ------------------------------------------------

            if not success:

                if is_rate_limited():

                    cycle_rate_limited = True

                    print("")
                    print(
                        "[CYCLE] "
                        "Instagram 429 detected."
                    )

                    print(
                        "[CYCLE] "
                        "Stopping all further "
                        "Instagram requests."
                    )

                    break

            # ------------------------------------------------
            # Account delay
            # ------------------------------------------------

            if (
                index
                <
                len(cycle_usernames) - 1
                and not shutdown_requested
            ):

                jitter = random.randint(
                    0,
                    ACCOUNT_JITTER
                )

                delay = (
                    ACCOUNT_DELAY
                    + jitter
                )

                print(
                    f"[CYCLE] "
                    f"Waiting "
                    f"{delay}s "
                    f"before next account..."
                )

                time.sleep(
                    delay
                )

        # ----------------------------------------------------
        # Rate limited
        # ----------------------------------------------------

        if cycle_rate_limited:

            print("")
            print("=" * 70)

            print(
                "[CYCLE] "
                "Instagram rate limit active."
            )

            print(
                "[CYCLE] "
                "No more Instagram requests "
                "will be made."
            )

            print("=" * 70)

            wait_for_rate_limit()

            continue

        # ----------------------------------------------------
        # Shutdown
        # ----------------------------------------------------

        if shutdown_requested:

            break

        # ----------------------------------------------------
        # Successful cycle
        # ----------------------------------------------------

        reset_backoff()

        print("")
        print("=" * 70)

        print(
            f"[CYCLE] "
            f"Finished checking "
            f"{checked_count}/"
            f"{len(cycle_usernames)} accounts."
        )

        print(
            "[CYCLE] "
            "No Instagram 429 in this cycle."
        )

        print(
            f"[CYCLE] "
            f"Next cycle in "
            f"{TIME_INTERVAL}s "
            f"({TIME_INTERVAL / 3600:.2f} hours)."
        )

        print("=" * 70)
        print("")

        # ----------------------------------------------------
        # Sleep until next cycle
        # ----------------------------------------------------

        remaining = (
            TIME_INTERVAL
        )

        while (
            remaining > 0
            and not shutdown_requested
        ):

            sleep_time = min(
                remaining,
                60
            )

            time.sleep(
                sleep_time
            )

            remaining -= (
                sleep_time
            )

    # ========================================================
    # Shutdown
    # ========================================================

    print("")
    print("=" * 70)

    print(
        "[SYSTEM] "
        "Instagram Discord Monitor stopped."
    )

    print("=" * 70)


# ============================================================
# Start
# ============================================================

if __name__ == "__main__":
    main()
```
