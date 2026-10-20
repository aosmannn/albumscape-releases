"""Keeps videos.json: which YouTube video is each popular song's official music video (or official visualizer).

Runs daily on GitHub Actions. It takes Apple Music's public Top Songs charts and, for each song it hasn't seen:
  1. asks Genius for the videos it lists (free; repository secret GENIUS_TOKEN) and checks each one against
     YouTube's free embed info (title and channel), so only the real official video is kept, and
  2. only if that finds nothing, searches YouTube (repository secret YT_API_KEY; uses the daily allowance).
The Albumscape app reads the result, so people get full music videos without needing any key. Only video IDs are
stored, never the videos. A song with no match is looked up again after 30 days.

The matching rules between the BEGIN and END markers are copied from the app (lockscreen_music.py) by
tools/sync_matching.py: don't edit them here.
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

HTTP = requests.Session()  # the shared matching code below uses this name
CHARTS = [f"https://rss.applemarketingtools.com/api/v2/{c}/music/most-played/100/songs.json" for c in ("us", "gb", "ca")]
YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
MAX_LOOKUPS = int(os.environ.get("MAX_LOOKUPS", "300"))  # songs per run (Genius and the embed check are free)
MAX_YT_SEARCHES = int(os.environ.get("MAX_SEARCHES", "95"))  # YouTube requests per run: 100 allowance units each, 10,000 a day
yt_requests = [0]  # counted for real: a song can need two (video, then visualizer)
RETRY_MISSES_AFTER = dt.timedelta(days=30)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST = os.path.join(HERE, "videos.json")

words = lambda s: {w.replace("'", "") for w in re.findall(r"[a-z0-9']+", (s or "").lower().replace("’", "'").replace("‘", "'").replace("`", "'"))} - {""}  # can't == can’t == cant
_words = words


def clean_title(title):
    title = re.sub(r"\(.*?\)|\[.*?\]", "", title)
    return re.sub(r"\s*-\s*(remaster|live|feat).*$", "", title, flags=re.IGNORECASE).strip()


def main_artist(artist):
    return re.split(r",|&|\bfeat\.?|\bft\.?|\bwith\b|\bx\b", artist or "", maxsplit=1, flags=re.I)[0].strip()


def video_key(artist, title):
    plain = lambda s: "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    norm = lambda s: " ".join(re.findall(r"[a-z0-9]+", plain(s).lower().replace("'", "").replace(chr(8217), "")))  # "ADÉLA" -> "adela", "Can't" -> "cant"
    return f"{norm(main_artist(artist))}|{norm(clean_title(title or ''))}"


# --- BEGIN shared with the app ---
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
    # triple j's "Like A Version" sessions are covers recorded for radio: their official video is triple j's own
    # upload ("Artist covers X for Like A Version"), not an artist-channel music video.
    lav = "like a version" in f"{track.get('title', '')} {track.get('album') or ''}".lower()
    lav_song = want_all - {"triple", "j", "like", "a", "version"} - want_artist
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
        if lav:  # for these, only triple j's own upload of this artist's performance of this song counts
            if (channel == {"triple", "j"} and "like a version" in raw.lower() and want_artist <= title and lav_song <= title
                    and not (title & (_NOT_THE_VIDEO - {"live", "cover"})) and best_score < 3):
                best, best_score = item["id"].get("videoId"), 3
                continue
        if (title & _NOT_THE_VIDEO) - want_all - want_full:
            continue
        core = _yt_song_words(raw, want_artist, False)
        if core != want and _yt_song_words(raw, want_artist, True) not in (want, want_full) and core != want_full:
            continue
        squash = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
        by_artist = (want_artist <= channel or (want_artist & channel and "vevo" in channel)
                     or (len(squash(main_artist)) >= 4 and squash(main_artist) in squash(chan_raw)))  # "TravisScottVEVO"
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


def verify_candidate(track, vid):
    """"video", "visualizer" or None for a candidate video, judged by its real title and channel from YouTube's free
    embed info (oEmbed, no allowance). Applies the same strict rules as the search: the right song, the artist's own
    upload, no lyric/audio/alternate/Topic uploads. A video that can't be embedded is None too."""
    r = HTTP.get("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}, timeout=10)
    if r.status_code != 200:
        return None
    d = r.json()
    item = [{"id": {"videoId": vid}, "snippet": {"title": d.get("title", ""), "channelTitle": d.get("author_name", "")}}]
    if _yt_pick(item, track, False):
        return "video"
    if _yt_pick(item, track, True):
        return "visualizer"
    return None


def free_video(track, candidates=None):
    """(id, kind) for the song without the YouTube search: Genius's candidates, each verified. A music video beats a
    visualizer. (None, None) when none passes."""
    best = None
    for vid in (candidates if candidates is not None else genius_candidates(track))[:4]:
        kind = verify_candidate(track, vid)
        if kind == "video":
            return vid, "video"
        if kind == "visualizer" and best is None:
            best = (vid, "visualizer")
    return best or (None, None)
# --- END shared with the app ---


def _gnorm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\s+-\s+.*$", " ", s)
    s = re.sub(r"['’`]", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def genius_direct_ids(artist, title, token):
    """YouTube video ids Genius lists for the song, from the official Genius API (same rules as the website's
    /api/genius service: exact title, right main artist). [] if Genius doesn't know the song."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    main = main_artist(artist)
    query = f"{main} {re.sub(r'[(][^)]*[)]', ' ', title)}".strip()
    r = HTTP.get("https://api.genius.com/search", params={"q": query, "per_page": 8}, headers=headers, timeout=20)
    r.raise_for_status()
    want, want_artist = _gnorm(title), _gnorm(main)
    for hit in r.json().get("response", {}).get("hits", []):
        res = hit.get("result") or {}
        if hit.get("type", "song") != "song" or _gnorm(res.get("title")) != want:
            continue
        name = _gnorm((res.get("primary_artist") or {}).get("name"))
        if not (want_artist and name and (name == want_artist or want_artist in name or name in want_artist)):
            continue
        s = HTTP.get(f"https://api.genius.com/songs/{res['id']}", params={"text_format": "plain"}, headers=headers, timeout=20)
        s.raise_for_status()
        ids = []
        for media in s.json().get("response", {}).get("song", {}).get("media") or []:
            m = re.search(r"(?:[?&]v=|youtu\.be/|/embed/)([\w-]{11})", media.get("url") or "")
            if media.get("provider") == "youtube" and m and m.group(1) not in ids:
                ids.append(m.group(1))
        return ids
    return []


def find_free(artist, title, token):
    """{"id", "kind"} from Genius's candidates, each verified for free; None if none passes. Raises on network trouble."""
    track = {"artist": main_artist(artist), "title": title}
    vid, kind = free_video(track, genius_direct_ids(artist, title, token))
    return {"id": vid, "kind": kind} if vid else None


def find_video(artist, title, key):
    """The official music video, else the official visualizer, by searching YouTube: {"id", "kind"} or None."""
    artist = main_artist(artist)
    track = {"artist": artist, "title": title}
    base = {"part": "snippet", "type": "video", "videoEmbeddable": "true", "videoCategoryId": "10", "maxResults": 8, "key": key}
    yt_requests[0] += 1
    r = HTTP.get(YT_SEARCH, params=dict(base, q=f"{artist} {clean_title(title)} official music video"), timeout=20)
    r.raise_for_status()
    items = r.json().get("items", [])
    vid = _yt_pick(items, track, False)
    if vid:
        return {"id": vid, "kind": "video"}
    vid = _yt_pick(items, track, True)
    if not vid:
        yt_requests[0] += 1
        r = HTTP.get(YT_SEARCH, params=dict(base, q=f"{artist} {clean_title(title)} official visualizer"), timeout=20)
        r.raise_for_status()
        vid = _yt_pick(r.json().get("items", []), track, True)
    return {"id": vid, "kind": "visualizer"} if vid else None


def chart_songs():
    songs, seen = [], set()
    for url in CHARTS:
        results = []
        for attempt in range(3):
            try:
                results = HTTP.get(url, timeout=30).json()["feed"]["results"]
                break
            except (requests.RequestException, ValueError, KeyError) as e:
                print(f"chart unavailable (try {attempt + 1}): {url}: {e}")
                time.sleep(5)
        for s in results:
            k = video_key(s["artistName"], s["name"])
            if k not in seen:
                seen.add(k)
                songs.append((k, s["artistName"], s["name"]))
    return songs


def main():
    genius_token, yt_key = os.environ.get("GENIUS_TOKEN"), os.environ.get("YT_API_KEY")
    if not genius_token and not yt_key:
        sys.exit("Set GENIUS_TOKEN (a free Genius API token) and/or YT_API_KEY (a YouTube Data API v3 key).")
    data = {"videos": {}}
    if os.path.exists(LIST):
        with open(LIST, encoding="utf-8") as f:
            data = json.load(f)
    videos = data.setdefault("videos", {})
    today = dt.date.today()
    songs = chart_songs()

    def due(k):
        e = videos.get(k)
        if e is None:
            return True
        return not e.get("id") and today - dt.date.fromisoformat(e.get("checked", "2000-01-01")) >= RETRY_MISSES_AFTER

    todo = [s for s in songs if due(s[0])]
    yt_requests[0] = 0
    print(f"{len(songs)} chart songs, {len(todo)} to look up (up to {MAX_LOOKUPS}; up to {MAX_YT_SEARCHES} YouTube requests)")
    found = {"genius": 0, "youtube": 0}
    missed = skipped = 0
    genius_ok, yt_open = bool(genius_token), bool(yt_key)
    for k, artist, title in todo[:MAX_LOOKUPS]:
        v, source = None, None
        genius_answered = not genius_token  # nothing to ask counts as "answered"
        if genius_ok:
            try:
                v = find_free(artist, title, genius_token)
                genius_answered = True
                source = "genius"
                time.sleep(0.25)  # be gentle with Genius
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code in (401, 403, 429):
                    print(f"Genius said {e.response.status_code}: not asking it again this run")
                    genius_ok = False
            except (requests.RequestException, ValueError) as e:
                print(f"  ? {artist} - {title}: {str(e)[:70]}")
        yt_answered = not yt_key
        if not v and genius_answered and yt_key and yt_open and yt_requests[0] + 2 <= MAX_YT_SEARCHES:
            try:
                v = find_video(artist, title, yt_key)
                yt_answered, source = True, "youtube"
            except requests.HTTPError as e:
                print(f"YouTube said {e.response.status_code if e.response is not None else '?'}: no more YouTube searches this run")
                yt_open = False
            except requests.RequestException as e:
                print(f"  ? {artist} - {title}: {str(e)[:70]}")
        if v:
            found[source] += 1
            videos[k] = dict(v, found=today.isoformat(), source=source)
            print(f"  + {artist} - {title}: {v['id']} ({v['kind']}, via {source})")
        elif genius_answered and yt_answered:  # every source we have said "no": remember that for 30 days
            missed += 1
            videos[k] = {"id": None, "checked": today.isoformat()}
        else:
            skipped += 1  # a source was unavailable or out of allowance: try again next run
    data["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with open(LIST, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    total = sum(1 for e in videos.values() if e.get("id"))
    print(f"found {found['genius']} via Genius and {found['youtube']} via YouTube; {missed} have no official video; "
          f"{skipped} will be tried again; the list has {total} videos")


if __name__ == "__main__":
    main()
