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
import math
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
OWNER_CODE = os.environ.get("OWNER_CODE", "epicfridge:06706")      # type this as the username to become owner
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


async def api_world(request):
    return web.json_response(world_json(), headers={"Cache-Control": "no-cache"})


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


# ================================================================ WORLD (shared with the client via /api/world)
WORLD_SIZE = 120
BOXES = []   # static solid boxes (players, cars and balls all collide with these)
DECOR = []   # visual-only extras


def box(cx, cz, w, d, top, thick=99, color=None, **kw):
    b = {"x": cx, "z": cz, "w": w, "d": d, "top": top, "thick": thick, "color": color}
    b.update(kw)
    BOXES.append(b)


def house(cx, cz, w, d, wall=0xffc8de, roof=0x8b5a3c):
    """Walk-in house, door on the +z side, furniture inside, stairs + trampoline to the roof."""
    t, H, door = 0.5, 4.2, 3.0
    box(cx, cz - d / 2 + t / 2, w, t, H, color=wall)                       # back wall
    box(cx - w / 2 + t / 2, cz, t, d - 2 * t, H, color=wall)               # left wall
    box(cx + w / 2 - t / 2, cz, t, d - 2 * t, H, color=wall)               # right wall
    seg = (w - door) / 2
    fz = cz + d / 2 - t / 2
    box(cx - w / 2 + seg / 2, fz, seg, t, H, color=wall)                   # front-left
    box(cx + w / 2 - seg / 2, fz, seg, t, H, color=wall)                   # front-right
    box(cx, fz, door, t, H, thick=H - 2.8, color=0xc9965d)                 # lintel over the door
    box(cx, cz + d / 2 + 1.0, door + 0.6, 1.6, 0.25, color=0xd9b58a)       # doorstep
    box(cx, cz, w + 1.4, d + 1.4, H + 0.5, thick=0.5, color=roof)          # roof
    box(cx + w / 4, cz - d / 4, 1.2, 1.2, H + 2.1, thick=1.6, color=0xb35c44)  # chimney
    DECOR.append({"t": "floor", "cx": cx, "cz": cz, "w": w - 2 * t, "d": d - 2 * t, "color": 0xc99a5b})
    # furniture
    box(cx - w / 4, cz - d / 2 + t + 0.9, 3.2, 1.4, 1.0, color=0x9ec5ff)                 # couch
    box(cx - w / 4, cz - d / 2 + t + 0.25, 3.2, 0.5, 1.7, color=0x7fa8e8)                 # couch back
    box(cx + w / 8, cz, 2.2, 1.4, 0.9, color=0xe0b07a)                                    # table
    box(cx + w / 2 - t - 1.6, cz - d / 2 + t + 1.9, 3.0, 3.6, 0.7, color=0xffd1e8)        # bed
    box(cx - w / 2 + t + 0.5, cz + d / 6, 0.9, 3.0, 1.9, color=0x8b5a3c)                  # shelf
    # outside stairs to the roof (east side)
    ex = cx + w / 2
    box(ex + 6.9, cz, 2.4, 3.0, 1.2, color=0xffe58a)
    box(ex + 4.5, cz, 2.4, 3.0, 2.4, color=0xffc29a)
    box(ex + 2.1, cz, 2.4, 3.0, 3.6, color=0xff9ec7)
    # trampoline on the west side that bounces you onto the roof
    box(cx - w / 2 - 4.0, cz, 4, 4, 0.5, thick=0.5, color=0x2b2d6e, kind="tramp", bounce=17.0)


