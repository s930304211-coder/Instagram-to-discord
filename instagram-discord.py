#!/usr/bin/python
import os
import time
import json
import requests
import instaloader

RAW_USERNAMES = os.environ.get('IG_USERNAME')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')

# 初始化 Instaloader
L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False
)

def webhook(webhook_url, post, username):
    data = {"embeds": []}
    
    # 取得貼文內文與圖片/縮圖
    caption = post.caption if post.caption else ""
    if len(caption) > 500:
        caption = caption[:500] + "..."
        
    embed = {
        "color": 15467852,
        "title": f"New pic of @{username}",
        "url": f"https://www.instagram.com/p/{post.shortcode}/",
        "description": caption,
        "thumbnail": {"url": post.url}
    }
    data["embeds"].append(embed)
    
    res = requests.post(webhook_url, data=json.dumps(data), headers={"Content-Type": "application/json"})
    try:
        res.raise_for_status()
    except Exception as err:
        print(f"[{username}] Error posting to Discord: {err}")
    else:
        print(f"[{username}] Successfully posted to Discord! Code {res.status_code}")

def check_account(username):
    try:
        # 載入公開帳號 Profile
        profile = instaloader.Profile.from_username(L.context, username)
        
        # 取得最新一則貼文
        posts = profile.get_posts()
        latest_post = next(posts, None)
        
        if not latest_post:
            print(f"[{username}] No posts found.")
            return

        shortcode = latest_post.shortcode
        env_key = f"LAST_IMAGE_ID_{username.upper()}"
        
        if os.environ.get(env_key) == shortcode:
            print(f"[{username}] No new image.")
        else:
            os.environ[env_key] = shortcode
            print(f"[{username}] New image found ({shortcode})! Posting to Discord...")
            webhook(WEBHOOK_URL, latest_post, username)

    except Exception as e:
        print(f"[{username}] Error fetching data: {e}")

if __name__ == "__main__":
    if RAW_USERNAMES and WEBHOOK_URL:
        usernames = [u.strip() for u in RAW_USERNAMES.split(",") if u.strip()]
        interval = float(os.environ.get('TIME_INTERVAL') or 600)
        
        print(f"Starting bot for accounts: {usernames}")
        
        while True:
            for username in usernames:
                check_account(username)
                time.sleep(5)  # 間隔 5 秒，避免觸發速率限制
            
            print(f"--- Sleeping for {interval} seconds ---")
            time.sleep(interval)
    else:
        print('Please configure IG_USERNAME and WEBHOOK_URL properly!')
