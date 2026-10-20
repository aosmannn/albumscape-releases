"""Copies the video-matching code from the Albumscape app (lockscreen_music.py) into update_videos.py, between its
BEGIN/END markers, so the daily list builder and the app judge videos by exactly the same rules.

    python tools/sync_matching.py <path to lockscreen_music.py>
"""
import re
import sys

app_path = sys.argv[1]
here = __file__.rsplit("tools", 1)[0] + "tools/update_videos.py"
app = open(app_path, encoding="utf-8", newline="").read().replace("\r\n", "\n")
block = app[app.index("_NOT_THE_VIDEO = {"):app.index("def youtube_find(")]
# the app's lookup-service helper isn't used here (the list builder talks to Genius directly)
block = re.sub(r'GENIUS_LOOKUP = .*?\n\n\ndef genius_candidates\(.*?\n\n\n(?=def verify_candidate)', "", block, flags=re.S)
words_line = re.search(r"^_words = lambda s:.*$", app, re.M).group(0)

u = open(here, encoding="utf-8", newline="").read().replace("\r\n", "\n")
begin, end = "# --- BEGIN shared with the app ---\n", "# --- END shared with the app ---"
i, j = u.index(begin) + len(begin), u.index(end)
u = u[:i] + block.rstrip("\n") + "\n" + u[j:]
u = re.sub(r"^words = lambda s:.*$", words_line.replace("_words = ", "words = ", 1), u, count=1, flags=re.M)
open(here, "w", encoding="utf-8", newline="").write(u)
print("synced", len(block.splitlines()), "lines from the app")
