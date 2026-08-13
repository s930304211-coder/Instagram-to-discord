#!/usr/bin/env python3

import atexit
import json
import os
import random
import re
import signal
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import instaloader
import requests


# ============================================================
# Configuration
# ============================================================

# ------------------------------------------------------------
# Instagram Accounts
#
# Example:
#
# IG_USERNAME=account1,account2,account3
# ------------------------------------------------------------

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
# Optional Instagram login
#
# IMPORTANT:
# Do not automatically login every restart.
#
# Recommended:
# use a persistent session file instead.
#
# IG_LOGIN_USERNAME=your_instagram_login
# IG_SESSION_FILE=/var/data/ig_session
# ------------------------------------------------------------

IG_LOGIN_USERNAME = os.environ.get(
    "IG_LOGIN_USERNAME",
    ""
)

IG_SESSION_FILE = os.environ.get(
    "IG_SESSION_FILE",
    ""
)


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


# ------------------------------------------------------------
# Polling
# ------------------------------------------------------------

CHECK_INTERVAL = int(
    os.environ.get(
        "CHECK_INTERVAL",
        "7200"
    )
)


# Recommended for 9 accounts:
#
# 180 = 3 minutes
# 240 = 4 minutes
# 300 = 5 minutes
#
ACCOUNT_DELAY = int(
    os.environ.get(
        "ACCOUNT_DELAY",
        "180"
    )
)


# ------------------------------------------------------------
# 429 Backoff
# ------------------------------------------------------------

INITIAL_BACKOFF = int(
    os.environ.get(
        "INITIAL_BACKOFF",
        "1200"
    )
)

MAX_BACKOFF = int(
    os.environ.get(
        "MAX_BACKOFF",
        "21600"
    )


# Additional safety buffer after Instagram's
# reported retry time.
#
# Instagram says:
#
# retry in 666 seconds
#
# We use:
#
# 666 + 30 seconds
#
RATE_LIMIT_BUFFER = int(
    os.environ.get(
        "RATE_LIMIT_BUFFER",
        "30"
    )
)


# ------------------------------------------------------------
# State
#
# IMPORTANT for Render:
#
# Set:
#
# STATE_FILE=/var/data/last_posts.json
#
# if /var/data is your persistent disk mount.
# ------------------------------------------------------------

STATE_FILE = os.environ.get(
    "STATE_FILE",
    "last_posts.json"
)


# ------------------------------------------------------------
# Persistent cooldown state
#
# This allows the application to remember a cooldown
# after a process restart, provided STATE_FILE is persistent.
# ------------------------------------------------------------

PERSIST_COOLDOWN = (
    os.environ.get(
        "PERSIST_COOLDOWN",
        "true"
    ).lower()
    in (
        "1",
        "true",
        "yes",
        "on"
    )
)


# ------------------------------------------------------------
# Discord file size
#
# Conservative value.
# ------------------------------------------------------------

MAX_DISCORD_FILE_SIZE = int(
    os.environ.get(
        "MAX_DISCORD_FILE_SIZE",
        "9500000"
    )
)


# ------------------------------------------------------------
# Media limits
#
# Discord supports up to 10 embeds per webhook message.
#
# We reserve:
#
# 1 embed = main post
# 9 embeds = media
#
# Therefore:
#
# MAX_MEDIA = 9
# ------------------------------------------------------------

MAX_MEDIA = int(
    os.environ.get(
        "MAX_MEDIA",
        "9"
    )
)

MAX_MEDIA = max(
    1,
    min(
        MAX_MEDIA,
        9
    )
)


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

HTTP_TIMEOUT = int(
    os.environ.get(
        "HTTP_TIMEOUT",
        "90"
    )
)


DISCORD_TIMEOUT = int(
    os.environ.get(
        "DISCORD_TIMEOUT",
        "180"
    )
)


# ------------------------------------------------------------
# Health server
# ------------------------------------------------------------

PORT = int(
    os.environ.get(
        "PORT",
        "10000"
    )
)


# ------------------------------------------------------------
# User Agent
#
# Keep this consistent.
#
# Changing User-Agent frequently can make Instagram
# session/risk signals worse.
# ------------------------------------------------------------

USER_AGENT = os.environ.get(
    "INSTAGRAM_USER_AGENT",
    (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/151.0 Safari/537.36"
    )
)


# ============================================================
# HTTP Session
# ============================================================

HTTP = requests.Session()

HTTP.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
})