def goal(line_x, direction, cz):
    """Soccer goal. direction=-1 opens toward +x (west goal), +1 opens toward -x (east goal)."""
    ox = line_x
    back = ox + direction * 3.0
    box(ox, cz - 4.0, 0.4, 0.4, 3.0, color=0xffffff)
    box(ox, cz + 4.0, 0.4, 0.4, 3.0, color=0xffffff)
    box(ox, cz, 0.4, 8.4, 3.0, thick=0.4, color=0xffffff)
    box(back, cz, 0.3, 8.4, 3.0, color=0xeeeeee, kind="net")
    box((ox + back) / 2, cz - 4.0, 3.0, 0.3, 3.0, color=0xeeeeee, kind="net")
    box((ox + back) / 2, cz + 4.0, 3.0, 0.3, 3.0, color=0xeeeeee, kind="net")
    box((ox + back) / 2, cz, 3.0, 8.4, 3.0, thick=0.2, color=0xeeeeee, kind="net")


PITCH = {"cx": 60.0, "cz": -20.0, "w": 50.0, "d": 30.0}
GOAL_W_X = PITCH["cx"] - PITCH["w"] / 2    # 35 (west goal line)
GOAL_E_X = PITCH["cx"] + PITCH["w"] / 2    # 85 (east goal line)
RING = {"cx": 0.0, "cz": 72.0, "r0": 28.0, "r1": 38.0}


