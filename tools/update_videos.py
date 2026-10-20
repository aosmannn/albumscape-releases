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


_words = words


_NOT_THE_VIDEO = {"lyric", "lyrics", "audio", "live", "reaction", "cover", "remix", "slowed",
                  "reverb", "sped", "instrumental", "karaoke", "8d", "nightcore", "teaser", "trailer", "snippet",
                  "behind", "dance", "tutorial", "review", "edit", "alternate", "alt", "demo", "acoustic", "unreleased",
                  "leaked", "leak", "mashup", "extended", "stripped", "interlude", "preview"}
_VISUALIZER = {"visualizer", "visualiser", "visualizr"}


_YT_NOISE = {"official", "music", "video", "visualizer", "visualiser", "visualizr", "hd", "4k", "audio", "explicit", "clean",
             "lyric", "lyrics", "mv", "m", "v", "uncensored", "remastered"}


def _yt_song_words(raw, artist_words, keep_parens):
    """The song-title words in a YouTube title: "Artist - Song (Official Video) ft. X" -> {song}."""
    import html
    t = html.unescape(raw)
    if not keep_parens:
        t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)
    t = re.split(r"\b(?:ft|feat|featuring)\b\.?", t, maxsplit=1, flags=re.I)[0]
    parts = re.split(r"\s[-–—|]\s", t, maxsplit=1)
    if len(parts) == 2:  # "Artist - Song" (or "Song - Artist")
        left, right = _words(parts[0]), _words(parts[1])
        t = parts[1] if (left & artist_words or not right & artist_words) else parts[0]
    return _words(t) - artist_words - _YT_NOISE


def _yt_pick(items, track, want_visualizer):
    """Best official upload among YouTube search results. A music video (want_visualizer False) or an
    official visualizer (True): the looping artwork video many artists upload when a song has no video.
    The song title must match exactly (no "Extra Luv" for "Extra"), and YouTube's automatic
    "Artist - Topic" uploads (a still cover with the audio) never count."""
    import html
    main_artist = re.split(r",|&|\bfeat\.?|\bft\.?|\bwith\b|\bx\b", track.get("artist") or "", maxsplit=1, flags=re.I)[0].strip()
    track = dict(track, artist=main_artist)  # "Drake, 21 Savage" -> "Drake": the uploader is the main artist
    want_artist = _words(main_artist)
    want_all = _words(clean_title(track.get("title", "")))
    want = want_all - want_artist or want_all
    want_full = _words(track.get("title", "")) - want_artist or want_all  # keeps "(triple j Like A Version)"
    best, best_score = None, 0
    for item in items:
        sn = item.get("snippet", {})
        raw = html.unescape(sn.get("title", ""))
        title = _words(raw)
        is_vis = bool(title & _VISUALIZER)
        if is_vis != want_visualizer:
            continue
        chan_raw = html.unescape(sn.get("channelTitle", ""))
        if re.search(r"-\s*topic\s*$", chan_raw, re.I):
            continue  # YouTube's automatic audio-only upload
        channel = _words(chan_raw.replace("VEVO", " vevo").replace("Vevo", " vevo"))
        if (title & _NOT_THE_VIDEO) - want_all - want_full:
            continue
        core = _yt_song_words(raw, want_artist, False)
        if core != want and _yt_song_words(raw, want_artist, True) not in (want, want_full) and core != want_full:
            continue
        by_artist = want_artist <= channel or (want_artist & channel and "vevo" in channel)
        if not by_artist:
            # someone else's upload: must read "Artist - Song", not "Other Artist - Song ft. Artist"
            low = raw.lower()
            a, b = low.find(track["artist"].lower()), low.find(clean_title(track["title"]).lower())
            if a < 0 or b < 0 or a > b:
                continue
            if is_vis and "official" not in title:
                continue  # a fan-made visualizer: only the artist's channel or an "Official" one counts
        score = (3 if by_artist else 0) + (2 if "official" in title and (is_vis or "video" in title) else 0) + (1 if "vevo" in channel else 0)
        if score > best_score:
            best, best_score = item["id"].get("videoId"), score
    return best if best_score >= 2 else None  # at least the artist's channel, or an "Official ..." upload


def find_video(artist, title, key):
    """The official music video, else the official visualizer: {"id", "kind"} or None. Same rules as the app."""
    artist = main_artist(artist)
    track = {"artist": artist, "title": title}
    base = {"part": "snippet", "type": "video", "videoEmbeddable": "true", "videoCategoryId": "10", "maxResults": 8, "key": key}
    r = requests.get(YT_SEARCH, params=dict(base, q=f"{artist} {clean_title(title)} official music video"), timeout=20)
    r.raise_for_status()
    items = r.json().get("items", [])
    vid = _yt_pick(items, track, False)
    if vid:
        return {"id": vid, "kind": "video"}
    vid = _yt_pick(items, track, True)
    if not vid:
        r = requests.get(YT_SEARCH, params=dict(base, q=f"{artist} {clean_title(title)} official visualizer"), timeout=20)
        r.raise_for_status()
        vid = _yt_pick(r.json().get("items", []), track, True)
    return {"id": vid, "kind": "visualizer"} if vid else None


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
            print(f"  + {artist} - {title}: {v['id']} ({v['kind']})")
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
