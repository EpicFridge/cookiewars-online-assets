"""
Cookiewars Multiplayer server
- Serves index.html
- Serves ONLY the .json / .atlas / .png files that sit directly inside each
  character folder in ./Cookiewars (the "texture" sub-folders are never served)
- Handles the multiplayer WebSocket at /ws
"""
import asyncio
import gzip
import hmac
import ipaddress
import json
import os
import random
import re
import sys
import time
import urllib.request

from aiohttp import web, WSMsgType

BASE = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(BASE, "Cookiewars")
VENDOR_DIR = os.path.join(BASE, "vendor")
PORT = int(os.environ.get("PORT", "8080"))
# Optional: where the character files are hosted (e.g. https://user.github.io/repo). Empty = serve from this PC.
ASSET_BASE = os.environ.get("ASSET_BASE", "").strip().rstrip("/")
MAX_PLAYERS = 40
MAX_PER_IP = int(os.environ.get("MAX_PER_IP", "2"))                 # connections allowed per network
# Strip quotes that might accidentally be imported from .env or config files
OWNER_CODE = os.environ.get("OWNER_CODE", "epicfridge:06706").strip().strip('"').strip("'")
OWNER_NAME = "epicfridge (OWNER)"
ALLOWED_EXT = {".json", ".atlas", ".png"}

# Spine 3.7 web runtime + three.js are downloaded once into ./vendor
VENDOR = {
    "three.min.js": [
        "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js",
        "https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js",
    ],
    "spine-webgl.js": [
        "https://raw.githubusercontent.com/EsotericSoftware/spine-runtimes/3.7/spine-ts/build/spine-webgl.js",
        "https://cdn.jsdelivr.net/gh/EsotericSoftware/spine-runtimes@3.7/spine-ts/build/spine-webgl.js",
    ],
}
VENDOR_CHECK = {"three.min.js": b"THREE", "spine-webgl.js": b"spine"}


def ensure_vendor():
    os.makedirs(VENDOR_DIR, exist_ok=True)
    for fname, urls in VENDOR.items():
        path = os.path.join(VENDOR_DIR, fname)
        if os.path.isfile(path) and os.path.getsize(path) > 10000:
            continue
        ok = False
        for url in urls:
            try:
                print(f"[vendor] downloading {fname} from {url}")
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                if len(data) > 10000 and VENDOR_CHECK[fname] in data:
                    with open(path, "wb") as f:
                        f.write(data)
                    ok = True
                    print(f"[vendor] saved {fname} ({len(data)//1024} KB)")
                    break
            except Exception as e:  # noqa
                print(f"[vendor] failed: {e}")
        if not ok:
            print(f"[vendor] WARNING: could not download {fname}. The browser will try a CDN instead.")


# ---------------------------------------------------------------- characters
def scan_characters():
    out = []
    if not os.path.isdir(ASSET_DIR):
        return out
    for name in sorted(os.listdir(ASSET_DIR), key=str.lower):
        d = os.path.join(ASSET_DIR, name)
        if not os.path.isdir(d):
            continue
        files = [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))]

        def pick(ext):
            for f in files:
                if f.lower() == (name + ext).lower():
                    return f
            for f in sorted(files):
                if f.lower().endswith(ext):
                    return f
            return None

        j, a, p = pick(".json"), pick(".atlas"), pick(".png")
        if j and a and p:
            out.append({"name": name, "json": j, "atlas": a, "png": p})
    return out


def char_names():
    return {c["name"] for c in scan_characters()}


# ---------------------------------------------------------------- http
async def index(request):
    return web.FileResponse(os.path.join(BASE, "index.html"), headers={"Cache-Control": "no-cache"})


async def api_characters(request):
    return web.json_response({"base": ASSET_BASE, "characters": scan_characters()}, headers={"Cache-Control": "no-cache"})


_text_cache = {}   # path -> (mtime, raw, gz)