def build_world():
    # --- original map (staircase / tower / bridge / pyramid / parkour / islands)
    for x, z, top in [(-6, 2, 1.1), (-10, -3, 2.2), (-10, -9, 3.3), (-4, -12, 4.4), (2, -12, 5.5)]:
        box(x, z, 4, 4, top)
    box(9, -12, 8, 8, 6.6, color=0xffd1e8)
    box(9, -3, 2.4, 10, 6.6, thick=0.6, color=0xc7a6ff)
    for z, top in [(6, 5.5), (11, 4.4), (16, 3.3), (21, 2.2)]:
        box(9, z, 4, 4, top)
    box(9, -12, 4.4, 4.4, 7.6, thick=1.0, hidden=True)            # cake collider
    DECOR.append({"t": "cake", "x": 9, "z": -12, "y": 6.6})
    box(-22, 14, 12, 12, 1.2, color=0xffe58a)
    box(-22, 14, 8, 8, 2.4, color=0xffc29a)
    box(-22, 14, 4, 4, 3.6, color=0xff9ec7)
    for z, top, col in [(-6, 1.4, 0x9ef0d0), (-10, 2.6, 0x8fd3ff), (-14, 3.8, 0xc7a6ff), (-18, 2.6, 0xff9ec7)]:
        box(-24, z, 2, 2, top, color=col)
    box(-24, -22, 6, 6, 3.8, color=0xffe58a)
    for x, top in [(16, 1.1), (21, 2.2), (26, 3.3)]:
        box(x, 8, 4, 4, top)
    box(26, 2, 4, 4, 3.3, thick=0.8, color=0x9ef0d0)
    box(26, -4, 3, 3, 3.3, thick=0.8, color=0xffe58a)
    box(31, -4, 3, 3, 4.4, thick=0.8, color=0xff9ec7)
    box(36, -4, 5, 5, 5.5, thick=0.8, color=0xc7a6ff)
    box(0, 26, 20, 2, 1.4, color=0x8fd3ff)
    box(-38, -38, 3, 3, 12, color=0xff9ec7)
    box(38, 38, 3, 3, 9, color=0x9ef0d0)

    # --- NEW: houses
    house(-60, 28, 16, 12)
    house(66, 44, 10, 8, wall=0xbfe9ff, roof=0xb2455e)

    # --- NEW: soccer pitch with two goals + scoreboard
    DECOR.append({"t": "pitch", **PITCH})
    goal(GOAL_W_X, -1, PITCH["cz"])
    goal(GOAL_E_X, +1, PITCH["cz"])
    box(PITCH["cx"] - 7, -38, 0.6, 0.6, 4.0, color=0x8b5a3c)
    box(PITCH["cx"] + 7, -38, 0.6, 0.6, 4.0, color=0x8b5a3c)
    DECOR.append({"t": "sign", "x": PITCH["cx"], "y": 5.2, "z": -38})

    # --- NEW: trampolines
    box(22, 14, 4, 4, 0.5, thick=0.5, color=0x2b2d6e, kind="tramp", bounce=15.0)
    box(-30, -32, 4, 4, 0.5, thick=0.5, color=0x2b2d6e, kind="tramp", bounce=16.0)
    box(40, 30, 5, 5, 0.5, thick=0.5, color=0x2b2d6e, kind="tramp", bounce=22.0)   # sky trampoline
    box(47, 30, 6, 6, 8.5, thick=1.0, color=0x9ef0d0)                              # sky island

    # --- NEW: big stepped ziggurat
    for i, (size, col) in enumerate(zip(range(28, 4, -4), [0xffe58a, 0xffc29a, 0xff9ec7, 0xc7a6ff, 0x8fd3ff, 0x9ef0d0])):
        box(-85, -60, size, size, 1.2 * (i + 1), color=col)

    # --- NEW: corner landmarks
    for sx in (-1, 1):
        for sz in (-1, 1):
            box(sx * 105, sz * 105, 4, 4, 14, color=[0xff9ec7, 0x9ef0d0, 0xffe58a, 0xc7a6ff][(sx + 1 + (sz + 1) // 2)])

    # --- NEW: race track
    DECOR.append({"t": "road", **RING})


build_world()

BALL_DEFS = [
    {"id": "soccer", "kind": "soccer", "r": 0.55, "m": 4.0, "e": 0.72, "drag": 0.55, "x": PITCH["cx"], "z": PITCH["cz"]},
    {"id": "beach1", "kind": "beach", "r": 1.0, "m": 1.2, "e": 0.82, "drag": 0.35, "x": 3.0, "z": 14.0},
    {"id": "beach2", "kind": "beach", "r": 1.0, "m": 1.2, "e": 0.82, "drag": 0.35, "x": -6.0, "z": 14.0},
    {"id": "bowl", "kind": "bowling", "r": 0.5, "m": 30.0, "e": 0.25, "drag": 0.28, "x": 0.0, "z": 19.0},
    {"id": "bounce1", "kind": "bouncy", "r": 0.45, "m": 3.0, "e": 0.94, "drag": 0.5, "x": 4.0, "z": 22.0},
    {"id": "bounce2", "kind": "bouncy", "r": 0.45, "m": 3.0, "e": 0.94, "drag": 0.5, "x": -4.0, "z": 22.0},
]
CAR_DEFS = [
    {"id": "car1", "color": 0xff7ab0, "x": 14.0, "z": 14.0, "h": -0.8},
    {"id": "car2", "color": 0x5fe0b5, "x": -14.0, "z": 14.0, "h": 0.8},
    {"id": "car3", "color": 0xffd84a, "x": RING["cx"] + 31.0, "z": RING["cz"], "h": 0.0},
    {"id": "car4", "color": 0x5fb8ff, "x": RING["cx"] + 35.0, "z": RING["cz"], "h": 0.0},
]


def world_json():
    return {
        "size": WORLD_SIZE, "boxes": BOXES, "decor": DECOR,
        "balls": [{k: d[k] for k in ("id", "kind", "r", "x", "z")} for d in BALL_DEFS],
        "cars": CAR_DEFS,
    }


# ================================================================ PHYSICS (balls) - server authoritative
G = 22.0
AABBS = [(b["x"] - b["w"] / 2, b["x"] + b["w"] / 2, b["top"] - min(b["thick"], b["top"]), b["top"],
          b["z"] - b["d"] / 2, b["z"] + b["d"] / 2) for b in BOXES]
BALLS = []
CARS = []
SCORE = [0, 0]   # [pink, mint]


def reset_ball(b):
    b.update(x=b["hx"], z=b["hz"], y=b["r"], vx=0.0, vy=0.0, vz=0.0, asleep=True, idle=0.0, respawn_at=0.0, scored=False, dirty=True)


def init_state():
    BALLS.clear()
    CARS.clear()
    for d in BALL_DEFS:
        b = dict(d)
        b["hx"], b["hz"] = d["x"], d["z"]
        reset_ball(b)
        BALLS.append(b)
    for d in CAR_DEFS:
        CARS.append({"id": d["id"], "x": d["x"], "y": 0.0, "z": d["z"], "h": d["h"], "v": 0.0, "driver": None, "t": 0.0})


init_state()


def car_public(c):
    return {"id": c["id"], "x": round(c["x"], 2), "y": round(c["y"], 2), "z": round(c["z"], 2), "h": round(c["h"], 3), "v": round(c["v"], 2), "driver": c["driver"]}


def closest_on_seg(px, py, pz, ax, ay, az, bx, by, bz):
    abx, aby, abz = bx - ax, by - ay, bz - az
    l2 = abx * abx + aby * aby + abz * abz
    t = 0.0 if l2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby + (pz - az) * abz) / l2))
    return ax + abx * t, ay + aby * t, az + abz * t