# ============================================================
# Custom Rate Limit Exception
# ============================================================

class InstagramRateLimited(Exception):

    def __init__(
        self,
        wait_seconds=0,
        message=""
    ):

        self.wait_seconds = (
            int(wait_seconds)
            if wait_seconds
            else 0
        )

        self.message = (
            message
            or "Instagram returned HTTP 429."
        )

        super().__init__(
            self.message
        )


# ============================================================
# Custom Rate Controller
#
# IMPORTANT:
#
# Instaloader normally handles 429 internally and may sleep
# for the calculated cooldown.
#
# We don't want the Instaloader request to block the whole
# application for 666 seconds.
#
# Therefore, handle_429() calculates the wait time and raises
# our own exception instead.
# ============================================================

class AbortOn429RateController(
    instaloader.RateController
):

    def handle_429(
        self,
        query_type
    ):

        try:

            wait_time = self.query_waittime(
                query_type,
                time.monotonic(),
                True
            )

        except Exception:

            wait_time = 0


        wait_time = max(
            0,
            int(
                round(
                    wait_time
                )
            )
        )


        raise InstagramRateLimited(
            wait_seconds=wait_time,
            message=(
                "Instagram returned HTTP 429 "
                f"for query type {query_type}."
            )
        )


# ============================================================
# Instaloader
# ============================================================

L = instaloader.Instaloader(

    sleep=True,

    quiet=False,

    user_agent=USER_AGENT,

    download_pictures=False,

    download_videos=False,

    download_video_thumbnails=False,

    download_geotags=False,

    download_comments=False,

    save_metadata=False,

    compress_json=False,

    # Very important:
    #
    # Don't allow Instaloader to keep retrying.
    max_connection_attempts=1,

    request_timeout=90,

    # 429 immediately leaves Instaloader.
    #
    # Our own cooldown logic handles it.
    fatal_status_codes=[
        429
    ],

    # Custom controller for safety.
    rate_controller=(
        lambda context:
        AbortOn429RateController(
            context
        )
    )
)


# ============================================================
# Global Runtime State
# ============================================================

rate_limit_until = 0.0

backoff_seconds = INITIAL_BACKOFF

shutdown_event = threading.Event()

state_lock = threading.Lock()

cycle_lock = threading.Lock()


# ============================================================
# State
# ============================================================

STATE = {}


# ============================================================
# Utility
# ============================================================

def now_epoch():

    return int(
        time.time()
    )


def format_timestamp(
    timestamp
):

    if not timestamp:

        return "N/A"


    try:

        return time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(
                timestamp
            )
        )

    except Exception:

        return "N/A"


def sleep_interruptible(
    seconds
):

    """
    Sleep in small chunks so SIGTERM/shutdown
    can stop the process cleanly.
    """

    seconds = max(
        0,
        int(seconds)
    )


    end_time = (
        time.time()
        + seconds
    )


    while not shutdown_event.is_set():

        remaining = (
            end_time
            - time.time()
        )


        if remaining <= 0:

            return


        shutdown_event.wait(
            min(
                remaining,
                5
            )
        )


# ============================================================
# Parse Instagram Wait Time
# ============================================================

def parse_wait_seconds(
    error
):

    if not error:

        return 0


    text = str(
        error
    )


    patterns = [

        # wait 666 seconds
        r"wait\s+(\d+)\s+seconds",

        # retry in 666 seconds
        r"retry(?:ing)?\s+in\s+(\d+)\s+seconds",

        # try again in 666
        r"try\s+again\s+in\s+(\d+)",

        # 666 seconds
        r"(\d+)\s+seconds",

    ]


    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )


        if not match:

            continue


        try:

            return int(
                match.group(1)
            )

        except (
            ValueError,
            TypeError
        ):

            pass


    return 0


# ============================================================
# Rate Limit Detection
# ============================================================

def is_rate_limit_error(
    error
):

    text = str(
        error
    ).lower()


    return any([
        "429" in text,
        "too many requests" in text,
        "too many" in text,
        "rate limit" in text,
        "rate-limit" in text,
        "rate limited" in text,
    ])


