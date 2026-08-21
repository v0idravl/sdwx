```text
███████╗██████╗ ██╗    ██╗██╗  ██╗
██╔════╝██╔══██╗██║    ██║╚██╗██╔╝
███████╗██║  ██║██║ █╗ ██║ ╚███╔╝
╚════██║██║  ██║██║███╗██║ ██╔██╗
███████║██████╔╝╚███╔███╔╝██╔╝ ██╗
╚══════╝╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═╝
  san diego · terminal weather · one file, stdlib only
```

![python](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)
![deps](https://img.shields.io/badge/dependencies-none-44CC11)
![apis](https://img.shields.io/badge/APIs-free%20%26%20keyless-1BB91F)
![tests](https://img.shields.io/badge/tests-104%20offline-89E051)

San Diego weather in your terminal: current conditions, hourly and
7-day forecast, active alerts, surf and ocean conditions for Ocean Beach and
Solana Beach, tides, sun and moon, and solar/geomagnetic activity in one
80-column report. Home conditions are for Golden Hill.

Sdwx pulls from free, keyless public APIs and renders a compact ANSI-colored
summary built for a tmux pane or a shell login hook. It is a single file,
Python 3 standard library only: no dependencies, no API keys, no config.

---

## ⚡ 30-second demo

```bash
git clone https://github.com/v0idravl/sdwx.git
cd sdwx
python3 sdwx.py
```

Example output (`--no-color`):

```text
══ SAN DIEGO ══ Sun 2026-07-26 23:17 ═══════════════════════════════════════════
── ALERTS ──────────────────────────────────────────────────────────────────────
[!] BEACH HAZARDS STATEMENT (expires 2026-07-27 15:00) — details below
[!] HEAT ADVISORY (expires 2026-07-27 06:00) — details below
── CURRENT ─────────────────────────────────────────────────────────────────────
[*]  Mostly clear · 71°F (feels 76°F)
[-] wind 3 mph SSW · gusts 5 mph
[-] humidity 90% · dew point 68°F · pressure 1013 hPa
[-] UV 0.0 · AQI 61 Moderate
── TODAY ───────────────────────────────────────────────────────────────────────
[*] hi 81°F · lo 67°F
[-] precip: no precipitation expected (max 1%)
[-] 23h 71°  0% 00h 70°  0% 01h 71°  0% 02h 71°  0% 03h 70°  0% 04h 69°  0%
[-] 05h 69°  0% 06h 69°  0% 07h 69°  0% 08h 70°  0% 09h 71°  0% 10h 76°  0%
── 7-DAY ───────────────────────────────────────────────────────────────────────
[-] Sun 07/26   Overcast          81°/ 67°   28%   10 mph
[-] Mon 07/27   Fog               80°/ 67°    1%   10 mph
[-] Tue 07/28   Fog               77°/ 64°    1%    9 mph
[-] Wed 07/29   Clear             76°/ 68°    0%   10 mph
[-] Thu 07/30   Overcast          79°/ 70°    0%    9 mph
[-] Fri 07/31   Overcast          83°/ 71°    0%    9 mph
[-] Sat 08/01   Partly cloudy     83°/ 74°    2%    9 mph
── OCEAN ───────────────────────────────────────────────────────────────────────
[*] OB      swell 3.1 ft @ 14 s SSW · waves 3.8 ft · SST 74°F
[-]         measured waves 3.3 ft @ 8 s WNW · water 77°F (buoy 46254)
[*] Solana  swell 2.8 ft @ 14 s SSW · waves 3.5 ft · SST 75°F
[-]         measured waves 4.3 ft @ 18 s WSW · water 76°F (buoy 46266)
[-] tides: 03:21 L -0.3ft · 09:58 H 3.7ft · 14:16 L 2.6ft · 20:31 H 6.0ft
── SUN & MOON ──────────────────────────────────────────────────────────────────
[*] sunrise 05:58 · sunset 19:51
[*] 🌔 Waxing Gibbous · 94% illuminated ↑
── SOLAR / GEOMAG ──────────────────────────────────────────────────────────────
[*] Kp 0.0 — quiet
[-] NOAA scales: R0 none · S0 none · G0 none
[-] outlook (tomorrow): G0 none · R minor 45% / major 10% · S 5%
── ALERT DETAILS ───────────────────────────────────────────────────────────────
[!] BEACH HAZARDS STATEMENT — Beach Hazards Statement issued July 26 at 10:55PM
    PDT until July 29 at 6:00PM PDT by NWS San Diego CA (expires 2026-07-27
    15:00)
      Surf of 3 to 6 feet with sets to 7 feet expected. Highest surf will be on
       south-southwest facing beaches.
      San Diego County Coastal Areas and Orange County Coastal Areas.
      Through Wednesday afternoon.
      High rip current risk, longshore current risk and dangerous swimming
       conditions. Minor coastal flooding is possible during evening high tides
       which are forecast to be 6.0 to 6.5 feet this evening though much of this
       week.
...
```

Sky conditions use Nerd Font weather glyphs; run it in a Nerd Font terminal
for best results. Everything else is plain Unicode.

Alert bullets replace the NWS `* WHAT...` / `* WHERE...` shouting with a glyph.
Only the hazard bullet is colored loudly, so it reads first:

| Glyph | Field | Meaning |
|---|---|---|
|  | What | what is happening |
|  | Where | which areas |
|  | When | the time window |
|  | Impacts | the hazard itself |
|  | Additional Details | supporting detail |

A field outside this set keeps its name, so nothing NWS adds later is lost.

---

## ⌨️ Usage

```bash
python3 sdwx.py              # full report, paged
python3 sdwx.py --no-cache   # bypass the 10-minute response cache
python3 sdwx.py --no-color   # plain text
python3 sdwx.py --no-pager   # straight to stdout, for tmux and scripts
python3 sdwx.py --debug      # explain failed sources on stderr
```

In a terminal the report goes through `$PAGER` (default `less`), which gets
`-RFX` so color survives, short reports never redraw, and quitting leaves the
report in your scrollback. It also gets `LESSUTFCHARDEF` set, because less
otherwise treats the Private Use Area where Nerd Font glyphs live as
non-printable and shows `<U+E30D>` instead of the glyph. Both are applied so
they layer over your own `$LESS` rather than replace it, and neither is
touched if you have already set them. Another `$PAGER` is run exactly as you
wrote it.

Color follows the terminal rather than stdout, so paging keeps it. Redirecting
or piping to another command drops both the color and the pager, and
`--no-color` or a `NO_COLOR` environment variable turns color off anywhere.

Values that cross attention thresholds (UV, AQI, gusts, precipitation
probability, Kp, NOAA R/S/G scales) are tinted green/yellow/orange/red; in
no-color mode severe values are marked `[!]` instead.

---

## 🌤 Report sections

| Section | Contents |
|---|---|
| ALERTS | active NWS alerts for the point, shown only when present |
| CURRENT | sky, temp/feels-like, wind and gusts, humidity, dew point, pressure, UV, US AQI |
| TODAY | hi/lo, first notable precipitation window, next 12 hours of temp and precip probability |
| 7-DAY | daily sky, hi/lo, precip probability, max wind |
| OCEAN | modeled and measured swell/waves/water temp for Ocean Beach and Solana Beach, next four tides |
| SUN & MOON | sunrise/sunset, moon phase and illumination |
| SOLAR / GEOMAG | planetary K-index, current NOAA R/S/G scales, tomorrow's outlook |

---

## 📡 Data sources

All sources are free and keyless.

| Source | Provides |
|---|---|
| [Open-Meteo](https://open-meteo.com/) forecast + air quality + marine APIs | current conditions, hourly/daily forecast, US AQI, modeled swell and SST |
| [NOAA CO-OPS](https://tidesandcurrents.noaa.gov/) station 9410230 (La Jolla, Scripps Wharf) | open-coast tide predictions |
| [NDBC](https://www.ndbc.noaa.gov/) buoys 46254 (Scripps Nearshore) and 46266 (Del Mar Nearshore) | measured wave height, period, direction, ocean temperature per spot |
| [NOAA SWPC](https://www.swpc.noaa.gov/) | planetary K-index, R/S/G scales and outlook |
| [NWS API](https://www.weather.gov/documentation/services-web-api) | active weather alerts |

All sources are fetched concurrently, so a cold run costs about one round
trip rather than ten.

Responses are cached in `~/.cache/sdwx/` with a per-source lifetime: three
minutes for alerts, six hours for tide predictions, ten to thirty minutes for
everything else. Repeated runs (status bars, login hooks) stay fast and polite
to the upstream APIs.

Each source fails independently. When a fetch fails, sdwx falls back to the
cached response even if it has expired, up to 24 hours old, and says so at the
top of the report; past that the section degrades to `[!] data unavailable`
and the rest still renders. Entries older than that are deleted on the next
run, since the tide request embeds its date and would otherwise leave a file
behind every day. `--debug` prints the underlying exception for each
failure. Exit status is nonzero only when every source fails.

---

## 🛠 Requirements

Python 3.8+. Nothing else: stdlib only, no third-party packages.

---

## 🧪 Tests

```bash
python3 -m unittest test_sdwx -v
```

104 tests, all offline. Fetchers are exercised against canned API payloads via
an injectable HTTP getter, so the suite never touches the network.
