#!/usr/bin/env python3
"""sdwx — San Diego terminal weather report."""

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
from concurrent import futures
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

# Home point: Golden Hill. Open-Meteo is a gridded model rather than a
# station, so this is the grid cell over the neighborhood; the nearest real
# observation station (KSAN) sits across the bay and reads less like it.
LAT, LON = 32.7175, -117.13
TZNAME = "America/Los_Angeles"

# La Jolla (Scripps Wharf). Open-coast tides, which is what both surf spots
# below actually see. The San Diego Bay station is closer to home but runs a
# different phase and amplitude.
COOPS = "9410230"

# Nearshore buoys chosen for proximity to each spot. Torrey Pines Inner and
# Imperial Beach are closer to OB on paper but publish no realtime2 feed.
SPOTS = [
    {"key": "ob", "label": "OB",
     "lat": 32.7500, "lon": -117.2530, "buoy": "46254"},
    {"key": "solana", "label": "Solana",
     "lat": 32.9912, "lon": -117.2712, "buoy": "46266"},
]

WIDTH = 80
CACHE_DIR = Path.home() / ".cache" / "sdwx"
CACHE_TTL = 600

# Per-source freshness. Tide tables are predictions good for days; alerts are
# the one thing worth refetching aggressively.
TTL = {
    "forecast": 600, "air": 900, "marine": 1800, "tides": 21600,
    "buoy": 1800, "kp": 300, "scales": 1800, "alerts": 180,
}

# How far past its TTL a cached response may still be served when the network
# is down. Past this it is too old to pass off as current, and old enough to
# delete outright.
STALE_MAX = 86400

# Per-request socket timeout. Short because a timeout now falls back to
# cached data rather than losing the section outright.
HTTP_TIMEOUT = 5

UA = {"User-Agent": "sdwx/1.0 (+https://github.com/v0idravl/sdwx)"}
NO_CACHE = False
COLOR = True
# URLs served from expired cache this run. Set from worker threads, read once
# they have joined.
STALE = set()
# Nerd Font glyphs live in the Private Use Area, which less treats as
# non-printable unless told otherwise. Covers the BMP and both supplementary
# planes, since Nerd Fonts v3 uses all three.
PUA_PRINTABLE = "E000-F8FF:p,F0000-FFFFD:p,100000-10FFFD:p"

# ---------------------------------------------------------------- style/tiers

ANSI = {
    "reset": "\x1b[0m",
    "dim": "\x1b[2m",
    "heading": "\x1b[1;36m",
    "warn": "\x1b[1;31m",
    "accent": "\x1b[36m",
    "ok": "\x1b[32m",
    "elevated": "\x1b[33m",
    "high": "\x1b[38;5;208m",
    "severe": "\x1b[1;31m",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def style(text, name):
    if not COLOR or not name:
        return text
    return f"{ANSI[name]}{text}{ANSI['reset']}"


def visible_len(s):
    return len(ANSI_RE.sub("", s))


# value < threshold -> tier name; past all thresholds -> "severe"
TIERS = {
    "uv": [(3, "ok"), (6, "elevated"), (8, "high")],
    "aqi": [(51, "ok"), (101, "elevated"), (151, "high")],
    "kp": [(4, "ok"), (5, "elevated"), (7, "high")],
    "gust": [(20, "ok"), (34, "elevated"), (48, "high")],
    "precip": [(30, "ok"), (70, "elevated"), (float("inf"), "high")],
    "scale": [(1, "ok"), (3, "elevated"), (4, "high")],
}


def tier(metric, value):
    for threshold, name in TIERS[metric]:
        if value < threshold:
            return name
    return "severe"


def tiered(metric, value, text):
    t = tier(metric, value)
    if not COLOR:
        return f"[!]{text}" if t == "severe" else text
    return style(text, t)


# --------------------------------------------------------------- conversions

DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg):
    return DIRS[int(deg / 22.5 + 0.5) % 16]


def c_to_f(c):
    return c * 9 / 5 + 32


def m_to_ft(m):
    return m * 3.28084


# ---------------------------------------------------------------------- moon

SYNODIC = 29.530588853
REF_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
MOON_GLYPHS = "🌑🌒🌓🌔🌕🌖🌗🌘"
MOON_NAMES = ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
              "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"]