# ============================================================
# Cooldown
# ============================================================

def get_remaining_cooldown():

    remaining = (
        rate_limit_until
        - time.time()
    )


    if remaining <= 0:

        return 0


    return max(
        1,
        int(
            remaining
        )
    )


def is_rate_limited():

    return (
        get_remaining_cooldown()
        > 0
    )


# ============================================================
# Persist Runtime State
# ============================================================

def load_state():

    global rate_limit_until
    global backoff_seconds


    if not os.path.exists(
        STATE_FILE
    ):

        print(
            "[STATE] State file does not exist:"
            f" {STATE_FILE}"
        )

        return {}


    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(
                f
            )


        if not isinstance(
            data,
            dict
        ):

            print(
                "[STATE] Invalid state format. "
                "Starting fresh."
            )

            return {}


        # ----------------------------------------------------
        # New state format
        # ----------------------------------------------------

        posts = data.get(
            "posts",
            {}
        )


        if isinstance(
            posts,
            dict
        ):

            state = posts

        else:

            # ------------------------------------------------
            # Backward compatibility:
            #
            # old state was:
            #
            # {
            #   "account": "shortcode"
            # }
            # ------------------------------------------------

            state = {
                key: value
                for key, value in data.items()
                if key not in (
                    "_meta",
                    "posts"
                )
            }


        # ----------------------------------------------------
        # Restore cooldown
        # ----------------------------------------------------

        if PERSIST_COOLDOWN:

            meta = data.get(
                "_meta",
                {}
            )


            if isinstance(
                meta,
                dict
            ):

                saved_cooldown_until = meta.get(
                    "rate_limit_until",
                    0
                )


                saved_backoff = meta.get(
                    "backoff_seconds",
                    INITIAL_BACKOFF
                )


                try:

                    saved_cooldown_until = float(
                        saved_cooldown_until
                    )

                except (
                    ValueError,
                    TypeError
                ):

                    saved_cooldown_until = 0


                try:

                    saved_backoff = int(
                        saved_backoff
                    )

                except (
                    ValueError,
                    TypeError
                ):

                    saved_backoff = INITIAL_BACKOFF


                # Only restore a future cooldown.
                if (
                    saved_cooldown_until
                    > time.time()
                ):

                    rate_limit_until = (
                        saved_cooldown_until
                    )


                    backoff_seconds = min(
                        max(
                            saved_backoff,
                            INITIAL_BACKOFF
                        ),
                        MAX_BACKOFF
                    )


                    remaining = get_remaining_cooldown()


                    print(
                        "[STATE] Restored Instagram "
                        f"cooldown: {remaining}s"
                    )


        print(
            f"[STATE] Loaded "
            f"{len(state)} account states."
        )


        return state


    except Exception as e:

        print(
            "[STATE] Failed to load state:"
            f" {e}"
        )

        return {}


def save_state(
    state
):

    try:

        state_path = Path(
            STATE_FILE
        )


        state_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )


        meta = {

            "rate_limit_until":
                (
                    rate_limit_until
                    if PERSIST_COOLDOWN
                    else 0
                ),

            "backoff_seconds":
                backoff_seconds,

            "updated_at":
                now_epoch(),

            "version":
                2

        }


        payload = {

            "_meta":
                meta,

            "posts":
                state

        }


        temp_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(
                state_path.parent
            ),
            prefix=".state_",
            suffix=".tmp",
            delete=False
        )


        temp_path = temp_file.name


        try:

            json.dump(
                payload,
                temp_file,
                ensure_ascii=False,
                indent=2
            )


            temp_file.flush()

            os.fsync(
                temp_file.fileno()
            )


        finally:

            temp_file.close()


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
            "[STATE] Failed to save state:"
            f" {e}"
        )


        try:

            if "temp_path" in locals():

                os.remove(
                    temp_path
                )

        except OSError:

            pass


        return False


# Load after all functions are available.
STATE = load_state()


# ============================================================
# Trigger Backoff
# ============================================================

