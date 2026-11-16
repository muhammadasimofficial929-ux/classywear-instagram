"""
Runs on GitHub Actions every 15 minutes (see .github/workflows/instagram.yml).
Posts every Instagram item in queue.json whose time has come, then writes the
result to posted.json (the workflow commits that file back to the repo).

Only uses the Python standard library. Secrets come from the repo settings:
  IG_TOKEN    Facebook Page token (has Instagram publishing permission)
  IG_USER_ID  Instagram business account id
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GRAPH = os.environ.get("GRAPH_URL", "https://graph.facebook.com")
TOKEN = os.environ["IG_TOKEN"]
IG = os.environ["IG_USER_ID"]
REPO = os.environ["REPO"]
BRANCH = os.environ.get("BRANCH", "main")
RAW = os.environ.get("RAW_BASE", f"https://raw.githubusercontent.com/{REPO}/{BRANCH}")
MAX_PER_RUN = 3

queue = json.loads(Path("queue.json").read_text(encoding="utf-8")) if Path("queue.json").exists() else []
posted = json.loads(Path("posted.json").read_text(encoding="utf-8")) if Path("posted.json").exists() else {}


def call(method, path, params):
    url = f"{GRAPH}/{path}"
    params = dict(params, access_token=TOKEN)
    if method == "GET":
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params))
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8")).get("error", {})
            raise RuntimeError(f"{err.get('message', e.reason)} (code {err.get('code')})") from None
        except ValueError:
            raise RuntimeError(f"HTTP {e.code}") from None


def publish(item):
    reel = item.get("type") == "reel"
    if reel:  # a promo video -> Instagram Reel (also shown in the feed)
        params = {"media_type": "REELS", "share_to_feed": "true", "caption": item["caption"],
                  "video_url": f"{RAW}/videos/{urllib.parse.quote(item['video'])}"}
    else:
        params = {"image_url": f"{RAW}/images/{urllib.parse.quote(item['image'])}", "caption": item["caption"]}
    box = call("POST", f"{IG}/media", params)
    cid = box["id"]
    for _ in range(75 if reel else 30):   # videos take Instagram a few minutes to process
        status = call("GET", cid, {"fields": "status_code"}).get("status_code")
        if status == "FINISHED":
            break
        if status == "ERROR":
            raise RuntimeError("Instagram could not process the " + ("video" if reel else "picture"))
        time.sleep(4)
    media = call("POST", f"{IG}/media_publish", {"creation_id": cid}).get("id", "")
    if reel:  # posted: remove the video from the repo so it does not keep using space
        try:
            Path("videos", item["video"]).unlink()
        except OSError:
            pass
    return media


now = time.time()
due = sorted((q for q in queue if q["ts"] <= now and posted.get(q["id"], {}).get("status") not in ("published", "failed")),
             key=lambda q: q["ts"])
for item in due[:MAX_PER_RUN]:
    rec = posted.get(item["id"], {})
    try:
        media = publish(item)
        posted[item["id"]] = {"status": "published", "media_id": media, "at": int(time.time())}
        print(f"posted {item.get('image') or item.get('video')}")
    except Exception as e:  # keep going with the next one
        tries = rec.get("tries", 0) + 1
        posted[item["id"]] = {"status": "failed" if tries >= 3 else "retry", "tries": tries, "error": str(e)}
        print(f"could not post {item.get('image') or item.get('video')}: {e}")

posted["_last_run"] = {"at": int(time.time()), "due": len(due)}
Path("posted.json").write_text(json.dumps(posted, indent=1, ensure_ascii=False), encoding="utf-8")