def moon_phase(dt):
    age = ((dt - REF_NEW_MOON).total_seconds() / 86400.0) % SYNODIC
    frac = age / SYNODIC
    illum = (1 - math.cos(2 * math.pi * frac)) / 2 * 100
    idx = int(frac * 8 + 0.5) % 8
    return {"age": age, "illum": illum, "waxing": age < SYNODIC / 2,
            "name": MOON_NAMES[idx], "glyph": MOON_GLYPHS[idx]}


# ------------------------------------------------------------------ WMO map
# Nerdfont weather glyphs (nf-weather); grouped here for easy tweaking.

GLYPH = {
    "sun": "", "part": "", "cloud": "", "fog": "",
    "drizzle": "", "rain": "", "frzrain": "",
    "snow": "", "showers": "", "storm": "",
}

WMO = {
    0: ("sun", "Clear"), 1: ("sun", "Mostly clear"),
    2: ("part", "Partly cloudy"), 3: ("cloud", "Overcast"),
    45: ("fog", "Fog"), 48: ("fog", "Rime fog"),
    51: ("drizzle", "Light drizzle"), 53: ("drizzle", "Drizzle"),
    55: ("drizzle", "Heavy drizzle"),
    56: ("frzrain", "Frz drizzle"), 57: ("frzrain", "Frz drizzle"),
    61: ("rain", "Light rain"), 63: ("rain", "Rain"), 65: ("rain", "Heavy rain"),
    66: ("frzrain", "Freezing rain"), 67: ("frzrain", "Freezing rain"),
    71: ("snow", "Light snow"), 73: ("snow", "Snow"), 75: ("snow", "Heavy snow"),
    77: ("snow", "Snow grains"),
    80: ("showers", "Light showers"), 81: ("showers", "Showers"),
    82: ("showers", "Heavy showers"),
    85: ("snow", "Snow showers"), 86: ("snow", "Snow showers"),
    95: ("storm", "Thunderstorm"), 96: ("storm", "T-storm w/ hail"),
    99: ("storm", "T-storm w/ hail"),
}


def wmo(code):
    key, text = WMO.get(code, ("cloud", f"Code {code}"))
    return GLYPH[key], text


# --------------------------------------------------------------------- fetch


def _http_get(url, headers):
    req = Request(url, headers=headers or UA)
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def cache_path(url):
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".cache")


def prune_cache():
    """Delete cache entries too old to ever be served again.

    The tide request embeds the date it was made for, so it mints a fresh
    key every day and yesterday's would otherwise sit in the cache
    forever. Anything past STALE_MAX is already refused as a fallback, so
    removing it costs nothing. Returns how many went.
    """
    cutoff = time.time() - STALE_MAX
    removed = 0
    try:
        entries = list(CACHE_DIR.glob("*.cache"))
    except OSError:
        return 0
    for p in entries:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def read_cache(path, max_age):
    """Cached body if it exists and is younger than max_age, else None."""
    try:
        if path.exists() and time.time() - path.stat().st_mtime < max_age:
            return path.read_text()
    except OSError:
        pass
    return None


def fetch(url, ttl=CACHE_TTL, headers=None, _get=None):
    p = cache_path(url)
    if not NO_CACHE:
        fresh = read_cache(p, ttl)
        if fresh is not None:
            return fresh
    try:
        body = (_get or _http_get)(url, headers)
    except Exception:
        # Network is unhappy. A stale response beats dropping the section
        # entirely, as long as it is not old enough to be misleading.
        if not NO_CACHE:
            stale = read_cache(p, STALE_MAX)
            if stale is not None:
                STALE.add(url)
                return stale
        raise
    if not NO_CACHE:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        except OSError:
            pass
    return body


def fetch_json(url, **kw):
    return json.loads(fetch(url, **kw))


# ------------------------------------------------------------------- sources

OM_FORECAST = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
    "dew_point_2m,weather_code,wind_speed_10m,wind_direction_10m,"
    "wind_gusts_10m,uv_index,pressure_msl"
    "&hourly=temperature_2m,precipitation_probability,weather_code"
    "&daily=temperature_2m_max,temperature_2m_min,"
    "precipitation_probability_max,weather_code,wind_speed_10m_max,"
    "wind_gusts_10m_max,uv_index_max,sunrise,sunset"
    "&forecast_days=7&temperature_unit=fahrenheit&wind_speed_unit=mph"
    f"&precipitation_unit=inch&timezone={TZNAME}"
)

