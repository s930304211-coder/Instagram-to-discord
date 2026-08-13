#!/usr/bin/env python3

import os
import time
import json
import random
import tempfile
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import instaloader


# ============================================================
# Environment Variables
# ============================================================

# 多個 Instagram 帳號：
#
# IG_USERNAME=account1,account2,account3
#
RAW_USERNAMES = os.environ.get("IG_USERNAME", "")

USERNAMES = [
    username.strip()
    for username in RAW_USERNAMES.split(",")
    if username.strip()
]


# Discord Webhook
#
# 優先使用 INSTAGRAM_POST_WEBHOOK
# 如果沒有，就使用 WEBHOOK_URL
#
WEBHOOK_URL = os.environ.get(
    "INSTAGRAM_POST_WEBHOOK",
    os.environ.get("WEBHOOK_URL", "")
)


# ============================================================
# Polling Settings
# ============================================================

# 每一輪檢查之間的時間
#
# 建議：
# 3600 = 1 小時
# 7200 = 2 小時
#
CHECK_INTERVAL = int(
    os.environ.get("CHECK_INTERVAL", "7200")
)


# 每個 Instagram 帳號之間的等待時間
#
# 9 個帳號建議至少 120 秒
#
ACCOUNT_DELAY = int(
    os.environ.get("ACCOUNT_DELAY", "120")
)


# ============================================================
# Instagram 429 Backoff
# ============================================================

# 第一次遇到 429 的基本冷卻時間
# 1200 = 20 分鐘
#
INITIAL_BACKOFF = int(
    os.environ.get("INITIAL_BACKOFF", "1200")
)


# 最大冷卻時間
# 21600 = 6 小時
#
MAX_BACKOFF = int(
    os.environ.get("MAX_BACKOFF", "21600")
)


# ============================================================
# State
# ============================================================

STATE_FILE = os.environ.get(
    "STATE_FILE",
    "last_posts.json"
)


# ============================================================
# Discord File Size
# ============================================================

# Render / Discord 上傳時採保守值
#
# 9.5 MB
#
MAX_DISCORD_FILE_SIZE = int(
    os.environ.get(
        "MAX_DISCORD_FILE_SIZE",
        "9500000"
    )
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
    compress_json=False
)


# ============================================================
# Global Rate Limit State
# ============================================================

rate_limit_until = 0

backoff_seconds = INITIAL_BACKOFF


# ============================================================
# State Functions
# ============================================================

def load_state():

    if not os.path.exists(STATE_FILE):
        print(
            f"[STATE] {STATE_FILE} 不存在，建立新的 State。"
        )

        return {}

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

            if isinstance(state, dict):
                return state

            print(
                "[STATE] State 格式不是 JSON object，重設。"
            )

            return {}

    except Exception as e:

        print(
            f"[STATE] 讀取失敗: {e}"
        )

        return {}


def save_state(state):

    try:

        state_path = Path(STATE_FILE)

        state_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        # 先寫入暫存檔
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

            f.flush()
            os.fsync(f.fileno())

        # 完成後再替換
        os.replace(
            temp_path,
            state_path
        )

        print(
            "[STATE] State saved."
        )

        return True

    except Exception as e:

        print(
            f"[STATE] 儲存失敗: {e}"
        )

        return False


STATE = load_state()


# ============================================================
# Rate Limit Functions
# ============================================================

def is_rate_limited():

    return time.time() < rate_limit_until


def get_remaining_cooldown():

    if not is_rate_limited():
        return 0

    return max(
        0,
        int(rate_limit_until - time.time())
    )


