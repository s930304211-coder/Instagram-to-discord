#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Instagram → Discord Monitor
Render Production Version

Features:
- Multiple Instagram accounts
- Instagram Session support
- Global Instagram 429 cooldown
- Exponential backoff
- Random jitter
- Persistent cooldown
- Persistent post state
- Discord Webhook
- Image / Video / Carousel
- Render health check
- Atomic state write
- Graceful shutdown
- Detailed boot diagnostics
"""

# ============================================================
# BOOT
# ============================================================

print("[BOOT 01] Python process started", flush=True)


# ============================================================
# Standard Library
# ============================================================

import json
import os
import random
import re
import signal
import sys
import tempfile
import threading
import time

print("[BOOT 02] Standard libraries imported", flush=True)


from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


print("[BOOT 03] HTTP server modules imported", flush=True)


# ============================================================
# Third Party
# ============================================================

print("[BOOT 04] Importing requests...", flush=True)

import requests

print(
    f"[BOOT 05] requests imported "
    f"version={getattr(requests, '__version__', 'unknown')}",
    flush=True,
)


print("[BOOT 06] Importing instaloader...", flush=True)

import instaloader

print(
    "[BOOT 07] instaloader imported "
    f"version={getattr(instaloader, '__version__', 'unknown')}",
    flush=True,
)


# ============================================================
# Environment
# ============================================================

print("[BOOT 08] Reading environment variables...", flush=True)


RAW_USERNAMES = os.environ.get(
    "IG_USERNAME",
    "",
)


USERNAMES = [
    username.strip()
    for username in RAW_USERNAMES.split(",")
    if username.strip()
]


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
        "180",
    )
)


# ============================================================
# Instagram Rate Limit
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
# ============================================================

IG_SESSION_FILE = os.environ.get(
    "IG_SESSION_FILE",
    "",
).strip()


IG_SESSION_USERNAME = os.environ.get(
    "IG_SESSION_USERNAME",
    "",
).strip()


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
# Global State
# ============================================================

shutdown_requested = False

rate_limit_until = 0.0

backoff_seconds = INITIAL_BACKOFF

STATE = {}


# ============================================================
# Locks
# ============================================================

STATE_LOCK = threading.Lock()

RATE_LIMIT_LOCK = threading.Lock()

CYCLE_LOCK = threading.Lock()


# ============================================================
# Exceptions
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
# Instaloader Rate Controller
# ============================================================

class AbortOn429RateController(
    instaloader.RateController
):

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
    "[BOOT 09] Creating Instaloader instance...",
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
    "[BOOT 10] Instaloader instance created",
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
        f"[STATE] Loading state file: {STATE_FILE}",
        flush=True,
    )

    if not os.path.exists(STATE_FILE):

        print(
            "[STATE] State file does not exist.",
            flush=True,
        )

        print(
            "[STATE] Starting with empty state.",
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
                "[STATE] Invalid state format.",
                flush=True,
            )

            return {}


        if "_meta" in raw_data:

            meta = raw_data.get(
                "_meta",
                {},
            )

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
                        "[STATE] Restored Instagram cooldown.",
                        flush=True,
                    )

                    print(
                        "[STATE] Remaining: "
                        f"{format_seconds(remaining)}",
                        flush=True,
                    )


            posts_state = raw_data.get(
                "posts",
                {},
            )

        else:

            posts_state = raw_data


        if not isinstance(
            posts_state,
            dict,
        ):

            posts_state = {}


        print(
            "[STATE] Loaded "
            f"{len(posts_state)} account records.",
            flush=True,
        )


        return posts_state


    except Exception as error:

        print(
            "[STATE] Load failed: "
            f"{error}",
            flush=True,
        )

        return {}


# ============================================================
# State Save
# ============================================================

def save_state(posts_state):

    try:

        state_path = os.path.abspath(
            STATE_FILE
        )

        state_dir = os.path.dirname(
            state_path
        )


        if state_dir:

            os.makedirs(
                state_dir,
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
                        "%Y-%m-%d %H:%M:%S",
                    ),

            },

            "posts":
                dict(posts_state),

        }


        fd, temp_path = tempfile.mkstemp(
            prefix=".instagram-state-",
            suffix=".tmp",
            dir=state_dir or ".",
        )


        try:

            with os.fdopen(
                fd,
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


        except Exception:

            try:
                os.remove(temp_path)
            except OSError:
                pass

            raise


        print(
            "[STATE] State saved successfully.",
            flush=True,
        )


        return True


    except Exception as error:

        print(
            "[STATE] Save failed: "
            f"{error}",
            flush=True,
        )

        return False


# ============================================================
# Rate Limit
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
                    match.group(1)
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
                error
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
        "⏳ Cooldown: "
        f"{format_seconds(wait_time)}",
        flush=True,
    )

    print(
        "🕐 Resume: "
        + time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(
                cooldown_until
            ),
        ),
        flush=True,
    )

    print(
        "📈 Next backoff: "
        f"{format_seconds(current_backoff)}",
        flush=True,
    )

    print(
        "🛑 Current cycle stopped.",
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

    if not IG_SESSION_FILE:

        print(
            "[IG] Session disabled.",
            flush=True,
        )

        print(
            "[IG] Running in anonymous mode.",
            flush=True,
        )

        return False


    if not IG_SESSION_USERNAME:

        print(
            "[IG] IG_SESSION_FILE configured "
            "but IG_SESSION_USERNAME is missing.",
            flush=True,
        )

        return False


    if not os.path.exists(
        IG_SESSION_FILE
    ):

        print(
            "[IG] Session file does not exist: "
            f"{IG_SESSION_FILE}",
            flush=True,
        )

        return False


    try:

        print(
            "[IG] Loading Instagram session...",
            flush=True,
        )


        L.load_session_from_file(
            IG_SESSION_USERNAME,
            IG_SESSION_FILE,
        )


        print(
            "[IG] Instagram session loaded successfully.",
            flush=True,
        )


        return True


    except Exception as error:

        print(
            "[IG] Session load failed: "
            f"{error}",
            flush=True,
        )

        print(
            "[IG] Continuing in anonymous mode.",
            flush=True,
        )

        return False


# ============================================================
# Get Latest Post
# ============================================================

def get_latest_post(username):

    print(
        f"[IG] Getting @{username} profile...",
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
        f"[IG] @{username} profile loaded.",
        flush=True,
    )


    print(
        f"[IG] Getting @{username} latest post...",
        flush=True,
    )


    posts = profile.get_posts()


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
            "[MEDIA] Downloading "
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
                        "[MEDIA] File exceeds "
                        "Discord safety limit.",
                        flush=True,
                    )


                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass


                    return None


                file.write(chunk)


        print(
            "[MEDIA] Download complete: "
            f"{total_size / 1024 / 1024:.2f} MB",
            flush=True,
        )


        return temp_path


    except Exception as error:

        print(
            "[MEDIA] Download failed: "
            f"{error}",
            flush=True,
        )


        try:
            os.remove(temp_path)
        except OSError:
            pass


        return None


# ============================================================
# Get Post Media (已修正語法錯誤)
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

                    media.append({

                        "type":
                            "video",

                        "url":
                            child.video_url,

                        "index":
                            index,

                    })


                elif child.display_url:

                    # 【修正處】閉合字典與陣列
                    media.append({

                        "type":
                            "image",

                        "url":
                            child.display_url,

                        "index":
                            index,

                    })


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


            # 【修正處】閉合字典與陣列
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
            "[MEDIA] Parse failed: "
            f"{error}",
            flush=True,
        )


    return media[:MAX_MEDIA_ITEMS]


# ============================================================
# Discord
# ============================================================

def send_discord_notification(
    username,
    post,
):

    if not WEBHOOK_URL:

        print(
            "[DISCORD] Webhook not configured.",
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


    media = get_post_media(post)


    print(
        "[DISCORD] Preparing "
        f"{len(media)} media items.",
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

                "footer": {
                    "text":
                        "Instagram → Discord",
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
                "[DISCORD] Processing media #"
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
                    "[DISCORD] Skipping media #"
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

                if index == 1:
                    embeds[0]["image"] = {
                        "url": f"attachment://{filename}"
                    }
                else:
                    embeds.append({
                        "url": post_url,
                        "image": {
                            "url": f"attachment://{filename}",
                        },
                        "color": 15467852,
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
                "✅ [DISCORD] Push successful.",
                flush=True,
            )

            return True


        if response.status_code == 429:

            print(
                "⚠️ [DISCORD] Discord webhook "
                "rate limited.",
                flush=True,
            )


        else:

            print(
                "❌ [DISCORD] Webhook failed "
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
            "❌ [DISCORD] Request error: "
            f"{error}",
            flush=True,
        )

        return False


    except Exception as error:

        print(
            "❌ [DISCORD] Unexpected error: "
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
                os.remove(file_path)
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
            f"[{username}] Instagram cooldown active.",
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
            f"🔍 Checking @{username}",
            flush=True,
        )


        post = get_latest_post(
            username
        )


        if not post:

            print(
                f"[{username}] No posts found.",
                flush=True,
            )

            return "SUCCESS"


        shortcode = post.shortcode


        with STATE_LOCK:

            previous_shortcode = (
                state.get(username)
            )


        print(
            f"[{username}] Latest: "
            f"{shortcode}",
            flush=True,
        )


        if previous_shortcode is None:

            print(
                f"[{username}] FIRST RUN",
                flush=True,
            )


            with STATE_LOCK:

                state[username] = shortcode


            save_state(state)


            print(
                f"[{username}] State recorded. "
                "Discord notification skipped.",
                flush=True,
            )


            return "SUCCESS"


        if previous_shortcode == shortcode:

            print(
                f"[{username}] No new post.",
                flush=True,
            )

            return "SUCCESS"


        print(
            "",
            flush=True,
        )

        print(
            f"🚨 NEW POST @{username}",
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


        success = send_discord_notification(
            username,
            post,
        )


        if success:

            with STATE_LOCK:

                state[username] = shortcode


            if save_state(state):

                print(
                    f"✅ [{username}] State updated.",
                    flush=True,
                )

            else:

                print(
                    f"⚠️ [{username}] Discord sent successfully "
                    "but state save failed.",
                    flush=True,
                )


            return "SUCCESS"


        print(
            f"❌ [{username}] Discord send failed.",
            flush=True,
        )


        print(
            f"⚠️ [{username}] State not updated.",
            flush=True,
        )


        return "ERROR"


    except InstagramRateLimited as error:

        print(
            "",
            flush=True,
        )

        print(
            f"🚨 [{username}] Instagram 429 intercepted.",
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


    except (
        instaloader.exceptions.TooManyRequestsException
    ) as error:

        print(
            "",
            flush=True,
        )

        print(
            f"🚨 [{username}] Instagram 429.",
            flush=True,
        )


        trigger_backoff(error)


        return "RATE_LIMITED"


    except (
        instaloader.exceptions.ConnectionException
    ) as error:

        error_text = str(error)

        lower_text = error_text.lower()


        if (

            "429" in error_text

            or "too many" in lower_text

            or "rate limit" in lower_text

            or "rate-limit" in lower_text

            or "rate limited" in lower_text

        ):

            print(
                f"🚨 [{username}] Instagram "
                "rate limit detected.",
                flush=True,
            )


            trigger_backoff(error)


            return "RATE_LIMITED"


        print(
            f"❌ [{username}] Instagram connection error: "
            f"{error_text}",
            flush=True,
        )


        return "ERROR"


    except (
        instaloader.exceptions.ProfileNotExistsException
    ):

        print(
            f"⚠️ [{username}] Profile does not exist.",
            flush=True,
        )


        return "ERROR"


    except (
        instaloader.exceptions.InstaloaderException
    ) as error:

        error_text = str(error)

        lower_text = error_text.lower()


        if (

            "429" in error_text

            or "too many" in lower_text

            or "rate limit" in lower_text

            or "rate-limit" in lower_text

            or "rate limited" in lower_text

        ):

            print(
                f"🚨 [{username}] Instagram "
                "rate limit detected.",
                flush=True,
            )


            trigger_backoff(error)


            return "RATE_LIMITED"


        print(
            f"❌ [{username}] Instaloader error: "
            f"{error_text}",
            flush=True,
        )


        return "ERROR"


    except Exception as error:

        error_text = str(error)

        lower_text = error_text.lower()


        if (

            "429" in error_text

            or "too many requests" in lower_text

            or "rate limit" in lower_text

        ):

            print(
                f"🚨 [{username}] Unexpected error "
                "appears to be Instagram 429.",
                flush=True,
            )


            trigger_backoff(error)


            return "RATE_LIMITED"


        print(
            f"❌ [{username}] Unexpected error: "
            f"{error}",
            flush=True,
        )


        return "ERROR"


# ============================================================
# Health Server
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

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )

            self.send_header(
                "Cache-Control",
                "no-store",
            )

            self.end_headers()

            self.wfile.write(
                b"Instagram to Discord Bot is alive."
            )

            return


        self.send_response(404)

        self.end_headers()


    def log_message(
        self,
        format,
        *args,
    ):

        return


# ============================================================
# Health Server Runner
# ============================================================

def run_web_server():

    try:

        port = int(
            os.environ.get(
                "PORT",
                "10000",
            )
        )


        print(
            f"[WEB] Starting HTTP server on "
            f"0.0.0.0:{port}",
            flush=True,
        )


        server = ThreadingHTTPServer(

            (
                "0.0.0.0",
                port,
            ),

            HealthHandler,

        )


        print(
            f"🌐 Health server listening on "
            f"0.0.0.0:{port}",
            flush=True,
        )


        print(
            "[WEB] Health endpoints: "
            "/ /health /healthz",
            flush=True,
        )


        server.serve_forever()


    except Exception as error:

        print(
            f"❌ Health server error: {error}",
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
        "Render Production Version",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )


    print(
        f"📋 Instagram accounts: {len(USERNAMES)}",
        flush=True,
    )


    if USERNAMES:

        print(
            "👤 Accounts: "
            + ", ".join(USERNAMES),
            flush=True,
        )


    print(
        f"⏱️ Check interval: "
        f"{CHECK_INTERVAL}s "
        f"({CHECK_INTERVAL / 3600:.1f}h)",
        flush=True,
    )


    print(
        f"⏳ Account delay: "
        f"{ACCOUNT_DELAY}s "
        f"({ACCOUNT_DELAY / 60:.1f}m)",
        flush=True,
    )


    print(
        f"🚨 Initial backoff: "
        f"{INITIAL_BACKOFF}s",
        flush=True,
    )


    print(
        f"🛑 Maximum backoff: "
        f"{MAX_BACKOFF}s",
        flush=True,
    )


    print(
        f"🎲 Jitter: "
        f"{BACKOFF_JITTER_MIN}s - "
        f"{BACKOFF_JITTER_MAX}s",
        flush=True,
    )


    print(
        f"💾 State file: {STATE_FILE}",
        flush=True,
    )


    print(
        f"💾 Persistent cooldown: "
        f"{PERSIST_COOLDOWN}",
        flush=True,
    )


    print(
        f"📦 Max Discord file: "
        f"{MAX_DISCORD_FILE_SIZE / 1000000:.2f} MB",
        flush=True,
    )


    if IG_SESSION_FILE:

        print(
            "🔐 Instagram Session: ENABLED",
            flush=True,
        )

        print(
            f"🔐 Session file: {IG_SESSION_FILE}",
            flush=True,
        )

        print(
            f"🔐 Session username: "
            f"{IG_SESSION_USERNAME or '(missing)'}",
            flush=True,
        )

    else:

        print(
            "🔓 Instagram Session: "
            "DISABLED / Anonymous",
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
        "[BOOT 11] main() entered",
        flush=True,
    )


    signal.signal(
        signal.SIGTERM,
        handle_shutdown,
    )

    signal.signal(
        signal.SIGINT,
        handle_shutdown,
    )


    print(
        "[BOOT 12] Signal handlers installed",
        flush=True,
    )


    print_configuration()


    if not USERNAMES:

        print(
            "❌ ERROR: IG_USERNAME is not configured.",
            flush=True,
        )

        sys.exit(1)


    if not WEBHOOK_URL:

        print(
            "❌ ERROR: Discord webhook is not configured.",
            flush=True,
        )

        sys.exit(1)


    print(
        "[BOOT 13] Configuration validation passed",
        flush=True,
    )


    print(
        "[BOOT 14] Loading persistent state...",
        flush=True,
    )


    global STATE

    STATE = load_state()


    print(
        "[BOOT 15] Persistent state loaded",
        flush=True,
    )


    print(
        "[BOOT 16] Loading Instagram session...",
        flush=True,
    )


    load_instagram_session()


    print(
        "[BOOT 17] Instagram session step complete",
        flush=True,
    )


    print(
        "[BOOT 18] Starting health server...",
        flush=True,
    )


    server_thread = threading.Thread(
        target=run_web_server,
        name="health-server",
        daemon=True,
    )


    server_thread.start()


    time.sleep(0.5)


    print(
        "[BOOT 19] Health server thread started",
        flush=True,
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
        "🚀 INSTAGRAM → DISCORD BOT READY",
        flush=True,
    )

    print(
        f"📋 Accounts: {len(USERNAMES)}",
        flush=True,
    )

    print(
        f"🌐 Port: "
        f"{os.environ.get('PORT', '10000')}",
        flush=True,
    )

    print(
        f"💾 State: {STATE_FILE}",
        flush=True,
    )

    print(
        "=" * 70,
        flush=True,
    )


    # ========================================================
    # Main Loop
    # ========================================================

    while not shutdown_requested:

        if is_rate_limited():

            remaining = (
                get_remaining_cooldown()
            )


            print(
                "",
                flush=True,
            )

            print(
                "🛑 Instagram cooldown active.",
                flush=True,
            )

            print(
                "⏳ Remaining: "
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


        if not CYCLE_LOCK.acquire(
            blocking=False
        ):

            sleep_interruptible(5)

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
                "⏰ Starting Instagram check cycle",
                flush=True,
            )

            print(
                time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                flush=True,
            )

            print(
                f"📋 Accounts: {len(USERNAMES)}",
                flush=True,
            )

            print(
                "=" * 70,
                flush=True,
            )


            hit_rate_limit = False


            for index, username in enumerate(
                USERNAMES
            ):

                if shutdown_requested:

                    break


                if is_rate_limited():

                    hit_rate_limit = True

                    print(
                        "🛑 Global Instagram cooldown "
                        "activated.",
                        flush=True,
                    )

                    break


                result = check_account(
                    username,
                    STATE,
                )


                if result == "RATE_LIMITED":

                    hit_rate_limit = True

                    print(
                        "🚨 Instagram rate limit.",
                        flush=True,
                    )

                    print(
                        "🛑 Stopping current cycle.",
                        flush=True,
                    )

                    break


                if result == "ERROR":

                    print(
                        f"⚠️ @{username} check failed. "
                        "Continuing.",
                        flush=True,
                    )


                if (
                    index < len(USERNAMES) - 1
                    and not shutdown_requested
                ):

                    print(
                        "",
                        flush=True,
                    )

                    print(
                        "⏳ Waiting before next account: "
                        f"{ACCOUNT_DELAY}s",
                        flush=True,
                    )


                    sleep_interruptible(
                        ACCOUNT_DELAY
                    )


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
                    "🚨 Cycle stopped because of "
                    "Instagram rate limit.",
                    flush=True,
                )

                print(
                    "⏳ Waiting for global cooldown.",
                    flush=True,
                )

                print(
                    "=" * 70,
                    flush=True,
                )

                continue


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
                    "✅ Cycle completed.",
                    flush=True,
                )

                print(
                    f"📋 Processed "
                    f"{len(USERNAMES)} accounts.",
                    flush=True,
                )

                print(
                    "😴 Sleeping until next cycle: "
                    f"{CHECK_INTERVAL}s "
                    f"({CHECK_INTERVAL / 3600:.1f}h)",
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


    print(
        "",
        flush=True,
    )

    print(
        "👋 Instagram → Discord Bot stopped safely.",
        flush=True,
    )


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":

    print(
        "[BOOT 00] Entering application...",
        flush=True,
    )

    try:

        main()

    except KeyboardInterrupt:

        print(
            "[BOOT] KeyboardInterrupt",
            flush=True,
        )

    except Exception as error:

        print(
            "",
            flush=True,
        )

        print(
            "💥 FATAL APPLICATION ERROR",
            flush=True,
        )

        print(
            f"{type(error).__name__}: {error}",
            flush=True,
        )

        import traceback

        traceback.print_exc()

        sys.exit(1)
