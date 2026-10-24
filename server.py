"""
Cookiewars Multiplayer server
- Serves index.html and assets
- Handles multiplayer WebSockets at /ws with WebRTC voice signaling & physical ball sync
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
ASSET_BASE = os.environ.get("ASSET_BASE", "").strip().rstrip("/")
MAX_PLAYERS = 40
MAX_PER_IP = int(os.environ.get("MAX_PER_IP", "2"))
OWNER_CODE = os.environ.get("OWNER_CODE", "epicfridge:06706").strip().strip('"').strip("'")
OWNER_NAME = "epicfridge (OWNER)"
ALLOWED_EXT = {".json", ".atlas", ".png"}

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
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                if len(data) > 10000 and VENDOR_CHECK[fname] in data:
                    with open(path, "wb") as f:
                        f.write(data)
                    ok = True
                    break
            except Exception:
                pass


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


async def index(request):
    return web.FileResponse(os.path.join(BASE, "index.html"), headers={"Cache-Control": "no-cache"})


async def api_characters(request):
    return web.json_response({"base": ASSET_BASE, "characters": scan_characters()}, headers={"Cache-Control": "no-cache"})


_text_cache = {}


async def asset(request):
    folder = request.match_info["folder"]
    fname = request.match_info["file"]
    ext = os.path.splitext(fname)[1].lower()
    if ext not in ALLOWED_EXT:
        raise web.HTTPNotFound()
    root = os.path.realpath(ASSET_DIR)
    path = os.path.realpath(os.path.join(ASSET_DIR, folder, fname))
    if os.path.dirname(os.path.dirname(path)) != root or not os.path.isfile(path):
        raise web.HTTPNotFound()
    cache = "public, max-age=86400"
    if ext == ".png":
        return web.FileResponse(path, headers={"Cache-Control": cache})
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


# ---------------------------------------------------------------- state & physics
players = {}
next_id = 1
_seq = 0

ball_state = {"x": 0.0, "y": 2.0, "z": 0.0, "vx": 0.0, "vy": 0.0, "vz": 0.0, "last_by": None}


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
    if v != v:
        return default
    return max(lo, min(hi, v))


def public(p):
    return {"id": p["id"], "name": p["name"], "char": p["char"], "x": p["x"], "y": p["y"], "z": p["z"], "owner": p["owner"]}


def client_ip(request):
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
        if a.version == 6:
            return str(ipaddress.ip_network(f"{a}/64", strict=False).network_address)
        return str(a)
    except ValueError:
        return ip


def admin_snapshot():
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
    global next_id, ball_state
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=16384)
    await ws.prepare(request)
    me = None
    ip = client_ip(request)
    try:
        while True:
            try:
                msg = await ws.receive(timeout=90)
            except asyncio.TimeoutError:
                break
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
                raw_name = str(data.get("name") or "").strip()
                clean_owner_code = str(OWNER_CODE or "").strip().strip('"').strip("'")
                is_owner = bool(clean_owner_code) and (
                    hmac.compare_digest(raw_name.encode("utf-8"), clean_owner_code.encode("utf-8")) or 
                    raw_name == clean_owner_code
                )

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
                        await ws.send_str(json.dumps({"t": "error", "m": f"Too many players from network (max {MAX_PER_IP})."}))
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
                    "players": others, "ball": ball_state
                }))
                await broadcast({"t": "join", "p": public(me)}, exclude=pid)
                await push_admin()
                continue

            # --- Movement ---
            if t == "s":
                me["x"] = num(data.get("x"), -80, 80, me["x"])
                me["y"] = num(data.get("y"), -50, 200, me["y"])
                me["z"] = num(data.get("z"), -80, 80, me["z"])
                await broadcast({"t": "s", "id": me["id"], "x": me["x"], "y": me["y"], "z": me["z"]}, exclude=me["id"])

            # --- Ball Physics Sync ---
            elif t == "ball":
                ball_state = {
                    "x": num(data.get("x"), -100, 100),
                    "y": num(data.get("y"), -10, 100),
                    "z": num(data.get("z"), -100, 100),
                    "vx": num(data.get("vx"), -50, 50),
                    "vy": num(data.get("vy"), -50, 50),
                    "vz": num(data.get("vz"), -50, 50),
                    "last_by": me["id"]
                }
                await broadcast({"t": "ball", **ball_state}, exclude=me["id"])

            # --- WebRTC Voice Signaling ---
            elif t in ("rtc_offer", "rtc_answer", "rtc_candidate"):
                target = players.get(str(data.get("to")))
                if target:
                    await send(target, {"t": t, "from": me["id"], "data": data.get("data")})

            # --- Owner Controls ---
            elif t == "mute_all" and me["owner"]:
                await broadcast({"t": "server_mute", "state": bool(data.get("state"))})

            elif t == "emote":
                n = int(num(data.get("n"), 1, 7, 0))
                if n:
                    await broadcast({"t": "emote", "id": me["id"], "n": n}, exclude=me["id"])

            elif t == "admin_list" and me["owner"]:
                await send(me, admin_snapshot())

            elif t == "kick" and me["owner"]:
                target = players.get(str(data.get("id", "")))
                if target and not target["owner"]:
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
                    continue
                me["chat_times"].append(now)
                await broadcast({"t": "chat", "id": me["id"], "name": me["name"], "m": text})
    except Exception as e:
        print("[ws] error:", e)
    finally:
        if me and players.get(me["id"]) is me:
            del players[me["id"]]
            await broadcast({"t": "leave", "id": me["id"], "name": me["name"]})
            await push_admin()
        if not ws.closed:
            await ws.close()
    return ws


async def on_startup(app):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, ensure_vendor)
    chars = scan_characters()
    print(f"[assets] {len(chars)} character(s) found in {ASSET_DIR}")


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