import argparse
import hashlib
import json
import configparser
import os
import re
import sqlite3
import tempfile
import uuid
import subprocess
import shutil
import time
from requests.exceptions import HTTPError, ConnectionError, Timeout
from PIL import Image, ImageOps, ImageChops
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/photoslibrary.appendonly",
          "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"]
BASE = "https://photoslibrary.googleapis.com/v1/"
FOLDER = "application/vnd.google-apps.folder"

def video_stream_hash(path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("Install ffmpeg and ffprobe for video integrity fallback")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path], check=True, capture_output=True, text=True, timeout=300)
    streams = json.loads(probe.stdout)["streams"]
    if not streams or any(x["codec_type"] not in ("video", "audio") for x in streams):
        raise ValueError("Unsupported video stream layout")
    signature = [{k:x.get(k) for k in ("codec_type","codec_name","width","height","sample_rate","channels","time_base","duration_ts","start_pts")} for x in streams]
    hashes = subprocess.run(["ffmpeg","-nostdin","-v","error","-xerror","-i",path,"-map","0","-c","copy","-f","streamhash","-hash","sha256","-"], check=True, capture_output=True, text=True, timeout=600)
    if len(hashes.stdout.strip().splitlines()) != len(streams):
        raise ValueError("Incomplete stream hashes")
    return signature, hashes.stdout.strip()

def decoded_video_hash(path):
    # Exact decoded content only: lossy transcoding is not proof of identity.
    probe = subprocess.run(["ffprobe","-v","error","-show_streams","-of","json",path], check=True, capture_output=True, text=True, timeout=300)
    streams = json.loads(probe.stdout)["streams"]
    if not streams or any(x["codec_type"] not in ("video","audio") for x in streams):
        raise ValueError("Unsupported decoded stream layout")
    signature = [{k:x.get(k) for k in ("codec_type","width","height","sample_rate","channels","channel_layout")} for x in streams]
    result = subprocess.run(["ffmpeg","-nostdin","-v","error","-xerror","-i",path,"-map","0","-c:v","rawvideo","-pix_fmt","yuv444p16le","-c:a","pcm_s32le","-f","framehash","-hash","sha256","-"], check=True, capture_output=True, text=True, timeout=1800)
    # Preserve frame timestamps, durations, stream order and hashes.
    lines = [line.strip() for line in result.stdout.splitlines() if not line.startswith("#software")]
    if not any(line and not line.startswith("#") for line in lines):
        raise ValueError("No decoded frames")
    return signature, lines

def processed_video_match(left, right):
    # Conservative comparison of all frames and audio, not just metadata.
    def probe(path):
        return json.loads(subprocess.run(["ffprobe","-v","error","-show_streams","-show_format","-of","json",path], check=True, capture_output=True, text=True, timeout=300).stdout)
    from fractions import Fraction
    import math
    a, b = probe(left), probe(right)
    x, y = a["streams"], b["streams"]
    print("Video codecs:", [t.get("codec_name") for t in x], "->", [t.get("codec_name") for t in y])
    if len(x) != len(y) or sum(t["codec_type"] == "video" for t in x) != 1 or sum(t["codec_type"] == "audio" for t in x) > 1:
        raise ValueError("Unsupported processed stream layout")
    if abs(float(a["format"]["duration"])-float(b["format"]["duration"])) > 0.10:
        raise ValueError("Processed video duration differs")
    for u,v in zip(x,y):
        if u["codec_type"] != v["codec_type"]:
            raise ValueError("Stream types differ")
        if u["codec_type"] == "video":
            if any(u.get(k) != v.get(k) for k in ("width","height","sample_aspect_ratio")):
                raise ValueError("Video geometry differs")
            if abs(float(Fraction(u["avg_frame_rate"]))-float(Fraction(v["avg_frame_rate"]))) > 0.01:
                raise ValueError("Frame rate differs")
        elif u["codec_type"] == "audio":
            if any(u.get(k) != v.get(k) for k in ("channels","channel_layout","sample_rate")):
                raise ValueError("Audio layout differs")
        else:
            raise ValueError("Unsupported stream type")
    with tempfile.TemporaryDirectory() as work:
        # Decode every frame; compare corresponding frames with SSIM.
        stats = os.path.join(work,"ssim.log")
        subprocess.run(["ffmpeg","-nostdin","-v","error","-xerror","-i",left,"-i",right,"-filter_complex",f"[0:v]settb=AVTB,setpts=PTS-STARTPTS[a];[1:v]settb=AVTB,setpts=PTS-STARTPTS[b];[a][b]ssim=stats_file={stats}:shortest=1:repeatlast=0","-an","-f","null","-"],check=True,capture_output=True,timeout=3600)
        scores = [float(m) for m in re.findall(r"All:([0-9.]+)",Path(stats).read_text())]
        def frames(path):
            result = subprocess.run(["ffprobe","-v","error","-count_frames","-select_streams","v:0","-show_entries","stream=nb_read_frames","-of","csv=p=0",path],check=True,capture_output=True,text=True,timeout=3600)
            return int(result.stdout.strip())
        if not scores or len(scores) != frames(left) or len(scores) != frames(right) or min(scores) < 0.95 or sum(scores)/len(scores) < 0.98:
            raise ValueError("Processed video frame similarity insufficient")
        if any(t["codec_type"] == "audio" for t in x):
            # Require exact decoded audio for now; reject lossy audio changes.
            def audio_hash(path):
                return subprocess.run(["ffmpeg","-nostdin","-v","error","-xerror","-i",path,"-map","0:a:0","-c:a","pcm_s32le","-f","hash","-hash","sha256","-"],check=True,capture_output=True,timeout=3600).stdout
            if audio_hash(left) != audio_hash(right):
                raise ValueError("Processed audio differs; retained")
        print("Processed video SSIM:", min(scores), sum(scores)/len(scores), "frames:", len(scores))