OM_AIR = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    f"?latitude={LAT}&longitude={LON}&current=us_aqi&timezone={TZNAME}"
)

def om_marine_url(lat, lon):
    return ("https://marine-api.open-meteo.com/v1/marine"
            f"?latitude={lat}&longitude={lon}"
            "&current=sea_surface_temperature,wave_height,wave_period,"
            "wave_direction,swell_wave_height,swell_wave_period,"
            "swell_wave_direction,wind_wave_height"
            f"&timezone={TZNAME}")


def src_forecast(_get=None):
    j = fetch_json(OM_FORECAST, ttl=TTL["forecast"], _get=_get)
    c, d, h = j["current"], j["daily"], j["hourly"]
    return {
        "current": {"temp": c["temperature_2m"], "feels": c["apparent_temperature"],
                    "rh": c["relative_humidity_2m"], "dew": c["dew_point_2m"],
                    "code": c["weather_code"], "wind": c["wind_speed_10m"],
                    "wind_dir": c["wind_direction_10m"], "gust": c["wind_gusts_10m"],
                    "uv": c["uv_index"], "pressure": c["pressure_msl"]},
        "daily": {"date": d["time"], "hi": d["temperature_2m_max"],
                  "lo": d["temperature_2m_min"],
                  "pp": d["precipitation_probability_max"],
                  "code": d["weather_code"], "wind": d["wind_speed_10m_max"],
                  "gust": d["wind_gusts_10m_max"], "uv": d["uv_index_max"],
                  "sunrise": d["sunrise"], "sunset": d["sunset"]},
        "hourly": {"time": h["time"], "temp": h["temperature_2m"],
                   "pp": h["precipitation_probability"],
                   "code": h["weather_code"]},
    }


def src_air(_get=None):
    j = fetch_json(OM_AIR, ttl=TTL["air"], _get=_get)
    return {"aqi": int(j["current"]["us_aqi"])}


def src_marine(lat, lon, _get=None):
    c = fetch_json(om_marine_url(lat, lon),
                   ttl=TTL["marine"], _get=_get)["current"]
    sst = c.get("sea_surface_temperature")
    return {
        "sst": c_to_f(sst) if sst is not None else None,
        "wave_ft": m_to_ft(c["wave_height"]), "wave_s": c["wave_period"],
        "swell_ft": m_to_ft(c["swell_wave_height"]),
        "swell_s": c["swell_wave_period"],
        "swell_dir": compass(c["swell_wave_direction"]),
    }


COOPS_BASE = ("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
              "?station=" + COOPS + "&units=english&time_zone=lst_ldt&format=json")
SWPC_KP = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
SWPC_SCALES = "https://services.swpc.noaa.gov/products/noaa-scales.json"
NWS_ALERTS = f"https://api.weather.gov/alerts/active?point={LAT}%2C{LON}"


def ndbc_url(buoy):
    return f"https://www.ndbc.noaa.gov/data/realtime2/{buoy}.txt"


def src_tides(now, _get=None):
    url = (COOPS_BASE + "&product=predictions&datum=MLLW&interval=hilo"
           "&begin_date=" + now.strftime("%Y%m%d") + "&range=48")
    preds = fetch_json(url, ttl=TTL["tides"], _get=_get)["predictions"]
    cutoff = now.strftime("%Y-%m-%d %H:%M")
    future = [p for p in preds if p["t"] > cutoff]
    return [{"t": p["t"], "type": p["type"], "ft": float(p["v"])}
            for p in future[:4]]


def _ndbc_val(v):
    return None if v == "MM" else float(v)


def src_ndbc(buoy, _get=None):
    lines = fetch(ndbc_url(buoy), ttl=TTL["buoy"], _get=_get).splitlines()
    names = lines[0].lstrip("#").split()
    row = dict(zip(names, lines[2].split()))
    wvht = _ndbc_val(row["WVHT"])
    wtmp = _ndbc_val(row["WTMP"])
    mwd = _ndbc_val(row["MWD"])
    return {
        "wave_ft": m_to_ft(wvht) if wvht is not None else None,
        "dpd_s": _ndbc_val(row["DPD"]),
        "dir": compass(mwd) if mwd is not None else None,
        "water_f": c_to_f(wtmp) if wtmp is not None else None,
    }