def trigger_backoff(
    error=None,
    explicit_wait=None
):

    global rate_limit_until
    global backoff_seconds


    # --------------------------------------------------------
    # Determine Instagram-provided wait
    # --------------------------------------------------------

    instagram_wait = 0


    if explicit_wait:

        instagram_wait = int(
            explicit_wait
        )


    if not instagram_wait:

        instagram_wait = parse_wait_seconds(
            error
        )


    # --------------------------------------------------------
    # Decide wait time
    # --------------------------------------------------------

    if instagram_wait > 0:

        wait_time = (
            instagram_wait
            + RATE_LIMIT_BUFFER
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


    # --------------------------------------------------------
    # Update cooldown
    # --------------------------------------------------------

    rate_limit_until = (
        time.time()
        + wait_time
    )


    # --------------------------------------------------------
    # Exponential backoff
    # --------------------------------------------------------

    backoff_seconds = min(
        max(
            INITIAL_BACKOFF,
            backoff_seconds * 2
        ),
        MAX_BACKOFF
    )


    print("")

    print(
        "=" * 70
    )

    print(
        "🚨 INSTAGRAM 429 RATE LIMIT"
    )

    print(
        "=" * 70
    )

    print(
        f"⏳ Cooldown: "
        f"{wait_time}s "
        f"({wait_time / 60:.1f} minutes)"
    )

    print(
        "🕐 Resume around: "
        f"{format_timestamp(rate_limit_until)}"
    )

    print(
        f"📈 Next backoff base: "
        f"{backoff_seconds}s"
    )

    print(
        "=" * 70
    )


    # --------------------------------------------------------
    # Save cooldown immediately
    # --------------------------------------------------------

    if PERSIST_COOLDOWN:

        save_state(
            STATE
        )


def reset_backoff():

    global backoff_seconds

    backoff_seconds = INITIAL_BACKOFF


# ============================================================
# Instagram Profile
# ============================================================

def get_latest_post(
    username
):

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


    # IMPORTANT:
    #
    # Only retrieve the first post.
    #
    # Do not convert the whole iterator to list().
    #
    post = next(
        posts,
        None
    )


    return post


# ============================================================
# Media
# ============================================================

def get_post_media(
    post
):

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
                "[MEDIA] Carousel items: "
                f"{len(children)}"
            )


            for index, child in enumerate(
                children,
                start=1
            ):

                if (
                    len(media)
                    >= MAX_MEDIA
                ):

                    break


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

        if is_rate_limit_error(e):

            raise InstagramRateLimited(
                wait_seconds=(
                    parse_wait_seconds(e)
                ),
                message=str(e)
            )


        print(
            "[MEDIA] Failed to get media:"
            f" {e}"
        )


    return media[:MAX_MEDIA]


# ============================================================
# Download File
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
            timeout=HTTP_TIMEOUT
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
                        "Discord safety limit."
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


    except requests.RequestException as e:

        print(
            "[MEDIA] HTTP download error:"
            f" {e}"
        )


    except Exception as e:

        print(
            "[MEDIA] Download error:"
            f" {e}"
        )


    try:

        os.remove(
            temp_path
        )

    except OSError:

        pass


    return None


# ============================================================
# Discord
# ============================================================

