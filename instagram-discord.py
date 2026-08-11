#!/usr/bin/python
# Copyright (c) 2020 Fernando
# Url: https://github.com/fernandod1/
# License: MIT

import re
import json
import sys
import requests
import os
import time

# 讀取環境變數（支援逗號分隔多個帳號）
RAW_USERNAMES = os.environ.get('IG_USERNAME')

def get_last_publication_url(html):
    return html.json()["graphql"]["user"]["edge_owner_to_timeline_media"]["edges"][0]["node"]["shortcode"]

def get_last_thumb_url(html):
    return html.json()["graphql"]["user"]["edge_owner_to_timeline_media"]["edges"][0]["node"]["thumbnail_src"]

def get_description_photo(html):
    try:
        return html.json()["graphql"]["user"]["edge_owner_to_timeline_media"]["edges"][0]["node"]["edge_media_to_caption"]["edges"][0]["node"]["text"]
    except Exception:
        return ""

def webhook(webhook_url, html, username):
    data = {}
    data["embeds"] = []
    embed = {}
    embed["color"] = 15467852
    embed["title"] = "New pic of @" + username
    embed["url"] = "https://www.instagram.com/p/" + get_last_publication_url(html) + "/"
    embed["description"] = get_description_photo(html)
    embed["thumbnail"] = {"url": get_last_thumb_url(html)}
    data["embeds"].append(embed)
    
    result = requests.post(webhook_url, data=json.dumps(data), headers={"Content-Type": "application/json"})
    try:
        result.raise_for_status()
    except requests.exceptions.HTTPError as err:
        print(f"Error posting for {username}: {err}")
    else:
        print(f"[{username}] Image successfully posted in Discord, code {result.status_code}.")

def get_instagram_html(username):
    headers = {
        "Host": "www.instagram.com",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.11 (KHTML, like Gecko) Chrome/23.0.1271.64 Safari/537.11"
    }
    html = requests.get(f"https://www.instagram.com/{username}/feed/?__a=1", headers=headers)
    return html

def check_account(username):
    try:
        html = get_instagram_html(username)
        last_pub_url = get_last_publication_url(html)
        env_key = f"LAST_IMAGE_ID_{username.upper()}"
        
        if os.environ.get(env_key) == last_pub_url:
            print(f"[{username}] No new image.")
        else:
            os.environ[env_key] = last_pub_url
            print(f"[{username}] New image found! Posting to Discord...")
            webhook(os.environ.get("WEBHOOK_URL"), html, username)
    except Exception as e:
        print(f"[{username}] Error: {e}")

if __name__ == "__main__":
    if RAW_USERNAMES and os.environ.get('WEBHOOK_URL'):
        # 拆分逗號分隔的帳號清單
        usernames = [u.strip() for u in RAW_USERNAMES.split(",") if u.strip()]
        interval = float(os.environ.get('TIME_INTERVAL') or 600)
        
        while True:
            for username in usernames:
                check_account(username)
                time.sleep(2) # 每個帳號檢查間隔 2 秒，避免請求過於頻繁
            
            print(f"--- Sleeping for {interval} seconds ---")
            time.sleep(interval)
    else:
        print('Please configure environment variables properly!')