def src_kp(_get=None):
    entries = fetch_json(SWPC_KP, ttl=TTL["kp"], _get=_get)
    return {"kp": float(entries[-1]["estimated_kp"])}


def src_scales(_get=None):
    j = fetch_json(SWPC_SCALES, ttl=TTL["scales"], _get=_get)
    cur, tom = j["0"], j["1"]

    def pair(block):
        return (block["Scale"] or "0", block["Text"] or "none")

    return {
        "now": {k: pair(cur[k]) for k in ("R", "S", "G")},
        "outlook": {"G": pair(tom["G"]),
                    "R_prob": (tom["R"].get("MinorProb"), tom["R"].get("MajorProb")),
                    "S_prob": tom["S"].get("Prob")},
    }


def src_alerts(_get=None):
    j = fetch_json(NWS_ALERTS, ttl=TTL["alerts"], _get=_get)
    out = []
    for f in j.get("features", []):
        p = f["properties"]
        out.append({"event": p.get("event", "Alert"),
                    "headline": p.get("headline", ""),
                    "expires": p.get("expires", ""),
                    "description": p.get("description") or "",
                    "instruction": p.get("instruction") or ""})
    return out


# -------------------------------------------------------------------- render


def heading(title):
    bar = "─" * (WIDTH - len(title) - 4)
    return style(f"── {title} {bar}", "heading")


def unavailable(name):
    return [style(f"[!] {name}: data unavailable", "warn")]


def render_header(now):
    line = f"══ SAN DIEGO ══ {now.strftime('%a %Y-%m-%d %H:%M')} "
    line += "═" * max(0, WIDTH - len(line))
    return [style(line, "heading")]


def aqi_category(aqi):
    for limit, name in [(50, "Good"), (100, "Moderate"),
                        (150, "Unhealthy (sens.)"), (200, "Unhealthy"),
                        (300, "Very Unhealthy")]:
        if aqi <= limit:
            return name
    return "Hazardous"


def render_current(fc, air):
    if fc is None:
        return unavailable("CURRENT")
    c = fc["current"]
    glyph, text = wmo(c["code"])
    if air is None:
        aqi_part = "AQI n/a"
    else:
        a = air["aqi"]
        aqi_part = tiered("aqi", a, f"AQI {a} {aqi_category(a)}")
    return [
        heading("CURRENT"),
        f"[*] {glyph} {text} · {c['temp']:.0f}°F (feels {c['feels']:.0f}°F)",
        f"[-] wind {c['wind']:.0f} mph {compass(c['wind_dir'])} · "
        + tiered("gust", c["gust"], f"gusts {c['gust']:.0f} mph"),
        f"[-] humidity {c['rh']}% · dew point {c['dew']:.0f}°F · "
        f"pressure {c['pressure']:.0f} hPa",
        "[-] " + tiered("uv", c["uv"], f"UV {c['uv']:.1f}") + f" · {aqi_part}",
    ]


def hour_index(times, now):
    """Index of the hour containing now.

    Open-Meteo starts the hourly array at local midnight today, which makes
    now.hour the right index, but only by coincidence of the response shape.
    Match the timestamp instead and keep the old assumption as a fallback.
    """
    try:
        return times.index(now.strftime("%Y-%m-%dT%H:00"))
    except ValueError:
        return min(now.hour, max(0, len(times) - 1))


def render_today(fc, now):
    if fc is None:
        return unavailable("TODAY")
    d, h = fc["daily"], fc["hourly"]
    lines = [heading("TODAY"),
             f"[*] hi {d['hi'][0]:.0f}°F · lo {d['lo'][0]:.0f}°F"]
    start = hour_index(h["time"], now)
    callout = None
    for i in range(start, min(start + 24, len(h["pp"]))):
        if h["pp"][i] >= 30:
            hhmm = h["time"][i][11:]
            callout = ("[-] precip: "
                       + tiered("precip", h["pp"][i], f"{h['pp'][i]}% at {hhmm}"))
            break
    if callout:
        lines.append(callout)
    else:
        mx = max(h["pp"][start:start + 24] or [0])
        lines.append(f"[-] precip: no precipitation expected (max {mx}%)")
    cells = []
    for i in range(start, min(start + 12, len(h["temp"]))):
        hh = h["time"][i][11:13]
        cells.append(f"{hh}h{h['temp'][i]:>3.0f}° {h['pp'][i]:>2d}%")
    for row in (cells[:6], cells[6:]):
        if row:
            lines.append("[-] " + " ".join(row))
    return lines