def migration_video_check(left, right):
    def probe(path):
        return json.loads(subprocess.run(["ffprobe","-v","error","-show_streams","-show_format","-of","json",path],check=True,capture_output=True,text=True,timeout=300).stdout)
    a,b = probe(left),probe(right)
    import math
    for obj in (a,b):
        duration = float(obj["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Invalid video duration")
    if abs(float(a["format"]["duration"])-float(b["format"]["duration"])) > 0.5:
        raise ValueError("Migration video duration differs")
    for kind in ("video","audio"):
        x = [t for t in a["streams"] if t["codec_type"] == kind]
        y = [t for t in b["streams"] if t["codec_type"] == kind]
        if len(x) != len(y) or (kind == "video" and not x):
            raise ValueError("Migration stream count differs")
        for u,v in zip(x,y):
            if kind == "video" and any(u.get(k) != v.get(k) for k in ("width","height")):
                raise ValueError("Migration video resolution differs")
    subprocess.run(["ffmpeg","-nostdin","-v","error","-xerror","-i",right,"-map","0:v","-map","0:a?","-f","null","-"],check=True,capture_output=True,timeout=3600)

def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description="Copy Drive photos to Google Photos without deleting originals")
    p.add_argument("--config", default=".env/config.ini")
    p.add_argument("--version", action="version", version="0.1.0")
    g = p.add_mutually_exclusive_group()
    for flag in ("init", "auth", "test", "scan", "dry-run", "all", "resume", "status", "retry-failed", "verify", "cleanup"):
        g.add_argument("--" + flag, action="store_true")
    g.add_argument("--create-albums", metavar="YEAR[-YEAR]", help="Create or reuse yearly Photos albums without migrating media")
    p.add_argument("--year", type=int, help="Restrict to a year; alone, upload that year")
    p.add_argument("--limit", type=int, default=0, help="Maximum new uploads this run (0 unlimited)")
    p.add_argument("--accept-processed", action="store_true", help="Allow processed images with matching dimensions and recorded upload ID even if pixels differ")
    p.add_argument("--accept-processed-video", action="store_true", help="Opt in to conservative processed video verification; original quality may differ")
    p.add_argument("--videos-only", action="store_true", help="Restrict migration and verification to videos")
    p.add_argument("--accept-unverified-video", action="store_true", help="Accept playable uploaded videos with matching duration, resolution and stream counts; content identity is not guaranteed")
    a = p.parse_args()
    if a.accept_unverified_video and not a.cleanup:
        p.error("--accept-unverified-video requires --cleanup")
    if a.accept_processed_video and not a.cleanup:
        p.error("--accept-processed-video requires --cleanup")
    if not any((a.init, a.auth, a.test, a.scan, a.dry_run, a.all, a.resume, a.status, a.retry_failed, a.verify, a.cleanup, a.create_albums, a.year)):
        p.error("Specify an action or --year")
    if a.year is not None and not 1000 <= a.year <= 9999:
        p.error("--year must be a four-digit year")
    if a.year and any((a.init, a.auth, a.test, a.status)):
        p.error("--year cannot be combined with --init, --auth, --test or --status")
    album_years = []
    if a.create_albums:
        match = re.fullmatch(r"(\d{4})(?:-(\d{4}))?", a.create_albums)
        if not match or a.year or a.limit or a.accept_processed:
            p.error("--create-albums requires YEAR or YEAR-YEAR, without migration options")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1000 <= first <= last <= 9999 or last-first > 100:
            p.error("Specify an ascending range of at most 101 four-digit years")
        album_years = [str(y) for y in range(first,last+1)]
    if a.cleanup and not a.year:
        p.error("--cleanup requires --year")
    if a.limit < 0:
        p.error("--limit must be nonnegative")
    path = Path(a.config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if a.init:
        with path.open("x") as f:
            f.write("[google]\ncredentials_file = .env/credentials.json\ntoken_file = .env/token.json\n[drive]\nsource_folder = Pictures\nsource_folder_id =\n[photos]\nalbum_prefix =\ninclude_videos = true\n")
        return
    c = configparser.ConfigParser(interpolation=None)
    if not c.read(path):
        p.error("Missing config; run --init")
    db = sqlite3.connect(path.parent / "migration.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, year TEXT, name TEXT, status TEXT, remote TEXT, error TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS albums (year TEXT PRIMARY KEY, id TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS cleanup_history (source_id TEXT PRIMARY KEY, year TEXT, remote TEXT, trashed_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    db.commit()
    if a.status:
        for row in db.execute("SELECT year,status,count(*) FROM media GROUP BY year,status ORDER BY year"):
            print(*row)
        for row in db.execute("SELECT year, count(*) FROM cleanup_history GROUP BY year ORDER BY year"):
            print(row[0], "trash_confirmed", row[1])
        return
    token = Path(c["google"]["token_file"])
    if a.auth or a.cleanup:
        SCOPES[0] = "https://www.googleapis.com/auth/drive"
    elif token.exists() and "https://www.googleapis.com/auth/drive" in json.loads(token.read_text()).get("scopes", []):
        SCOPES[0] = "https://www.googleapis.com/auth/drive"
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if token.exists() and not set(SCOPES).issubset(json.loads(token.read_text()).get("scopes", [])):
        creds = None
    if creds and creds.expired and creds.refresh_token:
        print("Refreshing Google OAuth token", flush=True)
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not a.auth:
            p.error("Run --auth interactively first")
        envfile = Path("env/.env")
        if envfile.exists():
            values = {}
            for line in envfile.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.removeprefix("export ").partition("=")
                if sep:
                    values[key.strip()] = value.strip().strip("'").strip('"')
            if not values.get("CLIENT_ID") or not values.get("CLIENT_SECRET"):
                p.error("env/.env must contain CLIENT_ID and CLIENT_SECRET")
            flow = InstalledAppFlow.from_client_config({"installed": {
                "client_id": values["CLIENT_ID"],
                "client_secret": values["CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"]
            }}, SCOPES)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(c["google"]["credentials_file"], SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
    token.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    if a.auth:
        print("Authenticated")
        return
    print("Connecting to Google Drive and Photos", flush=True)
    drive = build("drive", "v3", credentials=creds)
    http = AuthorizedSession(creds)
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from urllib.parse import urlsplit
    quota_zone = ZoneInfo("America/Los_Angeles")
    db.execute("CREATE TABLE IF NOT EXISTS quota_usage (day TEXT, kind TEXT, count INTEGER NOT NULL, PRIMARY KEY(day,kind))")
    db.commit()
    # Local counters are diagnostic only; Google enforces actual project quotas.
    db.execute("CREATE TABLE IF NOT EXISTS quota_responses (day TEXT, kind TEXT, status INTEGER, count INTEGER NOT NULL, PRIMARY KEY(day,kind,status))")
    db.commit()
    def remaining_api_budget():
        return 10000
    def record_quota_response(url, status):
        kind = "api" if urlsplit(url).hostname == "photoslibrary.googleapis.com" else "media"
        day = datetime.now(quota_zone).date().isoformat()
        db.execute("INSERT INTO quota_responses VALUES (?,?,?,1) ON CONFLICT(day,kind,status) DO UPDATE SET count=count+1", (day,kind,status))
        db.commit()
    last_request = 0.0
    def photos_request(method, url, **kw):
        nonlocal last_request
        delay = 60
        while True:
            time.sleep(max(0, 2.0 - (time.monotonic() - last_request)))
            last_request = time.monotonic()
            r = http.request(method, url, **kw)
            record_quota_response(url, r.status_code)
            if r.status_code != 429:
                return r
            from email.utils import parsedate_to_datetime
            retry = r.headers.get("Retry-After", "")
            try:
                wait = float(retry)
            except ValueError:
                try:
                    wait = parsedate_to_datetime(retry).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    wait = 0
            wait = max(delay, wait)
            r.close()
            print(f"Google Photos rate limit: waiting {wait:.0f} seconds before retry", flush=True)
            time.sleep(wait)
            delay = min(delay * 2, 3600)
    def api(method, endpoint, **kw):
        r = photos_request(method, BASE + endpoint, timeout=120, **kw)
        r.raise_for_status()
        return r.json()
    if album_years:
        # Only albums visible to this OAuth application can be discovered.
        by_title = {}
        page = None
        while True:
            params = {"pageSize": 50}
            if page:
                params["pageToken"] = page
            result = api("GET", "albums", params=params)
            for item in result.get("albums", []):
                by_title.setdefault(item["title"], []).append(item["id"])
            page = result.get("nextPageToken")
            if not page:
                break
        for year in album_years:
            title = c["photos"].get("album_prefix", "") + year
            saved = db.execute("SELECT id FROM albums WHERE year=?", (year,)).fetchone()
            matches = by_title.get(title, [])
            if saved:
                item = api("GET", "albums/" + saved[0])
                if item.get("title") != title:
                    p.error(f"{year}: saved album title differs; inspect before proceeding")
                album_id = saved[0]
                action = "existing saved album"
            elif len(matches) > 1:
                p.error(f"{year}: multiple matching albums; refusing ambiguous selection")
            elif matches:
                album_id = matches[0]
                action = "existing album adopted"
            else:
                album_id = api("POST", "albums", json={"album":{"title":title}})["id"]
                action = "album created"
            db.execute("INSERT OR REPLACE INTO albums VALUES (?,?)", (year,album_id))
            db.commit()
            print(year, action, flush=True)
        db.close()
        return 0
    def upload_payload(payload, mime):
        headers = {"Content-Type":"application/octet-stream","X-Goog-Upload-Protocol":"raw","X-Goog-Upload-Content-Type":mime}
        if mime.startswith("video/"):
            # Restart an interrupted upload session, never an uncertain media creation.
            for attempt in range(3):
                try:
                    with photos_request("POST", BASE+"uploads", headers={"X-Goog-Upload-Protocol":"resumable","X-Goog-Upload-Command":"start","X-Goog-Upload-Raw-Size":str(len(payload)),"X-Goog-Upload-Content-Type":mime}, timeout=120) as start:
                        start.raise_for_status()
                        url = start.headers["X-Goog-Upload-URL"]
                        granularity = int(start.headers.get("X-Goog-Upload-Chunk-Granularity", "262144"))
                    if granularity <= 0:
                        raise ValueError("Invalid upload chunk granularity")
                    chunk_size = max(granularity, (8*1024*1024//granularity)*granularity)
                    offset = 0
                    while offset < len(payload):
                        chunk = payload[offset:offset+chunk_size]
                        final = offset+len(chunk) == len(payload)
                        with photos_request("POST",url,data=chunk,headers={"Content-Type":"application/octet-stream","X-Goog-Upload-Offset":str(offset),"X-Goog-Upload-Command":"upload, finalize" if final else "upload"},timeout=300) as response:
                            response.raise_for_status()
                            if final:
                                return response.text
                        offset += len(chunk)
                    raise ValueError("Empty video")
                except (ConnectionError, Timeout, HTTPError) as exc:
                    if isinstance(exc, HTTPError) and exc.response is not None and exc.response.status_code not in (408,429,500,502,503,504):
                        raise
                    if attempt == 2:
                        raise
                    time.sleep(2**attempt)
        for attempt in range(3):
            try:
                with photos_request("POST",BASE+"uploads",data=payload,headers=headers,timeout=300) as response:
                    response.raise_for_status()
                    return response.text
            except (ConnectionError, Timeout, HTTPError) as exc:
                if isinstance(exc, HTTPError) and exc.response is not None and exc.response.status_code not in (408,429,500,502,503,504):
                    raise
                if attempt == 2:
                    raise
                time.sleep(2**attempt)

    def listing(q):
        page = None
        while True:
            r = drive.files().list(q=q+" and trashed=false", fields="nextPageToken,files(id,name,mimeType,md5Checksum,version,capabilities(canTrash))", pageSize=1000, pageToken=page).execute(num_retries=5)
            yield from r.get("files", [])
            page = r.get("nextPageToken")
            if not page:
                break
    def children(id):
        return listing("'" + id + "' in parents")
    root = c["drive"].get("source_folder_id", "").strip()
    if not root:
        name = c["drive"]["source_folder"].replace("\\", "\\\\").replace("'", "\\'")
        matches = list(listing("name = '"+name+"' and mimeType = '"+FOLDER+"' and 'root' in parents"))
        if len(matches) != 1:
            p.error("Specify source_folder_id: root folder missing or ambiguous")
        root = matches[0]["id"]
    if a.test:
        print("Drive:", drive.files().get(fileId=root, fields="id,name").execute()["name"])
        api("GET", "albums", params={"pageSize":1})
        print("Photos album read successful; no upload tested")
        return
    def walk(id):
        for f in children(id):
            m = f["mimeType"]
            if m == FOLDER:
                yield from walk(f["id"])
            elif m.startswith("image/") or (m.startswith("video/") and c["photos"].getboolean("include_videos", True)):
                yield f
            elif c["photos"].getboolean("include_videos", True) and f["name"].lower().endswith((".mp4",".m4v",".mov",".avi",".mkv",".webm",".3gp",".mpg",".mpeg")) and not m.startswith("application/vnd.google-apps."):
                import mimetypes
                f = dict(f)
                f["mimeType"] = mimetypes.guess_type(f["name"])[0] or "video/mp4"
                print(f["name"], "video detected by extension; Drive MIME:", m, flush=True)
                yield f
    def selected_walk(id):
        for f in walk(id):
            if not a.videos_only or f["mimeType"].startswith("video/"):
                yield f
    db.execute("CREATE TABLE IF NOT EXISTS verified_source (source_id TEXT PRIMARY KEY, version TEXT, remote TEXT, policy INTEGER, kind TEXT)")
    db.commit()
    db.execute("CREATE TABLE IF NOT EXISTS completed_year (year TEXT PRIMARY KEY)")
    db.commit()
    if a.verify or a.cleanup:
        totals = {"verified": 0, "unverified": 0, "trashed": 0, "exact": 0, "pixel_match": 0, "processed": 0, "folders_trashed": 0, "video_stream_match": 0, "processed_video": 0, "migration_video": 0}
        query = "SELECT year,id,name,status,remote FROM media"
        args = ()
        if a.year:
            query += " WHERE year=?"
            args = (str(a.year),)
        rows = list(db.execute(query, args))
        records = {(year, source_id): (status, remote) for year, source_id, name, status, remote in rows}
        inventory = {}
        folders = [f for f in children(root) if f["mimeType"] == FOLDER and re.fullmatch(r"\d{4}", f["name"]) and (not a.year or f["name"] == str(a.year))]
        if a.year and len(folders) != 1:
            p.error("Expected exactly one source year folder")
        for folder in folders:
            for f in selected_walk(folder["id"]):
                inventory[(folder["name"], f["id"])] = f
        if a.videos_only:
            rows = [row for row in rows if (row[0], row[1]) in inventory]
            if not inventory:
                print("No remaining source videos")
                return 0
        if a.year and not inventory and db.execute("SELECT 1 FROM completed_year WHERE year=?", (str(a.year),)).fetchone():
            print(a.year, "already completed; source inventory empty")
            return 0
        memberships = {}
        candidates = []
        for year, source_id, name, status, remote in rows:
            try:
                if status != "done" or not remote:
                    raise ValueError("No confirmed upload")
                if year not in memberships:
                    saved = db.execute("SELECT id FROM albums WHERE year=?", (year,)).fetchone()
                    if not saved:
                        raise ValueError("No recorded album")
                    members = {}
                    page = None
                    while True:
                        body = {"albumId": saved[0], "pageSize": 100}
                        if page:
                            body["pageToken"] = page
                        result = api("POST", "mediaItems:search", json=body)
                        members.update((item["id"], item) for item in result.get("mediaItems", []))
                        page = result.get("nextPageToken")
                        if not page:
                            break
                    memberships[year] = members
                item = memberships[year].get(remote, {})
                if item.get("id") != remote:
                    raise ValueError("Destination missing or outside expected album")
                f = inventory.get((year, source_id))
                if a.cleanup and f:
                    if not f.get("md5Checksum") or not f.get("version") or not f.get("capabilities", {}).get("canTrash"):
                        raise ValueError("Source checksum, version or trash permission missing")
                    cached = db.execute("SELECT kind FROM verified_source WHERE source_id=? AND version=? AND remote=? AND policy=?", (source_id, f["version"], remote, (int(a.accept_processed) | (2 if a.accept_processed_video else 0) | (4 if a.accept_unverified_video else 0)))).fetchone()
                    if cached:
                        totals[cached[0]] += 1
                        candidates.append(f)
                        totals["verified"] += 1
                        print(year, name, "verified from checkpoint")
                        continue
                    if f["mimeType"].startswith("video/"):
                        item = api("GET", "mediaItems/" + remote)
                    if f["mimeType"].startswith("video/") and item.get("mediaMetadata", {}).get("video", {}).get("status") != "READY":
                        raise ValueError("Video not ready; retained for next run")
                    before = {k: totals[k] for k in ("exact", "pixel_match", "processed", "video_stream_match", "processed_video", "migration_video")}
                    digest = hashlib.md5()
                    suffix = "=dv" if f["mimeType"].startswith("video/") else "=d"
                    with tempfile.TemporaryFile() as dest:
                        for attempt in range(4):
                            try:
                                if attempt:
                                    time.sleep(2**attempt)
                                    item = api("GET", "mediaItems/" + remote)
                                dest.seek(0)
                                dest.truncate()
                                digest = hashlib.md5()
                                with photos_request("GET", item["baseUrl"] + suffix, stream=True, timeout=300) as response:
                                    response.raise_for_status()
                                    for chunk in response.iter_content(1024 * 1024):
                                        digest.update(chunk)
                                        dest.write(chunk)
                                break
                            except (HTTPError, ConnectionError, Timeout) as exc:
                                if attempt == 3 or (isinstance(exc, HTTPError) and exc.response is not None and exc.response.status_code not in (400,403,404,408,500,502,503,504)):
                                    raise
                                print(year, name, "download retry", attempt+1, flush=True)
                        if digest.hexdigest() == f["md5Checksum"]:
                            totals["exact"] += 1
                        elif f["mimeType"].startswith("image/"):
                            with tempfile.TemporaryFile() as src:
                                downloader = MediaIoBaseDownload(src, drive.files().get_media(fileId=source_id))
                                done = False
                                while not done:
                                    _, done = downloader.next_chunk(num_retries=5)
                                src.seek(0)
                                check = hashlib.md5()
                                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                                    check.update(chunk)
                                if check.hexdigest() != f["md5Checksum"]:
                                    raise ValueError("Source changed during verification")
                                src.seek(0)
                                dest.seek(0)
                                with Image.open(src) as original, Image.open(dest) as downloaded:
                                    if getattr(original, "n_frames", 1) != 1 or getattr(downloaded, "n_frames", 1) != 1:
                                        raise ValueError("Animated image requires exact checksum")
                                    left = ImageOps.exif_transpose(original).convert("RGBA")
                                    right = ImageOps.exif_transpose(downloaded).convert("RGBA")
                                    if left.size != right.size:
                                        raise ValueError("Image dimensions differ")
                                    if all(b.getbbox() is None for b in ImageChops.difference(left, right).split()):
                                        totals["pixel_match"] += 1
                                        print(year, name, "fallback: identical decoded pixels; metadata may differ")
                                    elif a.accept_processed:
                                        totals["processed"] += 1
                                        print(year, name, "fallback: processed image accepted; pixels or metadata may differ")
                                    else:
                                        raise ValueError("Image pixels differ; retained")
                        elif f["mimeType"].startswith("video/"):
                            with tempfile.TemporaryDirectory() as work:
                                source_path = os.path.join(work, "source.mp4")
                                dest_path = os.path.join(work, "destination.mp4")
                                with open(source_path, "w+b") as src:
                                    downloader = MediaIoBaseDownload(src, drive.files().get_media(fileId=source_id))
                                    done = False
                                    while not done:
                                        _, done = downloader.next_chunk(num_retries=5)
                                    src.seek(0)
                                    if hashlib.md5(src.read()).hexdigest() != f["md5Checksum"]:
                                        raise ValueError("Source changed during verification")
                                dest.seek(0)
                                with open(dest_path, "wb") as out:
                                    shutil.copyfileobj(dest, out)
                                if a.accept_unverified_video:
                                    migration_video_check(source_path, dest_path)
                                    totals["migration_video"] += 1
                                    print(year, name, "migration policy: playable copy accepted; content identity not established")
                                elif video_stream_hash(source_path) == video_stream_hash(dest_path):
                                    totals["video_stream_match"] += 1
                                    print(year, name, "fallback: matching encoded video/audio streams")
                                elif decoded_video_hash(source_path) == decoded_video_hash(dest_path):
                                    totals["video_stream_match"] += 1
                                    print(year, name, "fallback: exact decoded frames and audio")
                                elif a.accept_processed_video:
                                    processed_video_match(source_path, dest_path)
                                    totals["processed_video"] += 1
                                    print(year, name, "fallback: processed video accepted")
                                else:
                                    raise ValueError("Decoded video/audio or timing differs; retained")
                        else:
                            raise ValueError("Video bytes differ; retained")
                    kind = next(k for k in before if totals[k] > before[k])
                    db.execute("INSERT OR REPLACE INTO verified_source VALUES (?,?,?,?,?)", (source_id, f["version"], remote, (int(a.accept_processed) | (2 if a.accept_processed_video else 0) | (4 if a.accept_unverified_video else 0)), kind))
                    db.commit()
                    candidates.append(f)
                totals["verified"] += 1
                print(year, name, "verified")
            except Exception as e:
                totals["unverified"] += 1
                print(year, name, "unverified:", type(e).__name__, str(e))
        for key, f in inventory.items():
            if key not in records:
                totals["unverified"] += 1
                print(key[0], f["name"], "unverified: no migration record")
        if a.cleanup and candidates:
            # Recheck complete inventory before making any changes.
            current = {f["id"]: f for f in walk(folders[0]["id"])}
            expected = {f["id"]: f for f in candidates}
            if any(i not in current or current[i].get("version") != expected[i].get("version") for i in expected):
                p.error("Source inventory changed; no cleanup performed")
            for f in candidates:
                latest = drive.files().get(fileId=f["id"], fields="version,md5Checksum,trashed").execute()
                if latest.get("trashed") or latest.get("version") != f["version"] or latest.get("md5Checksum") != f["md5Checksum"]:
                    p.error("Source changed; cleanup stopped")
                result = drive.files().update(fileId=f["id"], body={"trashed": True}, fields="id,trashed").execute()
                if not result.get("trashed"):
                    p.error("Trash operation not confirmed")
                db.execute("INSERT OR REPLACE INTO cleanup_history(source_id,year,remote) VALUES (?,?,?)", (f["id"],str(a.year),records[(str(a.year),f["id"])][1]))
                db.commit()
                totals["trashed"] += 1
                print(a.year, f["name"], "trashed")
            remaining = list(walk(folders[0]["id"]))
            print("Source media remaining:", len(remaining))
            def prune_empty(parent):
                for child in list(children(parent)):
                    if child["mimeType"] != FOLDER:
                        continue
                    prune_empty(child["id"])
                    if any(children(child["id"])):
                        continue
                    latest = drive.files().get(fileId=child["id"], fields="parents,trashed,capabilities(canTrash)").execute()
                    if latest.get("trashed") or parent not in latest.get("parents", []) or not latest.get("capabilities", {}).get("canTrash"):
                        continue
                    if any(children(child["id"])):
                        continue
                    result = drive.files().update(fileId=child["id"], body={"trashed": True}, fields="id,trashed").execute()
                    if not result.get("trashed"):
                        p.error("Empty folder trash operation not confirmed")
                    totals["folders_trashed"] += 1
                    print(a.year, child["name"], "empty folder trashed")
            prune_empty(folders[0]["id"])
            if not a.videos_only and not remaining and not totals["unverified"]:
                db.execute("INSERT OR IGNORE INTO completed_year VALUES (?)", (str(a.year),))
                db.commit()
        print("Verification summary:", totals)
        db.close()
        return 1 if totals["unverified"] or not totals["verified"] else 0
    count = 0
    print("Scanning source year folders", flush=True)
    for folder in sorted(children(root), key=lambda f:f["name"]):
        year = folder["name"]
        if folder["mimeType"] != FOLDER or not re.fullmatch(r"\d{4}", year) or (a.year and year != str(a.year)):
            continue
        if not (a.scan or a.dry_run) and db.execute("SELECT 1 FROM completed_year WHERE year=?", (year,)).fetchone() and not any(walk(folder["id"])):
            print(year, "already completed; source inventory empty")
            continue
        album = None
        saved = db.execute("SELECT id FROM albums WHERE year=?", (year,)).fetchone()
        existing = set()
        if saved and not (a.scan or a.dry_run):
            album = saved[0]
            page = None
            while True:
                body = {"albumId": album, "pageSize": 100}
                if page:
                    body["pageToken"] = page
                result = api("POST", "mediaItems:search", json=body)
                existing.update(item["id"] for item in result.get("mediaItems", []))
                page = result.get("nextPageToken")
                if not page:
                    break
            print(year, "existing album items:", len(existing), flush=True)
        batch = []
        batch_started = None
        def flush_batch():
            nonlocal batch_started
            if not batch:
                return
            # Persist uncertain submissions before creating remote items.
            for source_id, filename, upload_token in batch:
                db.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?)", (source_id,year,filename,"pending",None,None))
            db.commit()
            results = api("POST","mediaItems:batchCreate",json={"albumId":album,"newMediaItems":[{"simpleMediaItem":{"uploadToken":t,"fileName":n}} for _,n,t in batch]})["newMediaItemResults"]
            by_token = {x.get("uploadToken"):x for x in results}
            for source_id, filename, upload_token in batch:
                result = by_token.get(upload_token)
                if not result:
                    print(year, filename, "pending: missing batch result")
                    continue
                remote = result.get("mediaItem",{}).get("id")
                if not result.get("status",{}).get("code",0) and remote:
                    db.execute("UPDATE media SET status='done',remote=?,error=NULL WHERE id=?", (remote,source_id))
                    print(year, filename, "done (batch)")
                else:
                    db.execute("UPDATE media SET status='failed',error=? WHERE id=?", (str(result.get("status")),source_id))
                    print(year, filename, "batch failed")
            db.commit()
            batch.clear()
            batch_started = None
        print(year, "scanning source media", flush=True)
        for f in selected_walk(folder["id"]):
            id, name = f["id"], f["name"]
            row = db.execute("SELECT status,remote FROM media WHERE id=?", (id,)).fetchone()
            if a.scan or a.dry_run:
                print(year, name, row[0] if row else "new")
                continue
            recovering = bool(row and row[0] == "failed")
            if row and row[0] == "done":
                if not row[1]:
                    continue
                if row[1] in existing:
                    print(year, name, "existing upload confirmed by album", flush=True)
                    continue
                try:
                    print(year, name, "checking missing album item", flush=True)
                    api("GET", "mediaItems/" + row[1])
                    print(year, name, "destination exists outside expected album; retained for review", flush=True)
                    continue
                except HTTPError as e:
                    if e.response is None or e.response.status_code != 404:
                        raise
                    recovering = True
                    print(year, name, "recovering missing destination")
            if row and row[0] == "pending":
                continue
            if a.retry_failed and (not row or row[0] != "failed"):
                continue
            if a.limit and count >= a.limit:
                flush_batch()
                return
            if batch and (remaining_api_budget() <= 2 or time.monotonic() - batch_started >= 600):
                flush_batch()
            count += 1
            print(year, name, "starting upload", flush=True)
            try:
                if album is None:
                    saved = db.execute("SELECT id FROM albums WHERE year=?", (year,)).fetchone()
                    album = saved[0] if saved else api("POST", "albums", json={"album":{"title":c["photos"].get("album_prefix","")+year}})["id"]
                    db.execute("INSERT OR IGNORE INTO albums VALUES (?,?)", (year,album))
                    db.commit()
                with tempfile.TemporaryFile() as tmp:
                    downloader = MediaIoBaseDownload(tmp, drive.files().get_media(fileId=id))
                    done = False
                    while not done:
                        _, done = downloader.next_chunk(num_retries=5)
                    tmp.seek(0)
                    original = tmp.read()
                if f.get("md5Checksum") and hashlib.md5(original).hexdigest() != f["md5Checksum"]:
                    raise ValueError("Source changed during download")
                for attempt in range(3 if f["mimeType"] in ("image/jpeg", "video/mp4") else 1):
                    payload = original
                    if attempt and f["mimeType"] == "video/mp4":
                        # Append a valid ISO BMFF free box; no media packets are changed.
                        if len(original) < 8 or original[4:8] != b"ftyp":
                            raise ValueError("Unsupported MP4 container")
                        offset = 0
                        while offset < len(original):
                            if offset + 8 > len(original):
                                raise ValueError("Truncated MP4 box")
                            size = int.from_bytes(original[offset:offset+4], "big")
                            header = 8
                            if size == 1:
                                size = int.from_bytes(original[offset+8:offset+16], "big")
                                header = 16
                            if size < header or offset + size > len(original):
                                raise ValueError("Unsupported MP4 box size")
                            offset += size
                        marker = uuid.uuid4().bytes
                        payload = original + (8+len(marker)).to_bytes(4,"big") + b"free" + marker
                    elif attempt:
                        if not original.startswith(b"\xff\xd8"):
                            raise ValueError("Invalid JPEG")
                        comment = ("google-picture-sync recovery " + uuid.uuid4().hex).encode("ascii")
                        payload = original[:2] + b"\xff\xfe" + (len(comment)+2).to_bytes(2,"big") + comment + original[2:]
                    upload_token = upload_payload(payload, f["mimeType"])
                    if not recovering:
                        if not batch:
                            batch_started = time.monotonic()
                        batch.append((id,name,upload_token))
                        if len(batch) >= 50:
                            flush_batch()
                        break
                    db.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?)",(id,year,name,"pending",None,None))
                    db.commit()
                    result = api("POST","mediaItems:batchCreate",json={"albumId":album,"newMediaItems":[{"simpleMediaItem":{"uploadToken":upload_token,"fileName":name}}]})["newMediaItemResults"][0]
                    if result.get("status",{}).get("code",0):
                        db.execute("UPDATE media SET status='failed',error=? WHERE id=?", (str(result["status"].get("code")),id))
                        db.commit()
                        if result["status"].get("code") == 6 and f["mimeType"] in ("image/jpeg","video/mp4") and attempt < 2:
                            continue
                        break
                    remote = result["mediaItem"]["id"]
                    db.execute("UPDATE media SET status='done',remote=?,error=NULL WHERE id=?", (remote,id))
                    db.commit()
                    try:
                        api("GET", "mediaItems/" + remote)
                        break
                    except HTTPError as e:
                        if e.response is None or e.response.status_code != 404:
                            raise
                        print(year, name, "destination 404 after upload; attempt", attempt+1)
                        if attempt == 2 or f["mimeType"] not in ("image/jpeg", "video/mp4"):
                            break
                if recovering:
                    print(year,name,db.execute("SELECT status FROM media WHERE id=?",(id,)).fetchone()[0])
            except Exception as e:
                db.execute("INSERT OR IGNORE INTO media VALUES (?,?,?,?,?,?)",(id,year,name,"failed",None,type(e).__name__))
                db.commit()
                print(year,name,"error:",type(e).__name__, str(e))
        flush_batch()
    db.close()

if __name__ == "__main__":
    raise SystemExit(main())