def send_discord_notification(
    username,
    post
):

    if not WEBHOOK_URL:

        print(
            "[DISCORD] Webhook URL missing."
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
        "[DISCORD] Preparing "
        f"{len(media)} media."
    )


    temp_files = []

    file_handles = []

    attachments = []


    try:

        # ----------------------------------------------------
        # Main embed
        # ----------------------------------------------------

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
                    "Instagram → Discord"
            }

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

                mime_type = "video/mp4"


            else:

                extension = ".jpg"

                filename = (
                    f"instagram_{index}.jpg"
                )

                mime_type = "image/jpeg"


            print(
                "[DISCORD] Processing media "
                f"#{index} ({media_type})"
            )


            file_path = download_file(
                item["url"],
                extension,
                "ig_"
            )


            if file_path is None:

                print(
                    "[DISCORD] Skipping media "
                    f"#{index}"
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
            # Image embed
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

                # Discord can display uploaded video files,
                # so don't waste an embed slot on a fake
                # attachment image.

                embeds.append({

                    "url":
                        post_url,

                    "description":
                        f"🎬 Video #{index}",

                    "color":
                        15467852

                })


        # ----------------------------------------------------
        # Safety:
        #
        # 1 main embed + MAX_MEDIA media embeds
        #
        # MAX_MEDIA <= 9
        # ----------------------------------------------------

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
            "[DISCORD] Uploading "
            f"{len(attachments)} files..."
        )


        response = HTTP.post(

            WEBHOOK_URL,

            data={
                "payload_json":
                    payload_json
            },

            files=attachments,

            timeout=DISCORD_TIMEOUT

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
                "✅ [DISCORD] "
                "Successfully sent."
            )

            return True


        # ----------------------------------------------------
        # Discord rate limit
        # ----------------------------------------------------

        if response.status_code == 429:

            retry_after = 0


            try:

                data = response.json()

                retry_after = int(
                    data.get(
                        "retry_after",
                        0
                    )
                )

            except Exception:

                pass


            if not retry_after:

                try:

                    retry_after = int(
                        response.headers.get(
                            "Retry-After",
                            "0"
                        )
                    )

                except Exception:

                    retry_after = 0


            print(
                "🚨 [DISCORD] "
                "Discord rate limit."
            )


            if retry_after:

                print(
                    "⏳ Discord retry after: "
                    f"{retry_after}s"
                )


            return False


        print(
            "❌ [DISCORD] Webhook failed:"
        )


        print(
            response.text[:2000]
        )


        return False


    except requests.RequestException as e:

        print(
            "❌ [DISCORD] Request error:"
            f" {e}"
        )

        return False


    except InstagramRateLimited:

        raise


    except Exception as e:

        print(
            "❌ [DISCORD] Unexpected error:"
            f" {e}"
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
        # Remove temporary files
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
    # Never start an Instagram request during cooldown.
    # --------------------------------------------------------

    remaining = get_remaining_cooldown()


    if remaining > 0:

        print(
            f"[{username}] "
            "Instagram cooldown active: "
            f"{remaining}s"
        )

        return False


    try:

        print("")

        print(
            "=" * 65
        )

        print(
            f"🔍 Checking @{username}"
        )

        print(
            "=" * 65
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
            f"[{username}] Latest: "
            f"{shortcode}"
        )


        # ----------------------------------------------------
        # First run
        # ----------------------------------------------------

        if previous_shortcode is None:

            print(
                f"[{username}] FIRST RUN."
            )


            print(
                f"[{username}] Saving "
                f"{shortcode} "
                "without notification."
            )


            state[username] = shortcode


            if not save_state(
                state
            ):

                print(
                    f"⚠️ [{username}] "
                    "State save failed."
                )


            return True


        # ----------------------------------------------------
        # No new post
        # ----------------------------------------------------

        if previous_shortcode == shortcode:

            print(
                f"[{username}] "
                "No new post."
            )


            return True


        # ----------------------------------------------------
        # New post
        # ----------------------------------------------------

        print("")

        print(
            "🚨 NEW POST "
            f"@{username}"
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

        success = (
            send_discord_notification(
                username,
                post
            )
        )


        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Only update state after Discord succeeds.
        # ----------------------------------------------------

        if success:

            state[username] = shortcode


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
                    "Discord succeeded but "
                    "state save failed."
                )


            return True


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
    # Custom 429
    # ========================================================

    except InstagramRateLimited as e:

        print("")

        print(
            f"🚨 [{username}] "
            "Instagram 429 detected."
        )


        wait_time = (
            e.wait_seconds
            if e.wait_seconds
            else parse_wait_seconds(e)
        )


        trigger_backoff(
            error=e,
            explicit_wait=wait_time
        )


        return False


    # ========================================================
    # Instaloader 429
    # ========================================================

    except instaloader.exceptions.TooManyRequestsException as e:

        print(
            f"🚨 [{username}] "
            "Instagram returned 429."
        )


        trigger_backoff(
            e
        )


        return False


    # ========================================================
    # Connection Error
    # ========================================================

    except instaloader.exceptions.ConnectionException as e:

        print(
            f"❌ [{username}] "
            "Instagram connection error: "
            f"{e}"
        )


        if is_rate_limit_error(e):

            print(
                f"🚨 [{username}] "
                "Connection error appears "
                "to be a rate limit."
            )


            trigger_backoff(
                e
            )


            return False


        return True


    # ========================================================
    # Profile does not exist
    # ========================================================

    except instaloader.exceptions.ProfileNotExistsException:

        print(
            f"⚠️ [{username}] "
            "Profile does not exist."
        )


        return True


    # ========================================================
    # Private profile
    # ========================================================

    except instaloader.exceptions.PrivateProfileNotFollowedException:

        print(
            f"⚠️ [{username}] "
            "Private profile cannot be accessed."
        )


        return True


    # ========================================================
    # Login Required
    # ========================================================

    except instaloader.exceptions.LoginRequiredException as e:

        print(
            f"❌ [{username}] "
            "Instagram requires login:"
            f" {e}"
        )


        return True


    # ========================================================
    # Generic Instaloader
    # ========================================================

    except instaloader.exceptions.InstaloaderException as e:

        print(
            f"❌ [{username}] "
            f"Instaloader error: {e}"
        )


        if is_rate_limit_error(e):

            print(
                f"🚨 [{username}] "
                "Instaloader error looks "
                "like rate limiting."
            )


            trigger_backoff(
                e
            )


            return False


        return True


    # ========================================================
    # Unexpected
    # ========================================================

    except Exception as e:

        print(
            f"❌ [{username}] "
            f"Unexpected error: {e}"
        )


        if is_rate_limit_error(e):

            print(
                f"🚨 [{username}] "
                "Unexpected error appears "
                "to be Instagram rate limiting."
            )


            trigger_backoff(
                e
            )


            return False


        return True


# ============================================================
# Optional Instagram Session
# ============================================================

def load_instagram_session():

    if not IG_SESSION_FILE:

        print(
            "[IG] No session file configured."
        )

        return True


    session_path = Path(
        IG_SESSION_FILE
    )


    if not session_path.exists():

        print(
            "[IG] Session file does not exist:"
            f" {session_path}"
        )

        print(
            "[IG] Continuing without login."
        )

        return True


    if not IG_LOGIN_USERNAME:

        print(
            "[IG] IG_LOGIN_USERNAME is required "
            "when using IG_SESSION_FILE."
        )

        return False


    try:

        print(
            "[IG] Loading Instagram session..."
        )


        L.load_session_from_file(
            IG_LOGIN_USERNAME,
            str(
                session_path
            )
        )


        print(
            "[IG] Instagram session loaded."
        )


        return True


    except Exception as e:

        print(
            "[IG] Failed to load session:"
            f" {e}"
        )


        return False


# ============================================================
# Health Server
# ============================================================

class HealthCheckHandler(
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
                "text/plain; charset=utf-8"
            )


            self.send_header(
                "Cache-Control",
                "no-store"
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

        # Keep Render logs clean.
        return


def run_web_server():

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        HealthCheckHandler
    )


    # Allow clean shutdown.
    server.daemon_threads = True


    print("")

    print(
        f"🌐 Health Server listening "
        f"on 0.0.0.0:{PORT}"
    )


    print(
        "❤️ Health endpoints: "
        "/health /healthz"
    )


    try:

        server.serve_forever(
            poll_interval=1
        )

    except Exception as e:

        print(
            "[WEB] Server stopped:"
            f" {e}"
        )

    finally:

        try:

            server.server_close()

        except Exception:

            pass


# ============================================================
# Signal Handling
# ============================================================

def handle_shutdown(
    signum,
    frame
):

    print("")

    print(
        "🛑 Shutdown signal received."
    )


    shutdown_event.set()


def install_signal_handlers():

    try:

        signal.signal(
            signal.SIGTERM,
            handle_shutdown
        )

    except Exception:

        pass


    try:

        signal.signal(
            signal.SIGINT,
            handle_shutdown
        )

    except Exception:

        pass


# ============================================================
# Configuration Validation
# ============================================================

def validate_configuration():

    valid = True


    if not USERNAMES:

        print(
            "❌ ERROR: "
            "IG_USERNAME is missing."
        )

        valid = False


    if not WEBHOOK_URL:

        print(
            "❌ ERROR: "
            "Discord Webhook is missing."
        )

        valid = False


    if len(
        USERNAMES
    ) > 9:

        print(
            "⚠️ WARNING: "
            f"{len(USERNAMES)} accounts configured."
        )

        print(
            "⚠️ This version is designed "
            "around a small sequential account list."
        )


    duplicate_names = (
        len(USERNAMES)
        != len(
            set(
                USERNAMES
            )
        )
    )


    if duplicate_names:

        print(
            "❌ ERROR: "
            "Duplicate Instagram usernames."
        )

        valid = False


    if CHECK_INTERVAL < 600:

        print(
            "⚠️ WARNING: "
            "CHECK_INTERVAL is below 600 seconds."
        )


    if ACCOUNT_DELAY < 60:

        print(
            "⚠️ WARNING: "
            "ACCOUNT_DELAY is below 60 seconds."
        )


    if INITIAL_BACKOFF <= 0:

        print(
            "❌ ERROR: "
            "INITIAL_BACKOFF must be > 0."
        )

        valid = False


    if MAX_BACKOFF < INITIAL_BACKOFF:

        print(
            "❌ ERROR: "
            "MAX_BACKOFF must be >= INITIAL_BACKOFF."
        )

        valid = False


    return valid


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
        "=" * 70
    )


    print(
        f"📋 Accounts: "
        f"{len(USERNAMES)}"
    )


    for index, username in enumerate(
        USERNAMES,
        start=1
    ):

        print(
            f"   {index}. @{username}"
        )


    print("")

    print(
        f"⏱️ Check interval: "
        f"{CHECK_INTERVAL}s "
        f"({CHECK_INTERVAL / 3600:.1f} hours)"
    )


    print(
        f"⏳ Account delay: "
        f"{ACCOUNT_DELAY}s "
        f"({ACCOUNT_DELAY / 60:.1f} minutes)"
    )


    print(
        f"🚨 Initial 429 backoff: "
        f"{INITIAL_BACKOFF}s "
        f"({INITIAL_BACKOFF / 60:.1f} minutes)"
    )


    print(
        f"🛑 Maximum 429 backoff: "
        f"{MAX_BACKOFF}s "
        f"({MAX_BACKOFF / 3600:.1f} hours)"
    )


    print(
        f"➕ Instagram retry buffer: "
        f"{RATE_LIMIT_BUFFER}s"
    )


    print(
        f"💾 State file: "
        f"{STATE_FILE}"
    )


    print(
        f"💾 Persist cooldown: "
        f"{PERSIST_COOLDOWN}"
    )


    print(
        f"📦 Max Discord file size: "
        f"{MAX_DISCORD_FILE_SIZE / 1000000:.2f} MB"
    )


    print(
        f"🖼️ Max media per post: "
        f"{MAX_MEDIA}"
    )


    print(
        f"🌐 HTTP port: "
        f"{PORT}"
    )


    print(
        "=" * 70
    )


