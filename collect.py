#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
杭州主城区实时路况采集（高德交通态势 · 矩形/圆形/指定线路）
用于 GitHub Actions 定时运行，仅用标准库，无需 pip 安装。

数据落盘：
  data/tiles_YYYY-MM-DD.csv   区域级：每次查询的整体评价（拥堵百分比/等级）
  data/roads_YYYY-MM-DD.csv   路段级：每段路的 lcodes/方向/拥堵等级/速度
  data/_state.json            各切片上次采集时间（用于漂移容忍的间隔门控）
  data/_usage_YYYY-MM-DD_*.txt 各接口当日调用计数
  logs/errors.log             失败记录
"""

import argparse
import collections
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------- 配置 ----------------
KEY = os.environ.get("AMAP_KEY", "")
URL_RECT = "https://restapi.amap.com/v3/traffic/status/rectangle"
URL_CIRCLE = "https://restapi.amap.com/v3/traffic/status/circle"
URL_ROAD = "https://restapi.amap.com/v3/traffic/status/road"
LEVEL = 4          # 1高速 2快速路/国道 3高速辅路 4主要道路 5一般 6无名（包含关系）
TZ = timezone(timedelta(hours=8))

# 配额约束：高级服务按接口分别计费，各 100 次/日
#   矩形区域 100/日 -> 2 片 × 48 轮 = 96
#   圆形区域 100/日 -> 1 片 × 48 轮 = 48
#   指定线路 100/日 -> 1 条 × 96 轮 = 96（15 分钟粒度）
# 三个接口互不占用，合计 240 次/日
TILES = [
    {"id": "C01_城西", "type": "rect", "rect": "120.10,30.24;120.15,30.28", "every": 30},
    {"id": "C02_中心", "type": "rect", "rect": "120.15,30.24;120.20,30.28", "every": 30},
    {"id": "C03_滨江", "type": "circle", "location": "120.19,30.21", "radius": 4500, "every": 30},
    {"id": "R01_天目山路", "type": "road", "name": "天目山路", "adcode": "330100", "every": 15},
]
INTERVAL_MIN = 15

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
LOG_DIR = os.path.join(BASE, "logs")

STATUS_MAP = {"0": "未知", "1": "畅通", "2": "缓行", "3": "拥堵"}
EXHAUSTED = set()   # 本轮已超限的接口，避免对同一接口重复空转


def build_tiles():
    return TILES


def fetch(tile, timeout=15, retries=2):
    """按切片类型请求矩形或圆形交通态势，失败重试。返回 trafficinfo 字典或 None。"""
    if tile.get("type") == "circle":
        url_base = URL_CIRCLE
        params = {
            "key": KEY,
            "level": LEVEL,
            "location": tile["location"],
            "radius": str(tile["radius"]),
            "extensions": "all",
            "output": "json",
        }
        tag = tile["id"] + " circle"
    elif tile.get("type") == "road":
        url_base = URL_ROAD
        params = {
            "key": KEY,
            "name": tile["name"],
            "adcode": tile["adcode"],
            "level": LEVEL,
            "extensions": "all",
            "output": "json",
        }
        tag = tile["id"] + " road"
    else:
        url_base = URL_RECT
        params = {
            "key": KEY,
            "level": LEVEL,
            "rectangle": tile["rect"],
            "extensions": "all",
            "output": "json",
        }
        tag = tile["id"] + " rect"
    url = url_base + "?" + urllib.parse.urlencode(params, safe=",;")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hz-traffic-research/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
            if payload.get("status") == "1":
                return payload.get("trafficinfo") or {}
            code = str(payload.get("infocode") or "")
            log_error(f"{tag} api_fail info={payload.get('info')} code={code}")
            if code == "10044" or "OVER_LIMIT" in str(payload.get("info") or ""):
                EXHAUSTED.add(tile.get("type", "rect"))
                return None        # 该接口今日已超限，不再重试
        except Exception as e:
            log_error(f"{tag} attempt{attempt} exc={e!r}")
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    return None


def log_error(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "errors.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S}\t{msg}\n")


def append_csv(path, header, rows):
    if not rows:
        return
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(header)
        w.writerows(rows)


def read_state():
    path = os.path.join(DATA_DIR, "_state.json")
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def write_state(state):
    with open(os.path.join(DATA_DIR, "_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def run_once():
    if not KEY:
        print("错误：未设置 AMAP_KEY 环境变量", file=sys.stderr)
        sys.exit(2)

    now = datetime.now(TZ)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    day = now.strftime("%Y-%m-%d")

    tiles = build_tiles()

    # 日配额护栏：高级服务按接口分别 100 次/日，每个接口单独记账，接近 98 次即跳过
    def read_used(kind):
        path = os.path.join(DATA_DIR, f'_usage_{day}_{kind}.txt')
        if os.path.exists(path):
            try:
                return int(open(path).read().strip() or 0)
            except ValueError:
                return 0
        return 0

    def write_used(kind, value):
        with open(os.path.join(DATA_DIR, f'_usage_{day}_{kind}.txt'), 'w') as f:
            f.write(str(value))

    tile_rows, road_rows = [], []
    seen = set()   # 跨切片去重：lcodes
    stats = collections.defaultdict(int)   # 本轮各接口实际调用次数

    # 间隔门控改用「距上次采集的分钟数」判断，而不是判断当前分钟是否为整刻。
    # GitHub Actions 的定时触发会有数分钟到十几分钟漂移，按整刻判断会整轮跳过。
    state = read_state()
    for t in tiles:
        kind = t.get("type", "rect")
        every = t.get("every", INTERVAL_MIN)
        last = state.get(t["id"])
        if last:
            try:
                gap = (now - datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)).total_seconds() / 60
            except ValueError:
                gap = 999
            if gap < every * 0.6:
                print(f"[{ts}] skip {t['id']}: 距上次仅 {gap:.0f} 分钟（阈值 {every*0.6:.0f}）", flush=True)
                continue
        if kind in EXHAUSTED:
            print(f"[{ts}] skip {t['id']}: {kind} 接口今日已超限（10044）", flush=True)
            continue
        used = read_used(kind) + stats[kind]
        if used + 1 > 98:
            print(f"[{ts}] skip {t['id']}: {kind} 接口今日已达 {used} 次", flush=True)
            continue

        info = fetch(t)
        stats[kind] += 1
        time.sleep(0.4)  # 控速
        if info is None:
            continue

        ev = info.get("evaluation") or {}
        roads = info.get("roads") or []
        where = (t.get("rect")
                 or (f"{t.get('location')} r={t.get('radius')}" if t.get("type") == "circle" else "")
                 or t.get("name", ""))
        state[t["id"]] = ts
        tile_rows.append([
            ts, t["id"], where,
            ev.get("status", ""), ev.get("description", ""),
            ev.get("expedite", ""), ev.get("congested", ""),
            ev.get("blocked", ""), ev.get("unknown", ""),
            len(roads),
        ])

        for rd in roads:
            lcodes = rd.get("lcodes") or ""
            direction = rd.get("direction") or ""
            key = lcodes
            if not lcodes or key in seen:
                continue
            seen.add(key)
            road_rows.append([
                ts, t["id"], lcodes, direction, rd.get("name", ""),
                rd.get("status", ""), STATUS_MAP.get(rd.get("status", ""), ""),
                rd.get("speed", ""), rd.get("angle", ""),
            ])

    append_csv(
        os.path.join(DATA_DIR, f"tiles_{day}.csv"),
        ["ts", "tile_id", "rect", "eval_status", "eval_desc",
         "expedite_pct", "congested_pct", "blocked_pct", "unknown_pct", "n_roads"],
        tile_rows,
    )
    append_csv(
        os.path.join(DATA_DIR, f"roads_{day}.csv"),
        ["ts", "tile_id", "lcodes", "direction", "name",
         "status_code", "status_text", "speed_kmh", "angle"],
        road_rows,
    )

    # 按接口分别累计当日调用数
    for kind, n in stats.items():
        if n:
            write_used(kind, read_used(kind) + n)
    write_state(state)

    ok = len(tile_rows)
    print(f"[{ts}] tiles_ok={ok}/{len(tiles)} roads={len(road_rows)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="跑一轮")
    ap.add_argument("--loop", action="store_true", help="常驻循环（每5分钟）")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    if args.loop:
        while True:
            try:
                run_once()
            except SystemExit:
                pass
            except Exception as e:
                log_error(f"loop exc={e!r}")
            time.sleep(max(1, INTERVAL_MIN * 60 - int(time.time()) % (INTERVAL_MIN * 60)))
    else:
        run_once()


if __name__ == "__main__":
    main()