async def asset(request):
    folder = request.match_info["folder"]
    fname = request.match_info["file"]
    ext = os.path.splitext(fname)[1].lower()
    if ext not in ALLOWED_EXT:
        raise web.HTTPNotFound()
    root = os.path.realpath(ASSET_DIR)
    path = os.path.realpath(os.path.join(ASSET_DIR, folder, fname))
    # must be exactly <Cookiewars>/<folder>/<file>  (no sub-folders, no traversal)
    if os.path.dirname(os.path.dirname(path)) != root or not os.path.isfile(path):
        raise web.HTTPNotFound()
    cache = "public, max-age=86400"
    if ext == ".png":
        return web.FileResponse(path, headers={"Cache-Control": cache})
    # json / atlas: kept in memory and sent gzip-compressed
    mtime = os.path.getmtime(path)
    ent = _text_cache.get(path)
    if not ent or ent[0] != mtime:
        with open(path, "rb") as f:
            raw = f.read()
        ent = (mtime, raw, gzip.compress(raw, 6))
        _text_cache[path] = ent
    use_gz = "gzip" in request.headers.get("Accept-Encoding", "")
    etag = '"%d-%d%s"' % (int(mtime), len(ent[1]), "-gz" if use_gz else "")
    headers = {"Cache-Control": cache, "ETag": etag, "Vary": "Accept-Encoding"}
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    if use_gz:
        headers["Content-Encoding"] = "gzip"
    return web.Response(body=ent[2] if use_gz else ent[1],
                        content_type="application/json" if ext == ".json" else "text/plain",
                        headers=headers)


async def vendor(request):
    fname = request.match_info["file"]
    if fname not in VENDOR:
        raise web.HTTPNotFound()
    path = os.path.join(VENDOR_DIR, fname)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- websocket
players = {}  # id -> dict
next_id = 1
_seq = 0


def pid_seq():
    global _seq
    _seq += 1
    return _seq
CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(s, limit):
    if not isinstance(s, str):
        return ""
    s = CTRL.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def num(v, lo, hi, default=0.0):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def public(p):
    return {"id": p["id"], "name": p["name"], "char": p["char"], "x": p["x"], "y": p["y"], "z": p["z"], "owner": p["owner"]}


def client_ip(request):
    """Real visitor IP. Behind Cloudflare or Fly.io proxies, retrieve the visitor's
    address from reverse-proxy HTTP headers."""
    # Check headers injected by Fly.io or Cloudflare
    ip = (
        request.headers.get("Fly-Client-IP")
        or request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote
        or "?"
    )

    try:
        a = ipaddress.ip_address(ip)
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
        if a.version == 6:   # IPv6 users can rotate inside their /64, group by subnet
            return str(ipaddress.ip_network(f"{a}/64", strict=False).network_address)
        return str(a)
    except ValueError:
        return ip


def admin_snapshot():
    """First (non-owner) player from an IP is 'normal', later ones from the same IP are alts of that first one."""
    first = {}
    normal, alts = [], []
    for p in sorted(players.values(), key=lambda q: q["seq"]):
        if p["owner"]:
            normal.append({"id": p["id"], "name": p["name"]})
        elif p["ip"] not in first:
            first[p["ip"]] = p
            normal.append({"id": p["id"], "name": p["name"]})
        else:
            alts.append({"id": p["id"], "name": p["name"], "of": first[p["ip"]]["name"]})
    return {"t": "admin", "total": len(players), "max": MAX_PLAYERS, "normal": normal, "alts": alts}


async def push_admin():
    owners = [p for p in players.values() if p["owner"]]
    if owners:
        data = json.dumps(admin_snapshot())
        await asyncio.gather(*(send(p, data) for p in owners), return_exceptions=True)


async def send(p, msg):
    try:
        await p["ws"].send_str(msg if isinstance(msg, str) else json.dumps(msg))
    except Exception:
        pass


async def broadcast(msg, exclude=None):
    data = json.dumps(msg)
    targets = [p for pid, p in players.items() if pid != exclude]
    if targets:
        await asyncio.gather(*(send(p, data) for p in targets), return_exceptions=True)


