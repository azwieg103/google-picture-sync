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
from requests.exceptions import HTTPError
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
    hashes = subprocess.run(["ffmpeg","-v","error","-xerror","-i",path,"-map","0","-c","copy","-f","streamhash","-hash","sha256","-"], check=True, capture_output=True, text=True, timeout=600)
    if len(hashes.stdout.strip().splitlines()) != len(streams):
        raise ValueError("Incomplete stream hashes")
    return signature, hashes.stdout.strip()

def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description="Copy Drive photos to Google Photos without deleting originals")
    p.add_argument("--config", default=".env/config.ini")
    p.add_argument("--version", action="version", version="0.1.0")
    g = p.add_mutually_exclusive_group()
    for flag in ("init", "auth", "test", "scan", "dry-run", "all", "resume", "status", "retry-failed", "verify", "cleanup"):
        g.add_argument("--" + flag, action="store_true")
    p.add_argument("--year", type=int, help="Restrict to a year; alone, upload that year")
    p.add_argument("--limit", type=int, default=0, help="Maximum new uploads this run (0 unlimited)")
    p.add_argument("--accept-processed", action="store_true", help="Allow processed images with matching dimensions and recorded upload ID even if pixels differ")
    a = p.parse_args()
    if not any((a.init, a.auth, a.test, a.scan, a.dry_run, a.all, a.resume, a.status, a.retry_failed, a.verify, a.cleanup, a.year)):
        p.error("Specify an action or --year")
    if a.year is not None and not 1000 <= a.year <= 9999:
        p.error("--year must be a four-digit year")
    if a.year and any((a.init, a.auth, a.test, a.status)):
        p.error("--year cannot be combined with --init, --auth, --test or --status")
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
    if a.status:
        for row in db.execute("SELECT year,status,count(*) FROM media GROUP BY year,status ORDER BY year"):
            print(*row)
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
    def reserve_quota(url):
        kind = "api" if urlsplit(url).hostname == "photoslibrary.googleapis.com" else "media"
        limit = c.getint("photos", "daily_api_budget" if kind == "api" else "daily_media_budget", fallback=9000 if kind == "api" else 67500)
        if limit <= 0:
            raise ValueError("Daily quota budget must be positive")
        while True:
            now = datetime.now(quota_zone)
            day = now.date().isoformat()
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT count FROM quota_usage WHERE day=? AND kind=?", (day,kind)).fetchone()
            used = row[0] if row else 0
            if used < limit:
                db.execute("INSERT INTO quota_usage VALUES (?,?,1) ON CONFLICT(day,kind) DO UPDATE SET count=count+1", (day,kind))
                db.commit()
                return
            db.rollback()
            reset = datetime.combine(now.date()+timedelta(days=1), datetime.min.time(), tzinfo=quota_zone)+timedelta(minutes=5)
            wait = max(1, reset.timestamp()-now.timestamp())
            print(f"Daily Photos {kind} budget reached ({used}/{limit}); waiting until {reset.isoformat()}", flush=True)
            time.sleep(wait)
    last_request = 0.0
    def photos_request(method, url, **kw):
        nonlocal last_request
        delay = 60
        while True:
            time.sleep(max(0, 2.0 - (time.monotonic() - last_request)))
            last_request = time.monotonic()
            reserve_quota(url)
            r = http.request(method, url, **kw)
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
    if a.verify or a.cleanup:
        totals = {"verified": 0, "unverified": 0, "trashed": 0, "exact": 0, "pixel_match": 0, "processed": 0, "folders_trashed": 0, "video_stream_match": 0}
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
            for f in walk(folder["id"]):
                inventory[(folder["name"], f["id"])] = f
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
                    digest = hashlib.md5()
                    suffix = "=dv" if f["mimeType"].startswith("video/") else "=d"
                    with tempfile.TemporaryFile() as dest:
                        with photos_request("GET", item["baseUrl"] + suffix, stream=True, timeout=300) as response:
                            response.raise_for_status()
                            for chunk in response.iter_content(1024 * 1024):
                                digest.update(chunk)
                                dest.write(chunk)
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
                        elif f["mimeType"] == "video/mp4":
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
                                if video_stream_hash(source_path) != video_stream_hash(dest_path):
                                    raise ValueError("Video stream hashes or timing differ; retained")
                                totals["video_stream_match"] += 1
                                print(year, name, "fallback: matching encoded video/audio streams")
                        else:
                            raise ValueError("Video bytes differ; retained")
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
        if a.cleanup and not totals["unverified"] and totals["verified"]:
            # Recheck complete inventory before making any changes.
            current = {f["id"]: f for f in walk(folders[0]["id"])}
            expected = {f["id"]: f for f in candidates}
            if current.keys() != expected.keys() or any(current[i].get("version") != expected[i].get("version") for i in current):
                p.error("Source inventory changed; no cleanup performed")
            for f in candidates:
                latest = drive.files().get(fileId=f["id"], fields="version,md5Checksum,trashed").execute()
                if latest.get("trashed") or latest.get("version") != f["version"] or latest.get("md5Checksum") != f["md5Checksum"]:
                    p.error("Source changed; cleanup stopped")
                result = drive.files().update(fileId=f["id"], body={"trashed": True}, fields="id,trashed").execute()
                if not result.get("trashed"):
                    p.error("Trash operation not confirmed")
                totals["trashed"] += 1
                print(a.year, f["name"], "trashed")
            if list(walk(folders[0]["id"])):
                p.error("Eligible source files remain; inspect before retrying")
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
        print("Verification summary:", totals)
        db.close()
        return 1 if totals["unverified"] or not totals["verified"] else 0
    count = 0
    print("Scanning source year folders", flush=True)
    for folder in sorted(children(root), key=lambda f:f["name"]):
        year = folder["name"]
        if folder["mimeType"] != FOLDER or not re.fullmatch(r"\d{4}", year) or (a.year and year != str(a.year)):
            continue
        album = None
        print(year, "scanning source media", flush=True)
        for f in walk(folder["id"]):
            id, name = f["id"], f["name"]
            row = db.execute("SELECT status,remote FROM media WHERE id=?", (id,)).fetchone()
            if a.scan or a.dry_run:
                print(year, name, row[0] if row else "new")
                continue
            recovering = False
            if row and row[0] == "done":
                if not row[1]:
                    continue
                try:
                    print(year, name, "checking existing upload", flush=True)
                    api("GET", "mediaItems/" + row[1])
                    print(year, name, "existing upload confirmed", flush=True)
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
                return
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
                    r = photos_request("POST", BASE+"uploads", data=payload, headers={"Content-Type":"application/octet-stream","X-Goog-Upload-Protocol":"raw","X-Goog-Upload-Content-Type":f["mimeType"]}, timeout=300)
                    for retry in range(3):
                        if r.status_code not in (429, 500, 502, 503, 504):
                            break
                        time.sleep(2 ** retry)
                        r = photos_request("POST", BASE+"uploads", data=payload, headers={"Content-Type":"application/octet-stream","X-Goog-Upload-Protocol":"raw","X-Goog-Upload-Content-Type":f["mimeType"]}, timeout=300)
                    r.raise_for_status()
                    db.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?)",(id,year,name,"pending",None,None))
                    db.commit()
                    result = api("POST","mediaItems:batchCreate",json={"albumId":album,"newMediaItems":[{"simpleMediaItem":{"uploadToken":r.text,"fileName":name}}]})["newMediaItemResults"][0]
                    if result.get("status",{}).get("code",0):
                        db.execute("UPDATE media SET status='failed',error=? WHERE id=?", (str(result["status"].get("code")),id))
                        db.commit()
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
                print(year,name,db.execute("SELECT status FROM media WHERE id=?",(id,)).fetchone()[0])
            except Exception as e:
                db.execute("INSERT OR IGNORE INTO media VALUES (?,?,?,?,?,?)",(id,year,name,"failed",None,type(e).__name__))
                db.commit()
                print(year,name,"error:",type(e).__name__, str(e))
    db.close()

if __name__ == "__main__":
    raise SystemExit(main())
