#!/usr/bin/env python3
"""Email new Slickdeals deals (with product image) via Gmail.

Reads an RSS feed, compares item GUIDs against seen.json, and emails each new
item. On the very first run it only records what's already in the feed, so you
don't get a flood of old deals.

Environment variables:
  GMAIL_USER          Gmail address that sends the email (required unless DRY_RUN)
  GMAIL_APP_PASSWORD  16-character Gmail app password (required unless DRY_RUN)
  TO_EMAIL            Where to send alerts (defaults to GMAIL_USER)
  FEED_URL            RSS feed (defaults to Slickdeals Frontpage)
  BATCH               "true" = one email per run listing all new deals;
                      anything else = one email per deal (default)
  KEYWORDS            Optional comma-separated words; only deals whose title
                      contains one of them are emailed (case-insensitive)
  DRY_RUN             "true" = print emails instead of sending
  SEEN_FILE           Path of the state file (default: seen.json)
"""

import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from email.utils import make_msgid

DEFAULT_FEED = (
    "https://slickdeals.net/newsearch.php?mode=frontpage"
    "&searcharea=deals&searchin=first&rss=1"
)
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
UA = "Mozilla/5.0 (compatible; slickdeals-alert/1.0)"
MAX_SEEN = 2000  # keep the state file small
MAX_EMAILS_PER_RUN = 25  # safety cap


def env_true(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        body = (it.findtext("content:encoded", namespaces=NS) or
                it.findtext("description") or "").strip()
        m = re.search(r'<img[^>]+src="([^"]+)"', body)
        image = html.unescape(m.group(1)) if m else None
        items.append({"guid": guid, "title": title, "link": link,
                      "body": body, "image": image,
                      "pubDate": (it.findtext("pubDate") or "").strip()})
    return items


def load_seen(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # first run


def save_seen(path, seen):
    with open(path, "w") as f:
        json.dump(seen[-MAX_SEEN:], f, indent=0)
        f.write("\n")


def strip_img(body):
    """Remove the feed's own <img> so we can place our inline copy instead."""
    return re.sub(r"<img[^>]*>", "", body, count=1)


def deal_html(item, cid):
    img = (f'<a href="{html.escape(item["link"])}"><img src="cid:{cid[1:-1]}" '
           f'alt="" width="300" style="max-width:300px;height:auto;border:0"></a>'
           if cid else
           (f'<img src="{html.escape(item["image"])}" width="300" alt="">'
            if item["image"] else ""))
    return (
        '<div style="font-family:Arial,sans-serif;max-width:620px;'
        'margin-bottom:28px">'
        f'<h2 style="font-size:18px;margin:0 0 10px">'
        f'<a href="{html.escape(item["link"])}" style="color:#1a5fb4">'
        f'{html.escape(item["title"])}</a></h2>'
        f'{img}'
        f'<div style="font-size:14px;line-height:1.45;margin-top:10px">'
        f'{strip_img(item["body"])}</div>'
        f'<p><a href="{html.escape(item["link"])}">View on Slickdeals →</a></p>'
        '</div>'
    )


def build_message(items, sender, to):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if len(items) == 1:
        msg["Subject"] = items[0]["title"][:200]
    else:
        msg["Subject"] = f"{len(items)} new Slickdeals: {items[0]['title'][:120]}"

    images = []  # (cid, bytes, subtype)
    parts = []
    for it in items:
        cid = None
        if it["image"]:
            try:
                data = fetch(it["image"], timeout=20)
                cid = make_msgid(domain="slickdeals-alert")
                subtype = "png" if data[:4] == b"\x89PNG" else (
                    "gif" if data[:3] == b"GIF" else "jpeg")
                images.append((cid, data, subtype))
            except Exception as e:  # fall back to hotlinked image
                print(f"  image download failed ({e}); linking instead")
                cid = None
        parts.append(deal_html(it, cid))

    plain = "\n\n".join(f"{it['title']}\n{it['link']}" for it in items)
    msg.set_content(plain)
    msg.add_alternative("<html><body>" + "".join(parts) + "</body></html>",
                        subtype="html")
    html_part = msg.get_payload()[1]
    for cid, data, subtype in images:
        html_part.add_related(data, maintype="image", subtype=subtype, cid=cid)
    return msg


def send(msgs, user, password):
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(user, password)
        for m in msgs:
            s.send_message(m)


def main():
    feed_url = os.environ.get("FEED_URL") or DEFAULT_FEED
    seen_path = os.environ.get("SEEN_FILE", "seen.json")
    dry = env_true("DRY_RUN")
    batch = env_true("BATCH")
    user = os.environ.get("GMAIL_USER", "")
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    to = os.environ.get("TO_EMAIL") or user
    keywords = [k.strip().lower() for k in
                os.environ.get("KEYWORDS", "").split(",") if k.strip()]

    if not dry and not (user and pw):
        sys.exit("GMAIL_USER and GMAIL_APP_PASSWORD must be set")

    src = os.environ.get("FEED_FILE")
    xml_bytes = open(src, "rb").read() if src else fetch(feed_url)
    items = parse_feed(xml_bytes)
    print(f"Feed has {len(items)} items")

    seen = load_seen(seen_path)
    if seen is None:
        save_seen(seen_path, [it["guid"] for it in reversed(items)])
        print("First run: recorded current deals, sent nothing.")
        return

    seen_set = set(seen)
    new = [it for it in reversed(items) if it["guid"] not in seen_set]  # oldest first
    if keywords:
        matched = [it for it in new
                   if any(k in it["title"].lower() for k in keywords)]
    else:
        matched = new
    print(f"{len(new)} new, {len(matched)} to email")

    to_send = matched[-MAX_EMAILS_PER_RUN:]
    if to_send:
        groups = [to_send] if batch else [[it] for it in to_send]
        msgs = [build_message(g, user or "dry-run@example.com",
                              to or "dry-run@example.com") for g in groups]
        if dry:
            for m in msgs:
                print("----", m["Subject"])
            if os.environ.get("DRY_RUN_EML"):
                with open(os.environ["DRY_RUN_EML"], "wb") as f:
                    f.write(bytes(msgs[0]))
        else:
            send(msgs, user, pw)
            print(f"Sent {len(msgs)} email(s) to {to}")

    # Mark everything new as seen (including keyword-filtered ones)
    save_seen(seen_path, seen + [it["guid"] for it in new])


if __name__ == "__main__":
    main()