# ============================================================
# Main Cycle
# ============================================================

def run_cycle():

    # --------------------------------------------------------
    # Prevent accidental concurrent cycles.
    # --------------------------------------------------------

    if not cycle_lock.acquire(
        blocking=False
    ):

        print(
            "[CYCLE] Another cycle is already running."
        )

        return


    try:

        print("")

        print(
            "=" * 70
        )


        print(
            "⏰ Starting Instagram check cycle"
        )


        print(
            time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )


        print(
            "=" * 70
        )


        cycle_rate_limited = False

        checked_accounts = 0


        # ----------------------------------------------------
        # Accounts
        # ----------------------------------------------------

        for index, username in enumerate(
            USERNAMES
        ):

            if shutdown_event.is_set():

                break


            # ------------------------------------------------
            # Check global cooldown
            # ------------------------------------------------

            remaining = (
                get_remaining_cooldown()
            )


            if remaining > 0:

                cycle_rate_limited = True


                print("")

                print(
                    "🛑 Global Instagram cooldown "
                    "active."
                )


                print(
                    f"⏳ Remaining: "
                    f"{remaining}s"
                )


                break


            # ------------------------------------------------
            # Check account
            # ------------------------------------------------

            success = check_account(
                username,
                STATE
            )


            checked_accounts += 1


            # ------------------------------------------------
            # 429
            # ------------------------------------------------

            if not success:

                if is_rate_limited():

                    cycle_rate_limited = True


                    print("")

                    print(
                        "🛑 Instagram rate limit "
                        "detected."
                    )


                    print(
                        "🛑 Stopping current cycle."
                    )


                    break


            # ------------------------------------------------
            # Delay
            # ------------------------------------------------

            if (
                index
                < len(USERNAMES) - 1
            ):

                if shutdown_event.is_set():

                    break


                print("")

                print(
                    f"⏳ Waiting "
                    f"{ACCOUNT_DELAY}s "
                    "before next account..."
                )


                sleep_interruptible(
                    ACCOUNT_DELAY
                )


        # ----------------------------------------------------
        # Rate limited
        # ----------------------------------------------------

        if cycle_rate_limited:

            remaining = (
                get_remaining_cooldown()
            )


            print("")

            print(
                "🚨 Cycle stopped because "
                "of Instagram rate limiting."
            )


            print(
                f"⏳ Cooldown remaining: "
                f"{remaining}s"
            )


            return


        # ----------------------------------------------------
        # Successful cycle
        # ----------------------------------------------------

        reset_backoff()


        # Save state even when no new posts.
        save_state(
            STATE
        )


        print("")

        print(
            "=" * 70
        )


        print(
            f"✅ Finished checking "
            f"{checked_accounts}/"
            f"{len(USERNAMES)} accounts."
        )


        print(
            f"😴 Next cycle in "
            f"{CHECK_INTERVAL}s "
            f"({CHECK_INTERVAL / 3600:.1f} hours)"
        )


        print(
            "=" * 70
        )


    finally:

        cycle_lock.release()


