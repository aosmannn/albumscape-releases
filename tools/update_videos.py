"""Keeps videos.json: which YouTube video is each popular song's official music video.

Runs daily on GitHub Actions with a YouTube Data API key (repository secret YT_API_KEY). It takes Apple Music's
public Top Songs charts, searches YouTube once for each song it hasn't seen, and saves the video ID, so the
Albumscape app can play full music videos without anyone needing their own key. Only video IDs are stored,
never the videos. A song with no match is retried after 30 days.

The matching rules and video_key() mirror the app's (lockscreen_music.py); keep them in step.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata

import requests

CHARTS = [f"https://rss.applemarketingtools.com/api/v2/{c}/music/most-played/100/songs.json" for c in ("us", "gb", "ca")]
YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
MAX_SEARCHES = int(os.environ.get("MAX_SEARCHES", "95"))  # 100 quota units each; the free quota is 10,000 a day
RETRY_MISSES_AFTER = dt.timedelta(days=30)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST = os.path.join(HERE, "videos.json")

NOT_THE_VIDEO = {"lyric", "lyrics", "audio", "visualizer", "visualiser", "live", "reaction", "cover", "remix", "slowed",
                 "reverb", "sped", "instrumental", "karaoke", "8d", "nightcore", "teaser", "trailer", "snippet",
                 "behind", "dance", "tutorial", "review", "edit"}
words = lambda s: set(re.findall(r"[a-z0-9']+", (s or "").lower()))


def clean_title(title):
    title = re.sub(r"\(.*?\)|\[.*?\]", "", title)
    return re.sub(r"\s*-\s*(remaster|live|feat).*$", "", title, flags=re.IGNORECASE).strip()


def main_artist(artist):
    return re.split(r",|&|\bfeat\.?|\bft\.?|\bwith\b|\bx\b", artist or "", maxsplit=1, flags=re.I)[0].strip()


def video_key(artist, title):
    plain = lambda s: "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    norm = lambda s: " ".join(re.findall(r"[a-z0-9']+", plain(s).lower()))  # "ADÉLA" -> "adela"
    return f"{norm(main_artist(artist))}|{norm(clean_title(title or ''))}"


def find_video(artist, title, key):
    """The official music video's ID, or None. Same strict rules as the app."""
    artist = main_artist(artist)
    want_artist, want_title = words(artist), words(clean_title(title))
    r = requests.get(YT_SEARCH, params={"part": "snippet", "type": "video", "videoEmbeddable": "true",
                                        "videoCategoryId": "10", "maxResults": 8, "key": key,
                                        "q": f"{artist} {clean_title(title)} official music video"}, timeout=20)
    r.raise_for_status()
    best, best_score = None, 0
    for item in r.json().get("items", []):
        sn = item.get("snippet", {})
        raw_title = html.unescape(sn.get("title", ""))
        t = words(raw_title)
        channel = words(html.unescape(sn.get("channelTitle", "")).replace("VEVO", " vevo").replace("Vevo", " vevo"))
        if not want_title <= t or (t & NOT_THE_VIDEO) - want_title:
            continue
        by_artist = want_artist <= channel or bool(want_artist & channel and "vevo" in channel)
        if not by_artist:
            low = raw_title.lower()
            a, s = low.find(artist.lower()), low.find(clean_title(title).lower())
            if a < 0 or s < 0 or a > s:
                continue  # "Other Artist - Song ft. Artist" is a different song
        score = (3 if by_artist else 0) + (2 if {"official", "video"} <= t else 0) + (1 if "vevo" in channel else 0)
        if score > best_score:
            best, best_score = {"id": item["id"].get("videoId"), "title": raw_title,
                                "channel": html.unescape(sn.get("channelTitle", ""))}, score
    return best if best_score >= 2 else None


def main():
    key = os.environ.get("YT_API_KEY")
    if not key:
        sys.exit("Set YT_API_KEY (a YouTube Data API v3 key).")
    data = {"videos": {}}
    if os.path.exists(LIST):
        with open(LIST, encoding="utf-8") as f:
            data = json.load(f)
    videos = data.setdefault("videos", {})
    today = dt.date.today()

    songs, seen = [], set()
    for url in CHARTS:
        for attempt in range(3):
            try:
                results = requests.get(url, timeout=30).json()["feed"]["results"]
                break
            except (requests.RequestException, ValueError, KeyError) as e:
                print(f"chart unavailable (try {attempt + 1}): {url}: {e}")
                results = []
                time.sleep(5)
        for s in results:
            k = video_key(s["artistName"], s["name"])
            if k not in seen:
                seen.add(k)
                songs.append((k, s["artistName"], s["name"]))

    def due(k):
        e = videos.get(k)
        if e is None:
            return True
        return not e.get("id") and today - dt.date.fromisoformat(e.get("checked", "2000-01-01")) >= RETRY_MISSES_AFTER

    todo = [s for s in songs if due(s[0])]
    print(f"{len(songs)} chart songs, {len(todo)} to look up, searching up to {MAX_SEARCHES}")
    found = searched = 0
    for k, artist, title in todo[:MAX_SEARCHES]:
        try:
            v = find_video(artist, title, key)
        except requests.HTTPError as e:
            print(f"stopping: YouTube said {e.response.status_code} (quota used up?)")
            break
        except requests.RequestException as e:
            print(f"skipped {artist} - {title}: {e}")
            continue
        searched += 1
        if v:
            found += 1
            videos[k] = dict(v, found=today.isoformat())
            print(f"  + {artist} - {title}: {v['id']} ({v['channel']})")
        else:
            videos[k] = {"id": None, "checked": today.isoformat()}
            print(f"  - {artist} - {title}: no official video")
    data["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with open(LIST, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    print(f"searched {searched}, found {found}; the list has {sum(1 for v in videos.values() if v.get('id'))} videos")


if __name__ == "__main__":
    main()