def pushers(now):
    """Things that can shove a ball: players (capsules) and cars (capsules along their length)."""
    out = []
    for p in players.values():
        if p.get("car"):
            continue
        stale = now - p["pt"] > 0.3
        vx = 0.0 if stale else p["vx"]
        vz = 0.0 if stale else p["vz"]
        out.append((p["x"], p["y"] + 0.4, p["z"], p["x"], p["y"] + 1.4, p["z"], 0.4, vx, 0.0, vz, 70.0))
    for c in CARS:
        fx, fz = math.sin(c["h"]), math.cos(c["h"])
        v = c["v"] if (c["driver"] and now - c["t"] < 0.4) else 0.0
        out.append((c["x"] + fx, c["y"] + 0.6, c["z"] + fz, c["x"] - fx, c["y"] + 0.6, c["z"] - fz, 1.0, fx * v, 0.0, fz * v, 900.0))
    return out


def touching(b, push):
    for (ax, ay, az, bx, by, bz, rad, *_rest) in push:
        qx, qy, qz = closest_on_seg(b["x"], b["y"], b["z"], ax, ay, az, bx, by, bz)
        lim = b["r"] + rad + 0.03
        if (b["x"] - qx) ** 2 + (b["y"] - qy) ** 2 + (b["z"] - qz) ** 2 < lim * lim:
            return True
    return False