def render_week(fc):
    if fc is None:
        return unavailable("7-DAY")
    d = fc["daily"]
    lines = [heading("7-DAY")]
    for i in range(len(d["date"])):
        day = datetime.strptime(d["date"][i], "%Y-%m-%d")
        glyph, text = wmo(d["code"][i])
        pp = d["pp"][i]
        lines.append(
            f"[-] {day.strftime('%a %m/%d')}  {glyph} {text:<16.16}"
            f"{d['hi'][i]:>4.0f}°/{d['lo'][i]:>3.0f}°  "
            + tiered("precip", pp, f"{pp:>3d}%")
            + f"  {d['wind'][i]:>3.0f} mph")
    return lines


def render_alerts(alerts):
    if alerts is None:
        return unavailable("ALERTS")
    if not alerts:
        return []
    lines = [heading("ALERTS")]
    for a in alerts:
        exp = a["expires"][:16].replace("T", " ")
        txt = f"[!] {a['event'].upper()} (expires {exp}) — details below"
        lines.append(style(txt[:WIDTH], "severe"))
    return lines


# NWS bullets run on a fixed vocabulary: WHAT, WHERE, WHEN and IMPACTS turn
# up in lockstep on nearly every alert, with a short tail of rarer fields.
# A glyph says the same thing as the shouted label in one cell. Only the
# hazard bullet is loud, so the eye lands on it first.
ALERT_BULLETS = {
    "WHAT": ("", "accent"),
    "WHERE": ("", "accent"),
    "AFFECTED AREA": ("", "accent"),
    "WHEN": ("", "accent"),
    "IMPACTS": ("", "severe"),
    "ADDITIONAL DETAILS": ("", "dim"),
    "WINDS": ("", "accent"),
    "RELATIVE HUMIDITY": ("", "accent"),
}
UNKNOWN_BULLET = ("", "accent")
BULLET_RE = re.compile(r"^\*\s*([A-Z][A-Z /&'-]{2,40}?)\.\.\.\s*")
BULLET_INDENT = 7  # "    " + glyph + "  "


def alert_bullet(para):
    """Split an NWS bullet paragraph into (styled glyph, text).

    Returns (None, para) for prose that is not a bullet. An unrecognised
    label keeps its word, so a field NWS adds later cannot silently lose
    its meaning to a generic glyph.
    """
    m = BULLET_RE.match(para)
    if not m:
        return None, para
    label = m.group(1).strip()
    text = para[m.end():]
    if label in ALERT_BULLETS:
        glyph, tone = ALERT_BULLETS[label]
    else:
        glyph, tone = UNKNOWN_BULLET
        text = f"{label.title()}: {text}"
    return style(glyph, tone), text


def render_alert_details(alerts):
    if not alerts:
        return []
    lines = [heading("ALERT DETAILS")]
    for a in alerts:
        exp = a["expires"][:16].replace("T", " ")
        head = f"[!] {a['event'].upper()} — {a['headline']} (expires {exp})"
        for wrapped in textwrap.wrap(head, WIDTH, subsequent_indent="    "):
            lines.append(style(wrapped, "severe"))
        # NWS hard-wraps description paragraphs; unwrap each and rewrap to WIDTH
        body = "\n\n".join(t for t in (a["description"], a["instruction"]) if t)
        for para in re.split(r"\n\s*\n", body):
            words = " ".join(para.split())
            if not words:
                continue
            mark, text = alert_bullet(words)
            if mark is None:
                lines.extend(textwrap.wrap(text, WIDTH, initial_indent="    ",
                                           subsequent_indent="    "))
                continue
            # Hang the wrapped remainder under the text, not under the glyph.
            pad = " " * BULLET_INDENT
            wrapped = textwrap.wrap(text, WIDTH - BULLET_INDENT) or [""]
            lines.append(f"    {mark}  {wrapped[0]}")
            lines.extend(pad + cont for cont in wrapped[1:])
    return lines