async def ws_handler(request):
    global next_id
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
    await ws.prepare(request)
    me = None
    ip = client_ip(request)
    try:
        while True:
            try:
                msg = await ws.receive(timeout=90)
            except asyncio.TimeoutError:
                break  # nothing at all for 90s -> dead connection
            if msg.type != WSMsgType.TEXT:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            t = data.get("t")

            if t == "ping":
                await ws.send_str(json.dumps({"t": "pong", "c": data.get("c")}))
                continue

            if me is None:
                if t != "join":
                    continue
                
                # Fetch raw name input from WebSocket
                raw_name = str(data.get("name") or "").strip()
                
                # Retrieve loaded OWNER_CODE cleaned of quotes/spaces
                clean_owner_code = str(OWNER_CODE or "").strip().strip('"').strip("'")
                
                # Debug logging to terminal
                print(f"[DEBUG] Received name: '{raw_name}' (bytes: {raw_name.encode('utf-8')})")
                print(f"[DEBUG] Target OWNER_CODE: '{clean_owner_code}' (bytes: {clean_owner_code.encode('utf-8')})")
                
                # Direct string check or HMAC check
                is_owner = bool(clean_owner_code) and (
                    hmac.compare_digest(raw_name.encode("utf-8"), clean_owner_code.encode("utf-8")) or 
                    raw_name == clean_owner_code
                )
                
                print(f"[DEBUG] is_owner result: {is_owner}")

                char = data.get("char")
                if char not in char_names():
                    await ws.send_str(json.dumps({"t": "error", "m": "Unknown character."}))
                    break

                if is_owner:
                    name = OWNER_NAME
                else:
                    raw = clean_text(data.get("name"), 40)
                    name = raw[:16].strip() or f"Cookie{random.randint(1000, 9999)}"
                    norm = re.sub(r"[^a-z0-9]", "", name.lower())
                    if norm.startswith("epicfridge") or re.search(r"\(\s*owner\s*\)", raw, re.I):
                        await ws.send_str(json.dumps({"t": "error", "m": "That username is reserved."}))
                        break
                    if len(players) >= MAX_PLAYERS:
                        await ws.send_str(json.dumps({"t": "error", "m": "Server is full."}))
                        break
                    if sum(1 for p in players.values() if p["ip"] == ip) >= MAX_PER_IP:
                        await ws.send_str(json.dumps({"t": "error", "m": f"Too many players from your network (max {MAX_PER_IP})."}))
                        print(f"[limit] blocked extra connection from {ip}")
                        break
                pid = str(next_id)
                next_id += 1
                me = {
                    "id": pid, "name": name, "char": char, "ws": ws, "ip": ip, "owner": is_owner, "seq": pid_seq(),
                    "x": random.uniform(-3, 3), "y": 0.0, "z": random.uniform(6, 10),
                    "chat_times": [],
                }
                others = [public(p) for p in players.values()]
                players[pid] = me
                await ws.send_str(json.dumps({
                    "t": "welcome", "id": pid, "name": name,
                    "spawn": {"x": me["x"], "y": me["y"], "z": me["z"]}, "owner": is_owner,
                    "players": others,
                }))
                await broadcast({"t": "join", "p": public(me)}, exclude=pid)
                print(f"[+] {name} joined as {char} from {ip} ({len(players)} online)")
                await push_admin()
                continue

            if t == "s":
                me["x"] = num(data.get("x"), -80, 80, me["x"])
                me["y"] = num(data.get("y"), -50, 200, me["y"])
                me["z"] = num(data.get("z"), -80, 80, me["z"])
                await broadcast({"t": "s", "id": me["id"], "x": me["x"], "y": me["y"], "z": me["z"]}, exclude=me["id"])
            elif t == "emote":
                n = int(num(data.get("n"), 1, 7, 0))
                if n:
                    await broadcast({"t": "emote", "id": me["id"], "n": n}, exclude=me["id"])
            elif t == "admin_list" and me["owner"]:
                await send(me, admin_snapshot())
            elif t == "kick" and me["owner"]:
                target = players.get(str(data.get("id", "")))
                if target and not target["owner"]:
                    print(f"[kick] {target['name']} was kicked by {me['name']}")
                    await send(target, {"t": "kicked"})
                    try:
                        await target["ws"].close(code=4001, message=b"kicked")
                    except Exception:
                        pass
            elif t == "chat":
                text = clean_text(data.get("m"), 200)
                if not text:
                    continue
                now = time.time()
                me["chat_times"] = [x for x in me["chat_times"] if now - x < 6]
                if len(me["chat_times"]) >= 5:
                    continue  # rate limited
                me["chat_times"].append(now)
                print(f"[chat] {me['name']}: {text}")
                await broadcast({"t": "chat", "id": me["id"], "name": me["name"], "m": text})
    except Exception as e:  # noqa
        print("[ws] error:", e)
    finally:
        if me and players.get(me["id"]) is me:
            del players[me["id"]]
            await broadcast({"t": "leave", "id": me["id"], "name": me["name"]})
            print(f"[-] {me['name']} left ({len(players)} online)")
            await push_admin()
        if not ws.closed:
            await ws.close()
    return ws


# ---------------------------------------------------------------- main
async def on_startup(app):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, ensure_vendor)
    chars = scan_characters()
    print(f"[assets] {len(chars)} character(s) found in {ASSET_DIR}")
    print(f"[assets] character files are loaded from: {ASSET_BASE or 'this PC (local)'}")
    if not chars:
        print("[assets] WARNING: no characters found. Each folder needs a .json, .atlas and .png directly inside it.")


def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.add_routes([
        web.get("/", index),
        web.get("/index.html", index),
        web.get("/api/characters", api_characters),
        web.get("/Cookiewars/{folder}/{file}", asset),
        web.get("/vendor/{file}", vendor),
        web.get("/ws", ws_handler),
    ])
    print(f"Cookiewars server running on http://localhost:{PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