def step_ball(b, h, push):
    r = b["r"]
    b["vy"] -= G * h
    sp = math.sqrt(b["vx"] ** 2 + b["vy"] ** 2 + b["vz"] ** 2)
    if sp > 26:
        k = 26 / sp
        b["vx"] *= k; b["vy"] *= k; b["vz"] *= k
    b["x"] += b["vx"] * h
    b["y"] += b["vy"] * h
    b["z"] += b["vz"] * h
    supported = False
    if b["y"] < r:
        b["y"] = r
        if b["vy"] < 0:
            b["vy"] = -b["vy"] * b["e"]
            if abs(b["vy"]) < 1.5:
                b["vy"] = 0.0
        supported = True
    # static boxes
    for (x0, x1, y0, y1, z0, z1) in AABBS:
        if b["x"] < x0 - r or b["x"] > x1 + r or b["y"] < y0 - r or b["y"] > y1 + r or b["z"] < z0 - r or b["z"] > z1 + r:
            continue
        cx = min(max(b["x"], x0), x1); cy = min(max(b["y"], y0), y1); cz = min(max(b["z"], z0), z1)
        dx, dy, dz = b["x"] - cx, b["y"] - cy, b["z"] - cz
        d2 = dx * dx + dy * dy + dz * dz
        if d2 >= r * r:
            continue
        if d2 > 1e-12:
            d = math.sqrt(d2)
            nx, ny, nz = dx / d, dy / d, dz / d
            pen = r - d
        else:   # centre is inside the box: leave through the nearest face
            opts = [(b["x"] - x0, -1, 0, 0), (x1 - b["x"], 1, 0, 0), (b["y"] - y0, 0, -1, 0),
                    (y1 - b["y"], 0, 1, 0), (b["z"] - z0, 0, 0, -1), (z1 - b["z"], 0, 0, 1)]
            m = min(opts)
            pen = m[0] + r
            nx, ny, nz = m[1], m[2], m[3]
        b["x"] += nx * pen; b["y"] += ny * pen; b["z"] += nz * pen
        vn = b["vx"] * nx + b["vy"] * ny + b["vz"] * nz
        if vn < 0:
            e = 0.0 if -vn < 1.5 else b["e"]
            b["vx"] -= (1 + e) * vn * nx; b["vy"] -= (1 + e) * vn * ny; b["vz"] -= (1 + e) * vn * nz
        if ny > 0.6:
            supported = True
    # players / cars
    for (ax, ay, az, bx, by, bz, rad, pvx, pvy, pvz, pm) in push:
        qx, qy, qz = closest_on_seg(b["x"], b["y"], b["z"], ax, ay, az, bx, by, bz)
        dx, dy, dz = b["x"] - qx, b["y"] - qy, b["z"] - qz
        lim = r + rad
        d2 = dx * dx + dy * dy + dz * dz
        if d2 >= lim * lim:
            continue
        d = math.sqrt(d2)
        if d < 1e-6:
            nx, ny, nz, d = 0.0, 1.0, 0.0, 0.0
        else:
            nx, ny, nz = dx / d, dy / d, dz / d
        pen = lim - d
        b["x"] += nx * pen; b["y"] += ny * pen; b["z"] += nz * pen
        vn = (b["vx"] - pvx) * nx + (b["vy"] - pvy) * ny + (b["vz"] - pvz) * nz
        if vn < 0:
            k = (1 + 0.6) * pm / (pm + b["m"])
            b["vx"] -= vn * nx * k; b["vy"] -= vn * ny * k; b["vz"] -= vn * nz * k
            if pvx * pvx + pvz * pvz > 9 and -vn > 2.0 and ny < 0.5:
                b["vy"] += min(3.0, -vn * 0.12)     # a little loft on a proper kick
    # drag + world edge
    f = math.exp(-(b["drag"] if supported else 0.04) * h)
    b["vx"] *= f; b["vz"] *= f
    if supported and b["vx"] ** 2 + b["vz"] ** 2 < 0.09:
        b["vx"] *= 0.9; b["vz"] *= 0.9
    lim = WORLD_SIZE - 1.0
    if abs(b["x"]) > lim:
        b["x"] = math.copysign(lim, b["x"]); b["vx"] *= -0.5
    if abs(b["z"]) > lim:
        b["z"] = math.copysign(lim, b["z"]); b["vz"] *= -0.5
    b["sup"] = supported


def pair_collide():
    n = len(BALLS)
    for i in range(n):
        a = BALLS[i]
        for j in range(i + 1, n):
            c = BALLS[j]
            dx, dy, dz = c["x"] - a["x"], c["y"] - a["y"], c["z"] - a["z"]
            lim = a["r"] + c["r"]
            d2 = dx * dx + dy * dy + dz * dz
            if d2 >= lim * lim or d2 < 1e-12:
                continue
            if a["asleep"] and c["asleep"]:
                continue
            d = math.sqrt(d2)
            nx, ny, nz = dx / d, dy / d, dz / d
            ia, ic = 1.0 / a["m"], 1.0 / c["m"]
            pen = lim - d
            a["x"] -= nx * pen * ia / (ia + ic); a["y"] -= ny * pen * ia / (ia + ic); a["z"] -= nz * pen * ia / (ia + ic)
            c["x"] += nx * pen * ic / (ia + ic); c["y"] += ny * pen * ic / (ia + ic); c["z"] += nz * pen * ic / (ia + ic)
            vn = (c["vx"] - a["vx"]) * nx + (c["vy"] - a["vy"]) * ny + (c["vz"] - a["vz"]) * nz
            if vn < 0:
                j_ = -(1 + 0.8) * vn / (ia + ic)
                a["vx"] -= j_ * ia * nx; a["vy"] -= j_ * ia * ny; a["vz"] -= j_ * ia * nz
                c["vx"] += j_ * ic * nx; c["vy"] += j_ * ic * ny; c["vz"] += j_ * ic * nz
            a["asleep"] = False; c["asleep"] = False