# ============================================================
# Main
# ============================================================

def main():

    print("")

    print(
        "🚀 Starting Instagram → Discord monitor..."
    )


    # --------------------------------------------------------
    # Signals
    # --------------------------------------------------------

    install_signal_handlers()


    # --------------------------------------------------------
    # Validate
    # --------------------------------------------------------

    if not validate_configuration():

        return


    print_configuration()


    # --------------------------------------------------------
    # Optional session
    # --------------------------------------------------------

    if not load_instagram_session():

        print(
            "❌ Instagram session initialization failed."
        )

        return


    # --------------------------------------------------------
    # Start health server
    # --------------------------------------------------------

    server_thread = threading.Thread(
        target=run_web_server,
        name="health-server",
        daemon=True
    )


    server_thread.start()


    print(
        "✅ Health server started."
    )


    # --------------------------------------------------------
    # Main loop
    # --------------------------------------------------------

    while not shutdown_event.is_set():

        # ====================================================
        # Global cooldown
        # ====================================================

        remaining = (
            get_remaining_cooldown()
        )


        if remaining > 0:

            print("")

            print(
                "🛑 Instagram cooldown "
                f"still active: {remaining}s"
            )


            # Wake up at most once per minute.
            sleep_interruptible(
                min(
                    remaining,
                    60
                )
            )


            continue


        # ====================================================
        # Run cycle
        # ====================================================

        try:

            run_cycle()


        except InstagramRateLimited as e:

            print(
                "🚨 Unhandled Instagram 429 "
                "escaped cycle."
            )


            trigger_backoff(
                e,
                explicit_wait=e.wait_seconds
            )


        except Exception as e:

            print(
                "❌ Main cycle error:"
                f" {e}"
            )


        # ====================================================
        # After cycle
        # ====================================================

        if shutdown_event.is_set():

            break


        remaining = (
            get_remaining_cooldown()
        )


        if remaining > 0:

            print("")

            print(
                "🛑 Cooldown active after cycle:"
                f" {remaining}s"
            )


            continue


        # ====================================================
        # Normal sleep
        # ====================================================

        print("")

        print(
            f"😴 Sleeping "
            f"{CHECK_INTERVAL}s "
            f"until next cycle."
        )


        sleep_interruptible(
            CHECK_INTERVAL
        )


    # --------------------------------------------------------
    # Shutdown
    # --------------------------------------------------------

    print("")

    print(
        "🛑 Monitor stopped."
    )


    # Save final state.
    save_state(
        STATE
    )


# ============================================================
# Start
# ============================================================

if __name__ == "__main__":

    main()