def spot_lines(spot, marine, buoy):
    """The model and measured rows for one surf spot."""
    label = f"{spot['label']:<7}"
    out = []
    if marine:
        sst = f" · SST {marine['sst']:.0f}°F" if marine["sst"] is not None else ""
        out.append(f"[*] {label} swell {marine['swell_ft']:.1f} ft @ "
                   f"{marine['swell_s']:.0f} s {marine['swell_dir']} · "
                   f"waves {marine['wave_ft']:.1f} ft{sst}")
    if buoy:
        parts = []
        if buoy["wave_ft"] is not None:
            parts.append(f"waves {buoy['wave_ft']:.1f} ft")
        if buoy["dpd_s"] is not None:
            parts.append(f"@ {buoy['dpd_s']:.0f} s")
        if buoy["dir"]:
            parts.append(buoy["dir"])
        if buoy["water_f"] is not None:
            parts.append(f"· water {buoy['water_f']:.0f}°F")
        if parts:
            out.append(f"[-] {'':<7} measured {' '.join(parts)} "
                       f"(buoy {spot['buoy']})")
    return out


def render_ocean(data, tides):
    body = []
    for spot in SPOTS:
        body += spot_lines(spot, data.get("marine_" + spot["key"]),
                           data.get("buoy_" + spot["key"]))
    if not body and tides is None:
        return unavailable("OCEAN")
    lines = [heading("OCEAN")] + body
    if tides:
        cells = [f"{t['t'][11:]} {t['type']} {t['ft']:.1f}ft" for t in tides]
        lines.append("[-] tides: " + " · ".join(cells))
    return lines


def render_sunmoon(fc, now):
    if fc is None:
        return unavailable("SUN & MOON")
    d = fc["daily"]
    m = moon_phase(now)
    return [
        heading("SUN & MOON"),
        f"[*] sunrise {d['sunrise'][0][11:]} · sunset {d['sunset'][0][11:]}",
        f"[*] {m['glyph']} {m['name']} · {m['illum']:.0f}% illuminated "
        + ("↑" if m["waxing"] else "↓"),
    ]


def kp_text(kp):
    levels = [(4, "quiet"), (5, "active"), (6, "G1 minor storm"),
              (7, "G2 moderate storm"), (8, "G3 strong storm"),
              (9, "G4 severe storm")]
    for limit, name in levels:
        if kp < limit:
            return name
    return "G5 extreme storm"


def render_solar(kp, scales):
    if kp is None and scales is None:
        return unavailable("SOLAR / GEOMAG")
    lines = [heading("SOLAR / GEOMAG")]
    if kp:
        k = kp["kp"]
        lines.append("[*] " + tiered("kp", k, f"Kp {k:.1f} — {kp_text(k)}"))
    if scales:
        cells = []
        for key in ("R", "S", "G"):
            sc, txt = scales["now"][key]
            cells.append(tiered("scale", int(sc), f"{key}{sc} {txt}"))
        lines.append("[-] NOAA scales: " + " · ".join(cells))
        o = scales["outlook"]
        parts = [f"G{o['G'][0]} {o['G'][1]}"]
        if o["R_prob"][0]:
            parts.append(f"R minor {o['R_prob'][0]}% / major {o['R_prob'][1]}%")
        if o["S_prob"]:
            parts.append(f"S {o['S_prob']}%")
        lines.append("[-] outlook (tomorrow): " + " · ".join(parts))
    return lines


# ---------------------------------------------------------------------- main

def _sources():
    out = [
        ("forecast", lambda now: src_forecast()),
        ("air", lambda now: src_air()),
        ("tides", lambda now: src_tides(now.replace(tzinfo=None))),
        ("kp", lambda now: src_kp()),
        ("scales", lambda now: src_scales()),
        ("alerts", lambda now: src_alerts()),
    ]
    for spot in SPOTS:
        # Bind the spot per iteration; a bare closure would capture the last.
        out.append(("marine_" + spot["key"],
                    lambda now, s=spot: src_marine(s["lat"], s["lon"])))
        out.append(("buoy_" + spot["key"],
                    lambda now, s=spot: src_ndbc(s["buoy"])))
    return out


SOURCES = _sources()