def goal_check(b):
    """Returns 'mint' if the ball is inside the west goal, 'pink' if inside the east goal."""
    cz = PITCH["cz"]
    if abs(b["z"] - cz) < 3.8 and b["y"] < 2.9:
        if GOAL_W_X - 3.0 < b["x"] < GOAL_W_X - 0.3:
            return "mint"
        if GOAL_E_X + 0.3 < b["x"] < GOAL_E_X + 3.0:
            return "pink"
    return None


def phys_tick(dt, now):
    moved, events = [], []
    push = pushers(now)
    for b in BALLS:
        if b["respawn_at"] and now >= b["respawn_at"]:
            reset_ball(b)
        if b["asleep"] and touching(b, push):
            b["asleep"] = False
    sub = 3
    h = dt / sub
    for _ in range(sub):
        for b in BALLS:
            if not b["asleep"]:
                step_ball(b, h, push)
        pair_collide()
    for b in BALLS:
        if b["asleep"]:
            b["idle"] += dt
            if b["idle"] > 60 and math.hypot(b["x"] - b["hx"], b["z"] - b["hz"]) > 2.0:
                reset_ball(b)       # nobody touched it for a minute: send it home
            if b["dirty"]:
                moved.append(b); b["dirty"] = False
            continue
        b["idle"] = 0.0
        if b["kind"] == "soccer" and not b["scored"]:
            team = goal_check(b)
            if team:
                b["scored"] = True
                b["respawn_at"] = now + 3.0
                SCORE[0 if team == "pink" else 1] += 1
                events.append({"t": "goal", "team": team, "score": list(SCORE)})
        sp2 = b["vx"] ** 2 + b["vy"] ** 2 + b["vz"] ** 2
        if b.get("sup") and sp2 < 0.04 and b["y"] <= b["r"] + 0.6:
            b["vx"] = b["vy"] = b["vz"] = 0.0
            b["asleep"] = True
        moved.append(b)
        b["dirty"] = False
    return [[b["id"], round(b["x"], 2), round(b["y"], 2), round(b["z"], 2)] for b in moved], events


async def physics_loop():
    last = time.time()
    while True:
        await asyncio.sleep(1 / 30)
        now = time.time()
        dt = min(now - last, 0.1)
        last = now
        if not players:
            continue
        try:
            moved, events = phys_tick(dt, now)
            if moved:
                await broadcast({"t": "phys", "b": moved})
            for ev in events:
                print(f"[goal] {ev['team']} scores {ev['score']}")
                await broadcast(ev)
        except Exception as e:  # noqa
            print("[physics] error:", e)