def trigger_backoff(error=None):

    global rate_limit_until
    global backoff_seconds

    error_text = str(error) if error else ""

    # Instaloader 有時會在錯誤訊息中帶：
    #
    # wait 666 seconds
    #
    instagram_wait = None

    import re

    patterns = [
        r"wait\s+(\d+)\s+seconds",
        r"(\d+)\s+seconds",
        r"try again in\s+(\d+)"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            error_text,
            re.IGNORECASE
        )

        if match:

            try:
                instagram_wait = int(
                    match.group(1)
                )

                break

            except ValueError:
                pass


    if instagram_wait:

        wait_time = (
            instagram_wait + 30
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
    print("=" * 65)
    print("🚨 INSTAGRAM 429 RATE LIMIT")
    print("=" * 65)
    print(
        f"⏳ 冷卻時間：{wait_time} 秒"
    )
    print(
        f"🕐 預計恢復："
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(rate_limit_until))}"
    )
    print("=" * 65)
    print("")


    # Exponential Backoff
    backoff_seconds = min(
        backoff_seconds * 2,
        MAX_BACKOFF
    )


def reset_backoff():

    global backoff_seconds

    backoff_seconds = INITIAL_BACKOFF


# ============================================================
# Instagram
# ============================================================

def get_latest_post(username):

    print(
        f"[IG] Loading profile @{username}..."
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
            f"[MEDIA] Downloading {extension}..."
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


                total_size += len(chunk)


                # 超過 Discord 保守限制
                if (
                    total_size
                    > MAX_DISCORD_FILE_SIZE
                ):

                    print(
                        "[MEDIA] "
                        "File exceeds Discord limit."
                    )

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
            os.remove(temp_path)
        except OSError:
            pass


        return None


# ============================================================
# Get Instagram Media
# ============================================================

def get_post_media(post):

    media = []


    try:

        # ----------------------------------------------------
        # Carousel
        # ----------------------------------------------------

        if post.typename == "GraphSidecar":

            print(
                "[MEDIA] Type: CAROUSEL"
            )


            children = list(
                post.get_sidecar_nodes()
            )


            print(
                f"[MEDIA] "
                f"Carousel items: "
                f"{len(children)}"
            )


            for index, child in enumerate(
                children[:10],
                start=1
            ):


                if child.is_video:

                    print(
                        f"[MEDIA] "
                        f"#{index}: VIDEO"
                    )


                    if child.video_url:

                        media.append({
                            "type": "video",
                            "url": child.video_url,
                            "index": index
                        })


                else:

                    print(
                        f"[MEDIA] "
                        f"#{index}: IMAGE"
                    )


                    if child.display_url:

                        media.append({
                            "type": "image",
                            "url": child.display_url,
                            "index": index
                        })


        # ----------------------------------------------------
        # Single Video
        # ----------------------------------------------------

        elif post.is_video:

            print(
                "[MEDIA] Type: VIDEO"
            )


            if post.video_url:

                media.append({
                    "type": "video",
                    "url": post.video_url,
                    "index": 1
                })


        # ----------------------------------------------------
        # Single Image
        # ----------------------------------------------------

        else:

            print(
                "[MEDIA] Type: IMAGE"
            )


            if post.url:

                media.append({
                    "type": "image",
                    "url": post.url,
                    "index": 1
                })


    except Exception as e:

        print(
            f"[MEDIA] "
            f"取得媒體失敗: {e}"
        )


    return media[:10]


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
            "Webhook URL missing."
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


    media = get_post_media(post)


    print(
        f"[DISCORD] "
        f"Preparing {len(media)} media."
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
                f"[DISCORD] "
                f"Processing media #{index} "
                f"({media_type})"
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
            # Embed Media
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

            else:

                # Discord 對 attachment video
                # 可以直接顯示影片檔案
                embeds.append({

                    "url":
                        post_url,

                    "description":
                        f"🎬 Video #{index}",

                    "color":
                        15467852

                })


        # Discord 一次最多 10 個 embeds
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
            f"Uploading {len(attachments)} files..."
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
            f"[DISCORD] "
            f"HTTP {response.status_code}"
        )


        if response.status_code in (
            200,
            204
        ):

            print(
                "✅ [DISCORD] "
                "Successfully sent."
            )

            return True


        print(
            "❌ [DISCORD] "
            "Webhook failed:"
        )

        print(
            response.text[:2000]
        )


        return False


    except requests.exceptions.RequestException as e:

        print(
            f"❌ [DISCORD] "
            f"Request error: {e}"
        )

        return False


    except Exception as e:

        print(
            f"❌ [DISCORD] "
            f"Unexpected error: {e}"
        )

        return False


    finally:

        # ----------------------------------------------------
        # Close files
        # ----------------------------------------------------

        for file_object in file_handles:

            try:
                file_object.close()
            except Exception:
                pass


        # ----------------------------------------------------
        # Remove temp files
        # ----------------------------------------------------

        for file_path in temp_files:

            try:
                os.remove(file_path)
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

        print(
            f"[{username}] "
            "目前仍在 Instagram cooldown。"
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
                "No posts found."
            )

            return True


        shortcode = post.shortcode


        previous_shortcode = state.get(
            username
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
                "FIRST RUN."
            )

            print(
                f"[{username}] "
                f"Saving {shortcode} "
                "without notification."
            )


            state[username] = shortcode

            save_state(state)

            return True


        # ----------------------------------------------------
        # No New Post
        # ----------------------------------------------------

        if previous_shortcode == shortcode:

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
        # Send Discord
        # ----------------------------------------------------

        success = send_discord_notification(
            username,
            post
        )


        # ----------------------------------------------------
        # VERY IMPORTANT
        #
        # Only update state when Discord succeeded.
        # ----------------------------------------------------

        if success:

            state[username] = shortcode

            if save_state(state):

                print(
                    f"✅ [{username}] "
                    "State updated."
                )

            else:

                print(
                    f"⚠️ [{username}] "
                    "Discord succeeded but "
                    "State save failed."
                )


            return True


        else:

            print(
                f"❌ [{username}] "
                "Discord failed."
            )

            print(
                f"⚠️ [{username}] "
                "State NOT updated."
            )

            return True


    # ========================================================
    # Instagram 429
    # ========================================================

    except instaloader.exceptions.TooManyRequestsException as e:

        print(
            f"🚨 [{username}] "
            "Instagram returned 429."
        )

        trigger_backoff(e)

        return False


    # ========================================================
    # Connection Error
    # ========================================================

    except instaloader.exceptions.ConnectionException as e:

        error_text = str(e)

        print(
            f"❌ [{username}] "
            f"Instagram connection error: "
            f"{error_text}"
        )


        lower_text = (
            error_text.lower()
        )


        if (
            "429" in error_text
            or "too many" in lower_text
            or "rate limit" in lower_text
            or "rate" in lower_text
        ):

            trigger_backoff(e)

            return False


        return True


    # ========================================================
    # Profile Not Exists
    # ========================================================

    except instaloader.exceptions.ProfileNotExistsException:

        print(
            f"⚠️ [{username}] "
            "Profile does not exist."
        )

        return True


    # ========================================================
    # Generic Instaloader Error
    # ========================================================

    except instaloader.exceptions.InstaloaderException as e:

        print(
            f"❌ [{username}] "
            f"Instaloader error: {e}"
        )

        return True


    # ========================================================
    # Unexpected Error
    # ========================================================

    except Exception as e:

        print(
            f"❌ [{username}] "
            f"Unexpected error: {e}"
        )

        return True


# ============================================================
# Health Check Server
# ============================================================

class HealthCheckHandler(
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
                "text/plain; charset=utf-8"
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
        HealthCheckHandler
    )


    print("")
    print(
        f"🌐 Health Server "
        f"listening on port {port}"
    )


    server.serve_forever()


# ============================================================
# Main
# ============================================================

def main():

    print("")
    print("=" * 70)
    print("Instagram → Discord Monitor")
    print("=" * 70)


    # --------------------------------------------------------
    # Validate Environment
    # --------------------------------------------------------

    if not USERNAMES:

        print(
            "❌ ERROR: "
            "IG_USERNAME is missing."
        )

        return


    if not WEBHOOK_URL:

        print(
            "❌ ERROR: "
            "Discord Webhook is missing."
        )

        return


    # --------------------------------------------------------
    # Configuration
    # --------------------------------------------------------

    print(
        f"📋 Accounts: "
        f"{len(USERNAMES)}"
    )

    print(
        f"⏱️ Check interval: "
        f"{CHECK_INTERVAL}s "
        f"({CHECK_INTERVAL / 3600:.1f} hours)"
    )

    print(
        f"⏳ Account delay: "
        f"{ACCOUNT_DELAY}s"
    )

    print(
        f"🚨 Initial 429 backoff: "
        f"{INITIAL_BACKOFF}s"
    )

    print(
        f"🛑 Maximum 429 backoff: "
        f"{MAX_BACKOFF}s"
    )

    print(
        f"💾 State file: "
        f"{STATE_FILE}"
    )

    print("=" * 70)


    # --------------------------------------------------------
    # Start Health Server
    # --------------------------------------------------------

    server_thread = threading.Thread(
        target=run_web_server,
        daemon=True
    )

    server_thread.start()


    print(
        "✅ Health server started."
    )


    # --------------------------------------------------------
    # Main Loop
    # --------------------------------------------------------

    while True:

        # ====================================================
        # Global Rate Limit
        # ====================================================

        if is_rate_limited():

            remaining = (
                get_remaining_cooldown()
            )


            print("")
            print(
                "🛑 Instagram cooldown "
                f"still active: "
                f"{remaining}s"
            )


            # 每分鐘醒來一次
            time.sleep(
                min(
                    remaining,
                    60
                )
            )


            continue


        # ====================================================
        # Start Cycle
        # ====================================================

        print("")
        print("=" * 70)

        print(
            "⏰ Starting Instagram check cycle"
        )

        print(
            time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        print("=" * 70)


        cycle_rate_limited = False


        # ====================================================
        # Check Every Account
        # ====================================================

        for index, username in enumerate(
            USERNAMES
        ):


            # ------------------------------------------------
            # Check global cooldown
            # ------------------------------------------------

            if is_rate_limited():

                cycle_rate_limited = True

                print(
                    "🛑 Rate limit detected. "
                    "Stopping current cycle."
                )

                break


            # ------------------------------------------------
            # Check account
            # ------------------------------------------------

            success = check_account(
                username,
                STATE
            )


            # ------------------------------------------------
            # If 429
            # ------------------------------------------------

            if not success:

                cycle_rate_limited = True

                print("")
                print(
                    "🛑 Instagram rate limit "
                    "detected."
                )

                print(
                    "🛑 Stopping current cycle "
                    "to avoid further requests."
                )

                break


            # ------------------------------------------------
            # Delay between accounts
            # ------------------------------------------------

            if (
                index
                < len(USERNAMES) - 1
            ):

                print(
                    f"⏳ Waiting "
                    f"{ACCOUNT_DELAY}s "
                    "before next account..."
                )

                time.sleep(
                    ACCOUNT_DELAY
                )


        # ====================================================
        # Rate Limited
        # ====================================================

        if cycle_rate_limited:

            print("")
            print(
                "🚨 Current cycle stopped "
                "because of rate limit."
            )

            print(
                "⏳ Waiting for cooldown..."
            )

            continue


        # ====================================================
        # Successful Cycle
        # ====================================================

        reset_backoff()


        print("")
        print("=" * 70)

        print(
            f"✅ Finished checking "
            f"{len(USERNAMES)} accounts."
        )

        print(
            f"😴 Sleeping "
            f"{CHECK_INTERVAL}s "
            f"({CHECK_INTERVAL / 3600:.1f} hours)"
        )

        print("=" * 70)


        time.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# Start
# ============================================================

if __name__ == "__main__":

    main()
