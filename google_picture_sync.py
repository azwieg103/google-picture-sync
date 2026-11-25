import argparse
import configparser
import os
import re
import sqlite3
import tempfile
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

def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description="Copy Drive photos to Google Photos without deleting originals")
    p.add_argument("--config", default=".env/config.ini")
    p.add_argument("--version", action="version", version="0.1.0")
    g = p.add_mutually_exclusive_group(required=True)
    for flag in ("init", "auth", "test", "scan", "dry-run", "all", "resume", "status", "retry-failed"):
        g.add_argument("--" + flag, action="store_true")
    g.add_argument("--year", type=int)
    p.add_argument("--limit", type=int, default=0, help="Maximum new uploads this run (0 unlimited)")
    a = p.parse_args()
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
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
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
        creds = flow.run_local_server(port=0)
    token.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    if a.auth:
        print("Authenticated")
        return
    drive = build("drive", "v3", credentials=creds)
    http = AuthorizedSession(creds)
    def api(method, endpoint, **kw):
        r = http.request(method, BASE + endpoint, timeout=120, **kw)
        r.raise_for_status()
        return r.json()
    def listing(q):
        page = None
        while True:
            r = drive.files().list(q=q+" and trashed=false", fields="nextPageToken,files(id,name,mimeType)", pageSize=1000, pageToken=page).execute(num_retries=5)
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
    count = 0
    for folder in sorted(children(root), key=lambda f:f["name"]):
        year = folder["name"]
        if folder["mimeType"] != FOLDER or not re.fullmatch(r"\d{4}", year) or (a.year and year != str(a.year)):
            continue
        album = None
        for f in walk(folder["id"]):
            id, name = f["id"], f["name"]
            row = db.execute("SELECT status FROM media WHERE id=?", (id,)).fetchone()
            if a.scan or a.dry_run:
                print(year, name, row[0] if row else "new")
                continue
            if row and row[0] in ("done", "pending"):
                continue
            if a.retry_failed and (not row or row[0] != "failed"):
                continue
            if a.limit and count >= a.limit:
                return
            count += 1
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
                    r = http.post(BASE+"uploads", data=tmp, headers={"Content-Type":"application/octet-stream","X-Goog-Upload-Protocol":"raw","X-Goog-Upload-Content-Type":f["mimeType"]}, timeout=300)
                    r.raise_for_status()
                    upload = r.text
                db.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?)",(id,year,name,"pending",None,None))
                db.commit()
                result = api("POST","mediaItems:batchCreate",json={"albumId":album,"newMediaItems":[{"simpleMediaItem":{"uploadToken":upload,"fileName":name}}]})["newMediaItemResults"][0]
                if result.get("status",{}).get("code",0):
                    db.execute("UPDATE media SET status='failed',error=? WHERE id=?", (str(result["status"].get("code")),id))
                else:
                    db.execute("UPDATE media SET status='done',remote=? WHERE id=?", (result["mediaItem"]["id"],id))
                db.commit()
                print(year,name,db.execute("SELECT status FROM media WHERE id=?",(id,)).fetchone()[0])
            except Exception as e:
                db.execute("INSERT OR IGNORE INTO media VALUES (?,?,?,?,?,?)",(id,year,name,"failed",None,type(e).__name__))
                db.commit()
                print(year,name,"error:",type(e).__name__)
    db.close()

if __name__ == "__main__":
    main()