async def free_car(p):
    """Release the car a player is driving (exit, disconnect, kick)."""
    cid = p.get("car")
    if not cid:
        return
    p["car"] = None
    for c in CARS:
        if c["id"] == cid and c["driver"] == p["id"]:
            c["driver"] = None
            c["v"] = 0.0
            await broadcast({"t": "car", **car_public(c)})


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
    """Real visitor IP. Behind cloudflared the tunnel connects from localhost and
    puts the visitor's address in CF-Connecting-IP, so only trust that header then."""
    remote = request.remote or "?"
    ip = remote
    if remote in ("127.0.0.1", "::1"):
        h = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if h:
            ip = h
    try:
        a = ipaddress.ip_address(ip)
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
        if a.version == 6:   # IPv6 users can rotate inside their /64, so group by it
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
                raw = clean_text(data.get("name"), 40)
                is_owner = bool(OWNER_CODE) and hmac.compare_digest(raw.encode(), OWNER_CODE.encode())
                char = data.get("char")
                if char not in char_names():
                    await ws.send_str(json.dumps({"t": "error", "m": "Unknown character."}))
                    break
                if is_owner:
                    name = OWNER_NAME
                else:
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
                    "chat_times": [], "car": None, "pt": time.time(), "vx": 0.0, "vz": 0.0,
                }
                others = [public(p) for p in players.values()]
                players[pid] = me
                await ws.send_str(json.dumps({
                    "t": "welcome", "id": pid, "name": name,
                    "spawn": {"x": me["x"], "y": me["y"], "z": me["z"]}, "owner": is_owner,
                    "players": others,
                    "balls": [[b["id"], round(b["x"], 2), round(b["y"], 2), round(b["z"], 2)] for b in BALLS],
                    "cars": [car_public(c) for c in CARS], "score": list(SCORE),
                }))
                await broadcast({"t": "join", "p": public(me)}, exclude=pid)
                print(f"[+] {name} joined as {char} from {ip} ({len(players)} online)")
                await push_admin()
                continue

            if t == "s":
                nx = num(data.get("x"), -130, 130, me["x"])
                ny = num(data.get("y"), -50, 200, me["y"])
                nz = num(data.get("z"), -130, 130, me["z"])
                now = time.time()
                dt = now - me["pt"]
                if 0.02 < dt < 0.5:
                    me["vx"] = max(-16.0, min(16.0, 0.4 * me["vx"] + 0.6 * (nx - me["x"]) / dt))
                    me["vz"] = max(-16.0, min(16.0, 0.4 * me["vz"] + 0.6 * (nz - me["z"]) / dt))
                else:
                    me["vx"] = me["vz"] = 0.0
                me["pt"] = now
                me["x"], me["y"], me["z"] = nx, ny, nz
                if me["car"]:
                    continue
                await broadcast({"t": "s", "id": me["id"], "x": me["x"], "y": me["y"], "z": me["z"]}, exclude=me["id"])
            elif t == "emote":
                n = int(num(data.get("n"), 1, 7, 0))
                if n:
                    await broadcast({"t": "emote", "id": me["id"], "n": n}, exclude=me["id"])
            elif t == "car_enter":
                car = next((c for c in CARS if c["id"] == data.get("id")), None)
                if (car and car["driver"] is None and not me["car"] and abs(car["x"] - me["x"]) < 7
                        and abs(car["z"] - me["z"]) < 7 and abs(car["y"] - me["y"]) < 4):
                    car["driver"] = me["id"]; car["v"] = 0.0; car["t"] = time.time(); me["car"] = car["id"]
                    await broadcast({"t": "car", **car_public(car)})
            elif t == "car_exit":
                await free_car(me)
            elif t == "c":
                car = next((c for c in CARS if c["id"] == me["car"]), None)
                if car and car["driver"] == me["id"]:
                    car["x"] = num(data.get("x"), -130, 130, car["x"]); car["z"] = num(data.get("z"), -130, 130, car["z"])
                    car["y"] = num(data.get("y"), -5, 60, car["y"]); car["h"] = num(data.get("h"), -1000, 1000, car["h"])
                    car["v"] = num(data.get("v"), -40, 40, 0.0); car["t"] = time.time()
                    me["x"], me["y"], me["z"] = car["x"], car["y"], car["z"]
                    await broadcast({"t": "c", **car_public(car)}, exclude=me["id"])
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
        if me and me.get("car"):
            await free_car(me)
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
    app["physics"] = asyncio.ensure_future(physics_loop())
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
        web.get("/api/world", api_world),
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