def gather(now):
    """Fetch every source concurrently.

    These are a dozen independent HTTP round trips that spend their time
    blocked on sockets, so threads turn the total wall clock into roughly
    the slowest single source. Returns (data, errors); a failed source is
    None in data and its exception is kept for --debug.
    """
    data, errors = {}, {}
    with futures.ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        pending = {pool.submit(fn, now): key for key, fn in SOURCES}
        for fut in futures.as_completed(pending):
            key = pending[fut]
            try:
                data[key] = fut.result()
            except Exception as exc:
                data[key] = None
                errors[key] = exc
    return data, errors


def all_failed(data):
    return all(v is None for v in data.values())


def render_stale_note(stale_count):
    """Say so when the report is built from expired cache."""
    if not stale_count:
        return []
    plural = "" if stale_count == 1 else "s"
    return [style(f"[!] {stale_count} source{plural} served from stale cache "
                  "— network unreachable", "elevated")]


def build_report(data, now, stale_count=0):
    lines = []
    lines += render_header(now)
    lines += render_stale_note(stale_count)
    lines += render_alerts(data["alerts"])
    lines += render_current(data["forecast"], data["air"])
    lines += render_today(data["forecast"], now)
    lines += render_week(data["forecast"])
    lines += render_ocean(data, data["tides"])
    lines += render_sunmoon(data["forecast"], now)
    lines += render_solar(data["kp"], data["scales"])
    lines += render_alert_details(data["alerts"])
    return lines


def resolve_output(args, isatty, env):
    """Return (color, use_pager).

    Color follows the terminal, not stdout: when we page, less still draws
    to the tty, so the pipe we own is no reason to strip ANSI.
    """
    color = isatty and not args.no_color and not env.get("NO_COLOR")
    return color, isatty and not args.no_pager


def pager_command(env):
    """Return (argv, child_env) for the pager.

    less needs -R to pass ANSI through, -F to skip paging output that
    already fits, and -X to leave the report in the scrollback. Those go
    on the argv rather than $LESS so they win over the user's env without
    clobbering the rest of it.

    less also treats Private Use Area codepoints as control characters
    and prints them as "<U+E30D>". That is exactly where the Nerd Font
    weather glyphs live, so declare the PUA printable unless the user
    already has an opinion. Any other $PAGER is run as written.
    """
    argv = shlex.split(env.get("PAGER") or "less")
    child = dict(env)
    if argv and os.path.basename(argv[0]) == "less":
        argv.append("-RFX")
        child.setdefault("LESSUTFCHARDEF", PUA_PRINTABLE)
    return argv, child


def emit(text, use_pager, env=None):
    """Write the report, through a pager when asked.

    Degrades to plain stdout if the pager is missing, and stays quiet if
    the reader quits early or interrupts.
    """
    if not use_pager:
        print(text)
        return
    argv, child_env = pager_command(os.environ if env is None else env)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, env=child_env,
                                universal_newlines=True)
    except (OSError, ValueError):
        print(text)
        return
    try:
        proc.communicate(text + "\n")
    except (BrokenPipeError, KeyboardInterrupt):
        proc.wait()


def main(argv=None):
    global NO_CACHE, COLOR
    ap = argparse.ArgumentParser(prog="sdwx",
                                 description="San Diego terminal weather")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the response cache")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI color")
    ap.add_argument("--no-pager", action="store_true",
                    help="write straight to stdout instead of $PAGER")
    ap.add_argument("--debug", action="store_true",
                    help="report why sources failed, on stderr")
    args = ap.parse_args(argv)
    NO_CACHE = args.no_cache
    COLOR, use_pager = resolve_output(args, sys.stdout.isatty(), os.environ)
    pruned = 0 if NO_CACHE else prune_cache()
    now = datetime.now().astimezone()
    data, errors = gather(now)
    emit("\n".join(build_report(data, now, len(STALE))), use_pager)
    if args.debug:
        for key in sorted(errors):
            exc = errors[key]
            print(f"{key}: {type(exc).__name__}: {exc}", file=sys.stderr)
        for url in sorted(STALE):
            print(f"stale cache: {url}", file=sys.stderr)
        print(f"pruned {pruned} expired cache entries", file=sys.stderr)
    return 1 if all_failed(data) else 0


if __name__ == "__main__":
    sys.exit(main())
