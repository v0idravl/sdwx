import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from datetime import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sdwx

_TMP_CACHE = None


def setUpModule():
    global _TMP_CACHE
    _TMP_CACHE = tempfile.TemporaryDirectory()
    sdwx.CACHE_DIR = Path(_TMP_CACHE.name)


def tearDownModule():
    _TMP_CACHE.cleanup()


class TestStyle(unittest.TestCase):
    def setUp(self):
        sdwx.COLOR = True

    def test_style_wraps_ansi(self):
        s = sdwx.style("hi", "ok")
        self.assertTrue(s.startswith("\x1b[") and s.endswith("\x1b[0m"))
        self.assertIn("hi", s)

    def test_style_plain_when_color_off(self):
        sdwx.COLOR = False
        self.assertEqual(sdwx.style("hi", "ok"), "hi")

    def test_visible_len_strips_ansi(self):
        self.assertEqual(sdwx.visible_len(sdwx.style("hello", "warn")), 5)

    def test_every_defined_style_is_used(self):
        """A palette entry nothing references is dead weight."""
        source = Path(sdwx.__file__).read_text()
        # Look only past the palette itself, or every name matches trivially.
        body = source.split("ANSI = {", 1)[1].split("}", 1)[1]
        unused = sorted(n for n in sdwx.ANSI
                        if n != "reset" and f'"{n}"' not in body)
        self.assertEqual(unused, [], f"styles defined but never used: {unused}")

    def test_http_timeout_is_short_enough_to_stay_snappy(self):
        self.assertLessEqual(sdwx.HTTP_TIMEOUT, 5)


class TestTier(unittest.TestCase):
    def test_uv(self):
        self.assertEqual(sdwx.tier("uv", 1), "ok")
        self.assertEqual(sdwx.tier("uv", 4), "elevated")
        self.assertEqual(sdwx.tier("uv", 7), "high")
        self.assertEqual(sdwx.tier("uv", 9), "severe")

    def test_aqi(self):
        self.assertEqual(sdwx.tier("aqi", 40), "ok")
        self.assertEqual(sdwx.tier("aqi", 75), "elevated")
        self.assertEqual(sdwx.tier("aqi", 120), "high")
        self.assertEqual(sdwx.tier("aqi", 180), "severe")

    def test_kp(self):
        self.assertEqual(sdwx.tier("kp", 2), "ok")
        self.assertEqual(sdwx.tier("kp", 4), "elevated")
        self.assertEqual(sdwx.tier("kp", 6), "high")
        self.assertEqual(sdwx.tier("kp", 7), "severe")

    def test_precip_never_severe(self):
        self.assertEqual(sdwx.tier("precip", 10), "ok")
        self.assertEqual(sdwx.tier("precip", 50), "elevated")
        self.assertEqual(sdwx.tier("precip", 95), "high")

    def test_scale(self):
        self.assertEqual(sdwx.tier("scale", 0), "ok")
        self.assertEqual(sdwx.tier("scale", 2), "elevated")
        self.assertEqual(sdwx.tier("scale", 3), "high")
        self.assertEqual(sdwx.tier("scale", 5), "severe")

    def test_tiered_plaintext_severe_prefix(self):
        sdwx.COLOR = False
        self.assertEqual(sdwx.tiered("uv", 9, "UV 9"), "[!]UV 9")
        self.assertEqual(sdwx.tiered("uv", 1, "UV 1"), "UV 1")
        sdwx.COLOR = True


class TestConversions(unittest.TestCase):
    def test_compass(self):
        self.assertEqual(sdwx.compass(0), "N")
        self.assertEqual(sdwx.compass(225), "SW")
        self.assertEqual(sdwx.compass(312), "NW")
        self.assertEqual(sdwx.compass(359), "N")

    def test_c_to_f(self):
        self.assertAlmostEqual(sdwx.c_to_f(17.0), 62.6)

    def test_m_to_ft(self):
        self.assertAlmostEqual(sdwx.m_to_ft(0.94), 3.08, places=2)


class TestMoon(unittest.TestCase):
    def test_new_moon_2024_eclipse(self):
        m = sdwx.moon_phase(datetime(2024, 4, 8, 18, 21, tzinfo=timezone.utc))
        self.assertLess(m["illum"], 2)
        self.assertEqual(m["name"], "New Moon")

    def test_full_moon_2024_04(self):
        m = sdwx.moon_phase(datetime(2024, 4, 23, 23, 49, tzinfo=timezone.utc))
        self.assertGreater(m["illum"], 98)
        self.assertEqual(m["name"], "Full Moon")

    def test_first_quarter_2024_04(self):
        m = sdwx.moon_phase(datetime(2024, 4, 15, 19, 13, tzinfo=timezone.utc))
        self.assertEqual(m["name"], "First Quarter")
        self.assertTrue(40 < m["illum"] < 60)

    def test_waxing_after_new_moon(self):
        m = sdwx.moon_phase(datetime(2024, 4, 10, 12, 0, tzinfo=timezone.utc))
        self.assertTrue(m["waxing"])

    def test_waning_after_full_moon(self):
        m = sdwx.moon_phase(datetime(2024, 4, 26, 12, 0, tzinfo=timezone.utc))
        self.assertFalse(m["waxing"])

    def test_waxing_at_new_moon_boundary(self):
        # just past the modeled (mean synodic) new moon, ~22:45 UTC that day
        m = sdwx.moon_phase(datetime(2024, 4, 8, 23, 0, tzinfo=timezone.utc))
        self.assertEqual(m["name"], "New Moon")
        self.assertTrue(m["waxing"])

    def test_waning_at_full_moon_boundary(self):
        m = sdwx.moon_phase(datetime(2024, 4, 24, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(m["name"], "Full Moon")
        self.assertFalse(m["waxing"])


class TestWmo(unittest.TestCase):
    def test_known_codes(self):
        glyph, text = sdwx.wmo(0)
        self.assertEqual(text, "Clear")
        self.assertTrue(glyph)
        self.assertEqual(sdwx.wmo(95)[1], "Thunderstorm")

    def test_unknown_code_fallback(self):
        glyph, text = sdwx.wmo(42)
        self.assertIn("42", text)


class TestFetch(unittest.TestCase):
    def setUp(self):
        self._old = sdwx.CACHE_DIR
        self.tmp = tempfile.TemporaryDirectory()
        sdwx.CACHE_DIR = Path(self.tmp.name)
        sdwx.NO_CACHE = False
        self.calls = 0

    def tearDown(self):
        sdwx.CACHE_DIR = self._old
        sdwx.NO_CACHE = False
        self.tmp.cleanup()

    def fake_get(self, url, headers):
        self.calls += 1
        return '{"n": %d}' % self.calls

    def test_caches_second_call(self):
        a = sdwx.fetch("http://x/one", _get=self.fake_get)
        b = sdwx.fetch("http://x/one", _get=self.fake_get)
        self.assertEqual(a, b)
        self.assertEqual(self.calls, 1)

    def test_no_cache_flag_bypasses(self):
        sdwx.fetch("http://x/two", _get=self.fake_get)
        sdwx.NO_CACHE = True
        sdwx.fetch("http://x/two", _get=self.fake_get)
        self.assertEqual(self.calls, 2)

    def test_expired_cache_refetches(self):
        import os
        sdwx.fetch("http://x/three", _get=self.fake_get)
        p = sdwx.cache_path("http://x/three")
        old = time.time() - 9999
        os.utime(p, (old, old))
        sdwx.fetch("http://x/three", _get=self.fake_get)
        self.assertEqual(self.calls, 2)

    def test_fetch_json_parses(self):
        j = sdwx.fetch_json("http://x/four", _get=self.fake_get)
        self.assertEqual(j["n"], 1)

    def age(self, url, seconds):
        p = sdwx.cache_path(url)
        old = time.time() - seconds
        os.utime(p, (old, old))

    def dead_get(self, url, headers):
        raise OSError("network unreachable")

    def test_expired_cache_serves_stale_when_offline(self):
        sdwx.STALE.clear()
        body = sdwx.fetch("http://x/stale", _get=self.fake_get)
        self.age("http://x/stale", 9999)
        again = sdwx.fetch("http://x/stale", _get=self.dead_get)
        self.assertEqual(again, body)
        self.assertIn("http://x/stale", sdwx.STALE)
        sdwx.STALE.clear()

    def test_stale_beyond_max_age_still_raises(self):
        sdwx.STALE.clear()
        sdwx.fetch("http://x/ancient", _get=self.fake_get)
        self.age("http://x/ancient", sdwx.STALE_MAX + 60)
        with self.assertRaises(OSError):
            sdwx.fetch("http://x/ancient", _get=self.dead_get)
        self.assertEqual(sdwx.STALE, set())

    def test_no_stale_fallback_under_no_cache(self):
        sdwx.STALE.clear()
        sdwx.fetch("http://x/nc", _get=self.fake_get)
        self.age("http://x/nc", 9999)
        sdwx.NO_CACHE = True
        with self.assertRaises(OSError):
            sdwx.fetch("http://x/nc", _get=self.dead_get)
        self.assertEqual(sdwx.STALE, set())

    def test_fresh_cache_never_marked_stale(self):
        sdwx.STALE.clear()
        sdwx.fetch("http://x/fresh", _get=self.fake_get)
        sdwx.fetch("http://x/fresh", _get=self.dead_get)
        self.assertEqual(sdwx.STALE, set())

    def test_prune_removes_only_entries_past_stale_max(self):
        sdwx.fetch("http://x/keep", _get=self.fake_get)
        sdwx.fetch("http://x/borderline", _get=self.fake_get)
        sdwx.fetch("http://x/drop", _get=self.fake_get)
        self.age("http://x/borderline", sdwx.STALE_MAX - 60)
        self.age("http://x/drop", sdwx.STALE_MAX + 60)
        self.assertEqual(sdwx.prune_cache(), 1)
        self.assertTrue(sdwx.cache_path("http://x/keep").exists())
        self.assertTrue(sdwx.cache_path("http://x/borderline").exists())
        self.assertFalse(sdwx.cache_path("http://x/drop").exists())

    def test_prune_never_drops_a_servable_fallback(self):
        """Anything prune removes was already refused as a stale fallback."""
        sdwx.STALE.clear()
        sdwx.fetch("http://x/edge", _get=self.fake_get)
        self.age("http://x/edge", sdwx.STALE_MAX - 60)
        sdwx.prune_cache()
        self.assertEqual(sdwx.fetch("http://x/edge", _get=self.dead_get),
                         '{"n": 1}')
        sdwx.STALE.clear()

    def test_prune_survives_a_missing_cache_dir(self):
        sdwx.CACHE_DIR = Path(self.tmp.name) / "does-not-exist"
        self.assertEqual(sdwx.prune_cache(), 0)

    def test_prune_of_empty_cache_is_zero(self):
        self.assertEqual(sdwx.prune_cache(), 0)

    def test_dated_urls_do_not_accumulate_forever(self):
        """The tide URL embeds its date, so it mints a key every day."""
        get = lambda url, headers: '{"predictions": []}'
        for day in range(1, 6):
            sdwx.src_tides(dt(2026, 7, day, 12, 0), _get=get)
        self.assertEqual(len(list(sdwx.CACHE_DIR.glob("*.cache"))), 5)
        for p in sdwx.CACHE_DIR.glob("*.cache"):
            old = time.time() - (sdwx.STALE_MAX + 60)
            os.utime(p, (old, old))
        self.assertEqual(sdwx.prune_cache(), 5)
        self.assertEqual(list(sdwx.CACHE_DIR.glob("*.cache")), [])

    def test_per_source_ttl_is_honored(self):
        """Tide predictions outlive the default TTL by hours."""
        sdwx.fetch("http://x/ttl", ttl=sdwx.TTL["tides"], _get=self.fake_get)
        self.age("http://x/ttl", sdwx.CACHE_TTL + 60)
        sdwx.fetch("http://x/ttl", ttl=sdwx.TTL["tides"], _get=self.fake_get)
        self.assertEqual(self.calls, 1)

    def test_no_cache_never_writes(self):
        sdwx.NO_CACHE = True
        sdwx.fetch("http://x/five", _get=self.fake_get)
        self.assertFalse(sdwx.cache_path("http://x/five").exists())


FORECAST_FIX = {
    "current": {"temperature_2m": 64.0, "apparent_temperature": 66.3,
                "relative_humidity_2m": 88, "dew_point_2m": 60.4,
                "weather_code": 0, "wind_speed_10m": 3.0,
                "wind_direction_10m": 312, "wind_gusts_10m": 4.0,
                "uv_index": 0.0, "pressure_msl": 1016.9},
    "daily": {"time": ["2026-07-03"] * 7,
              "temperature_2m_max": [81.2] * 7, "temperature_2m_min": [62.6] * 7,
              "precipitation_probability_max": [5] * 7, "weather_code": [3] * 7,
              "wind_speed_10m_max": [10.0] * 7, "wind_gusts_10m_max": [15.0] * 7,
              "uv_index_max": [9.0] * 7,
              "sunrise": ["2026-07-03T05:45"] * 7, "sunset": ["2026-07-03T20:01"] * 7},
    "hourly": {"time": ["2026-07-03T%02d:00" % h for h in range(24)] * 7,
               "temperature_2m": [70.0] * 168,
               "precipitation_probability": [0] * 168,
               "weather_code": [1] * 168},
}

MARINE_FIX = {
    "current": {"sea_surface_temperature": 17.0, "wave_height": 0.94,
                "wave_period": 11.2, "wave_direction": 219,
                "swell_wave_height": 0.76, "swell_wave_period": 12.3,
                "swell_wave_direction": 197, "wind_wave_height": 0.16}
}


def fake_getter(payload):
    def _get(url, headers):
        return json.dumps(payload)
    return _get


class TestOpenMeteoSources(unittest.TestCase):
    def setUp(self):
        sdwx.NO_CACHE = True

    def tearDown(self):
        sdwx.NO_CACHE = False

    def test_src_forecast_normalizes(self):
        fc = sdwx.src_forecast(_get=fake_getter(FORECAST_FIX))
        self.assertEqual(fc["current"]["temp"], 64.0)
        self.assertEqual(fc["current"]["feels"], 66.3)
        self.assertEqual(len(fc["daily"]["hi"]), 7)
        self.assertEqual(fc["daily"]["sunrise"][0], "2026-07-03T05:45")
        self.assertEqual(len(fc["hourly"]["temp"]), 168)

    def test_src_air(self):
        aqi = sdwx.src_air(_get=fake_getter({"current": {"us_aqi": 34}}))
        self.assertEqual(aqi, {"aqi": 34})

    def test_src_marine_converts_imperial(self):
        m = sdwx.src_marine(32.75, -117.25, _get=fake_getter(MARINE_FIX))
        self.assertAlmostEqual(m["sst"], 62.6, places=1)
        self.assertAlmostEqual(m["swell_ft"], 2.5, places=1)
        self.assertEqual(m["swell_dir"], "SSW")
        self.assertAlmostEqual(m["wave_ft"], 3.1, places=1)

    def test_src_marine_none_sst(self):
        fix = {"current": dict(MARINE_FIX["current"], sea_surface_temperature=None)}
        m = sdwx.src_marine(32.75, -117.25, _get=fake_getter(fix))
        self.assertIsNone(m["sst"])


TIDES_FIX = {"predictions": [
    {"t": "2026-07-03 06:21", "v": "-0.231", "type": "L"},
    {"t": "2026-07-03 12:56", "v": "4.033", "type": "H"},
    {"t": "2026-07-03 17:37", "v": "2.569", "type": "L"},
    {"t": "2026-07-03 23:36", "v": "5.516", "type": "H"},
    {"t": "2026-07-04 06:50", "v": "-0.4", "type": "L"},
    {"t": "2026-07-04 13:20", "v": "4.1", "type": "H"},
]}

NDBC_FIX = """#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft
2026 07 04 03 56  MM   MM   MM   0.5    17   4.2 287     MM    MM  22.0    MM   MM   MM    MM
"""

SCALES_FIX = {
    "0": {"DateStamp": "2026-07-04", "TimeStamp": "05:02:00",
          "R": {"Scale": "0", "Text": "none"}, "S": {"Scale": "0", "Text": "none"},
          "G": {"Scale": "1", "Text": "minor"}},
    "1": {"R": {"Scale": None, "Text": None, "MinorProb": "70", "MajorProb": "20"},
          "S": {"Scale": None, "Text": None, "Prob": "20"},
          "G": {"Scale": "1", "Text": "minor"}},
}

ALERTS_FIX = {"features": [
    {"properties": {"event": "Red Flag Warning",
                    "headline": "Red Flag Warning until Friday",
                    "expires": "2026-07-04T18:00:00-07:00",
                    "description": "* WHAT...Gusty winds and low humidity.\n\n"
                                   "* WHERE...Inland valleys.",
                    "instruction": "Avoid outdoor burning."}}
]}


class TestNoaaSources(unittest.TestCase):
    def setUp(self):
        sdwx.NO_CACHE = True

    def tearDown(self):
        sdwx.NO_CACHE = False

    def test_src_tides_next_four(self):
        now = dt(2026, 7, 3, 14, 0)
        tides = sdwx.src_tides(now, _get=fake_getter(TIDES_FIX))
        self.assertEqual(len(tides), 4)
        self.assertEqual(tides[0]["t"], "2026-07-03 17:37")
        self.assertEqual(tides[0]["type"], "L")
        self.assertAlmostEqual(tides[1]["ft"], 5.5, places=1)

    def test_src_ndbc_parses_mm_as_none(self):
        def _get(url, headers):
            return NDBC_FIX
        b = sdwx.src_ndbc("46254", _get=_get)
        self.assertAlmostEqual(b["wave_ft"], 1.6, places=1)
        self.assertAlmostEqual(b["dpd_s"], 17.0)
        self.assertEqual(b["dir"], "WNW")
        self.assertAlmostEqual(b["water_f"], 71.6, places=1)

    def test_src_kp_last_entry(self):
        fix = [{"estimated_kp": 3.0}, {"estimated_kp": 6.33}]
        self.assertEqual(sdwx.src_kp(_get=fake_getter(fix))["kp"], 6.33)

    def test_src_scales(self):
        s = sdwx.src_scales(_get=fake_getter(SCALES_FIX))
        self.assertEqual(s["now"]["G"], ("1", "minor"))
        self.assertEqual(s["outlook"]["R_prob"], ("70", "20"))

    def test_src_alerts(self):
        a = sdwx.src_alerts(_get=fake_getter(ALERTS_FIX))
        self.assertEqual(a[0]["event"], "Red Flag Warning")
        self.assertIn("Gusty winds", a[0]["description"])
        self.assertEqual(a[0]["instruction"], "Avoid outdoor burning.")

    def test_src_alerts_empty(self):
        self.assertEqual(sdwx.src_alerts(_get=fake_getter({"features": []})), [])


ALERT_LONG = {
    "event": "Beach Hazards Statement",
    "headline": ("Beach Hazards Statement issued July 11 at 2:41AM PDT "
                 "until July 12 at 9:00PM PDT by NWS San Diego CA"),
    "expires": "2026-07-12T21:00:00-07:00",
    "description": ("* WHAT...Minor coastal flooding due to "
                    "high astronomical tides\nof 7 to 7.5 feet and surf "
                    "to 3 to 5 feet.\n\n"
                    "* WHERE...San Diego County coastal areas and Orange "
                    "County\ncoastal areas.\n\n"
                    "* WHEN...From late Sunday morning through Tuesday "
                    "evening."),
    "instruction": ("Remain out of the water to avoid hazardous swimming "
                    "conditions."),
}


def all_lines_fit(case, lines):
    for ln in lines:
        case.assertLessEqual(sdwx.visible_len(ln), 80, repr(ln))


class TestRenderWeather(unittest.TestCase):
    def setUp(self):
        sdwx.COLOR = False
        sdwx.NO_CACHE = True
        self.fc = sdwx.src_forecast(_get=fake_getter(FORECAST_FIX))

    def tearDown(self):
        sdwx.COLOR = True
        sdwx.NO_CACHE = False

    def test_heading_width(self):
        h = sdwx.heading("CURRENT")
        self.assertEqual(sdwx.visible_len(h), 80)
        self.assertIn("CURRENT", h)

    def test_render_header(self):
        lines = sdwx.render_header(dt(2026, 7, 3, 14, 10))
        self.assertTrue(any("SAN DIEGO" in ln for ln in lines))
        all_lines_fit(self, lines)

    def test_render_current(self):
        lines = sdwx.render_current(self.fc, {"aqi": 34})
        joined = "\n".join(lines)
        self.assertIn("64", joined)
        self.assertIn("feels 66", joined)
        self.assertIn("NW", joined)
        self.assertIn("AQI 34 Good", joined)
        all_lines_fit(self, lines)

    def test_render_current_no_air(self):
        lines = sdwx.render_current(self.fc, None)
        self.assertIn("AQI n/a", "\n".join(lines))

    def test_render_current_unavailable(self):
        lines = sdwx.render_current(None, None)
        self.assertIn("data unavailable", lines[0])

    def test_render_today_dry(self):
        lines = sdwx.render_today(self.fc, dt(2026, 7, 3, 14, 0))
        joined = "\n".join(lines)
        self.assertIn("hi 81", joined)
        self.assertIn("no precipitation expected", joined)
        all_lines_fit(self, lines)

    def test_render_today_rain_callout(self):
        fix = json.loads(json.dumps(FORECAST_FIX))
        fix["hourly"]["precipitation_probability"][16] = 60
        fc = sdwx.src_forecast(_get=fake_getter(fix))
        joined = "\n".join(sdwx.render_today(fc, dt(2026, 7, 3, 14, 0)))
        self.assertIn("60%", joined)
        self.assertIn("16:00", joined)

    def test_render_week_seven_rows(self):
        lines = sdwx.render_week(self.fc)
        rows = [ln for ln in lines if ln.startswith("[-]")]
        self.assertEqual(len(rows), 7)
        all_lines_fit(self, lines)


class TestHourIndex(unittest.TestCase):
    def times(self, start_hour, n=48):
        return [f"2026-07-26T{(start_hour + i) % 24:02d}:00" for i in range(n)]

    def test_matches_by_timestamp(self):
        now = dt(2026, 7, 26, 22, 41)
        self.assertEqual(sdwx.hour_index(self.times(0), now), 22)

    def test_does_not_assume_array_starts_at_midnight(self):
        """The old now.hour indexing silently read the wrong hour here."""
        times = [f"2026-07-26T{h:02d}:00" for h in range(6, 24)]
        now = dt(2026, 7, 26, 22, 41)
        self.assertEqual(sdwx.hour_index(times, now), 16)
        self.assertEqual(times[sdwx.hour_index(times, now)],
                         "2026-07-26T22:00")

    def test_falls_back_when_absent(self):
        now = dt(2026, 7, 26, 22, 41)
        self.assertEqual(sdwx.hour_index(["2026-07-25T00:00"], now), 0)

    def test_fallback_never_indexes_past_end(self):
        now = dt(2026, 7, 26, 23, 0)
        idx = sdwx.hour_index(["2026-07-25T00:00", "2026-07-25T01:00"], now)
        self.assertLess(idx, 2)


class TestAqiCategory(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(sdwx.aqi_category(34), "Good")
        self.assertEqual(sdwx.aqi_category(75), "Moderate")
        self.assertEqual(sdwx.aqi_category(120), "Unhealthy (sens.)")
        self.assertEqual(sdwx.aqi_category(180), "Unhealthy")
        self.assertEqual(sdwx.aqi_category(250), "Very Unhealthy")
        self.assertEqual(sdwx.aqi_category(400), "Hazardous")


class TestRenderExtras(unittest.TestCase):
    def setUp(self):
        sdwx.COLOR = False
        sdwx.NO_CACHE = True

    def tearDown(self):
        sdwx.COLOR = True
        sdwx.NO_CACHE = False

    def test_render_alerts_empty_is_no_lines(self):
        self.assertEqual(sdwx.render_alerts([]), [])

    def test_render_alerts_none_is_unavailable(self):
        self.assertIn("data unavailable", sdwx.render_alerts(None)[0])

    def test_render_alerts_content(self):
        lines = sdwx.render_alerts([{"event": "Red Flag Warning",
                                     "headline": "until Friday evening",
                                     "expires": "2026-07-04T18:00:00-07:00"}])
        joined = "\n".join(lines)
        self.assertIn("[!]", joined)
        self.assertIn("RED FLAG WARNING", joined.upper())
        all_lines_fit(self, lines)

    def test_render_alerts_one_line_per_alert(self):
        alerts = [ALERT_LONG,
                  {"event": "Red Flag Warning",
                   "headline": "Red Flag Warning until Friday",
                   "expires": "2026-07-04T18:00:00-07:00",
                   "description": "", "instruction": ""}]
        lines = sdwx.render_alerts(alerts)
        self.assertEqual(len(lines), 3)  # heading + one line per alert
        self.assertIn("BEACH HAZARDS STATEMENT", lines[1])
        self.assertIn("details below", lines[1])
        self.assertIn("expires 2026-07-12 21:00", lines[1])
        all_lines_fit(self, lines)

    def test_render_alert_details_full_text(self):
        lines = sdwx.render_alert_details([ALERT_LONG])
        joined = " ".join(" ".join(ln.split()) for ln in lines)
        for word in ALERT_LONG["headline"].split():
            self.assertIn(word, joined)
        self.assertIn("high astronomical tides of 7 to 7.5 feet", joined)
        self.assertIn("San Diego County coastal areas", joined)
        self.assertIn("Remain out of the water", joined)
        all_lines_fit(self, lines)

    def test_render_alert_details_drops_shouted_labels(self):
        joined = "\n".join(sdwx.render_alert_details([ALERT_LONG]))
        for label in ("* WHAT", "* WHERE", "* WHEN", "WHERE...", "WHEN..."):
            self.assertNotIn(label, joined)
        for label in ("WHAT", "WHERE", "WHEN"):
            self.assertIn(sdwx.ALERT_BULLETS[label][0], joined)

    def test_render_alert_details_hangs_wrapped_text(self):
        """Continuation lines align under the text, not under the glyph."""
        sdwx.COLOR = False
        try:
            lines = sdwx.render_alert_details([ALERT_LONG])
            what = next(i for i, ln in enumerate(lines)
                        if sdwx.ALERT_BULLETS["WHAT"][0] in ln)
            cont = lines[what + 1]
            self.assertTrue(cont.startswith(" " * sdwx.BULLET_INDENT))
            self.assertFalse(cont.startswith(" " * (sdwx.BULLET_INDENT + 1)))
        finally:
            sdwx.COLOR = True

    def test_render_alert_details_empty_and_none(self):
        self.assertEqual(sdwx.render_alert_details([]), [])
        self.assertEqual(sdwx.render_alert_details(None), [])


class TestAlertBullet(unittest.TestCase):
    def setUp(self):
        sdwx.COLOR = False

    def tearDown(self):
        sdwx.COLOR = True

    def test_known_label_becomes_glyph_only(self):
        mark, text = sdwx.alert_bullet("* WHERE...San Diego County.")
        self.assertEqual(mark, sdwx.ALERT_BULLETS["WHERE"][0])
        self.assertEqual(text, "San Diego County.")

    def test_impacts_is_the_loud_one(self):
        self.assertEqual(sdwx.ALERT_BULLETS["IMPACTS"][1], "severe")
        for label in ("WHAT", "WHERE", "WHEN"):
            self.assertEqual(sdwx.ALERT_BULLETS[label][1], "accent")

    def test_glyph_is_colored_when_color_is_on(self):
        sdwx.COLOR = True
        mark, _ = sdwx.alert_bullet("* IMPACTS...Dangerous surf.")
        self.assertTrue(mark.startswith("\x1b["))
        self.assertEqual(sdwx.visible_len(mark), 1)

    def test_unknown_label_keeps_its_word(self):
        """A field NWS adds later must not vanish into a generic glyph."""
        mark, text = sdwx.alert_bullet("* SNOW LEVEL...6000 feet.")
        self.assertEqual(mark, sdwx.UNKNOWN_BULLET[0])
        self.assertIn("Snow Level", text)
        self.assertIn("6000 feet", text)

    def test_plain_prose_is_not_a_bullet(self):
        mark, text = sdwx.alert_bullet("Remain out of the water.")
        self.assertIsNone(mark)
        self.assertEqual(text, "Remain out of the water.")

    def test_every_label_found_in_the_wild_is_mapped(self):
        """Labels observed across 339 live NWS alerts, July 2026."""
        for label in ("WHAT", "WHERE", "WHEN", "IMPACTS", "ADDITIONAL DETAILS",
                      "AFFECTED AREA", "WINDS", "RELATIVE HUMIDITY"):
            self.assertIn(label, sdwx.ALERT_BULLETS)

    def test_every_glyph_is_one_cell(self):
        for glyph, _ in list(sdwx.ALERT_BULLETS.values()) + [sdwx.UNKNOWN_BULLET]:
            self.assertEqual(len(glyph), 1, repr(glyph))

    def test_render_ocean(self):
        marine = sdwx.src_marine(32.75, -117.25, _get=fake_getter(MARINE_FIX))
        buoy = {"wave_ft": 1.6, "dpd_s": 17.0, "dir": "WNW", "water_f": 71.6}
        tides = [{"t": "2026-07-03 17:37", "type": "L", "ft": 2.6},
                 {"t": "2026-07-03 23:36", "type": "H", "ft": 5.5}]
        data = {"marine_ob": marine, "buoy_ob": buoy,
                "marine_solana": marine, "buoy_solana": buoy}
        lines = sdwx.render_ocean(data, tides)
        joined = "\n".join(lines)
        self.assertIn("swell 2.5 ft @ 12 s SSW", joined)
        self.assertIn("17:37 L 2.6ft", joined)
        all_lines_fit(self, lines)

    def test_render_ocean_labels_both_spots(self):
        marine = sdwx.src_marine(32.75, -117.25, _get=fake_getter(MARINE_FIX))
        data = {"marine_ob": marine, "marine_solana": marine}
        joined = "\n".join(sdwx.render_ocean(data, None))
        for spot in sdwx.SPOTS:
            self.assertIn(spot["label"], joined)

    def test_render_ocean_one_spot_down(self):
        """A dead buoy must not take the other spot's rows with it."""
        marine = sdwx.src_marine(32.75, -117.25, _get=fake_getter(MARINE_FIX))
        data = {"marine_ob": marine, "buoy_ob": None,
                "marine_solana": None, "buoy_solana": None}
        joined = "\n".join(sdwx.render_ocean(data, None))
        self.assertIn("OB", joined)
        self.assertNotIn("data unavailable", joined)

    def test_render_ocean_partial(self):
        lines = sdwx.render_ocean({}, None)
        self.assertIn("data unavailable", lines[0])

    def test_render_sunmoon(self):
        fc = sdwx.src_forecast(_get=fake_getter(FORECAST_FIX))
        now = dt(2024, 4, 23, 23, 49, tzinfo=timezone.utc)
        lines = sdwx.render_sunmoon(fc, now)
        joined = "\n".join(lines)
        self.assertIn("sunrise 05:45", joined)
        self.assertIn("Full Moon", joined)
        self.assertIn("illuminated ↓", joined)
        all_lines_fit(self, lines)

    def test_render_sunmoon_waxing_arrow(self):
        fc = sdwx.src_forecast(_get=fake_getter(FORECAST_FIX))
        now = dt(2024, 4, 15, 19, 13, tzinfo=timezone.utc)
        joined = "\n".join(sdwx.render_sunmoon(fc, now))
        self.assertIn("illuminated ↑", joined)

    def test_kp_text(self):
        self.assertIn("quiet", sdwx.kp_text(2.0))
        self.assertIn("G1", sdwx.kp_text(5.3))
        self.assertIn("G2", sdwx.kp_text(6.3))

    def test_render_solar(self):
        scales = sdwx.src_scales(_get=fake_getter(SCALES_FIX))
        lines = sdwx.render_solar({"kp": 6.33}, scales)
        joined = "\n".join(lines)
        self.assertIn("Kp 6.3", joined)
        self.assertIn("G1 minor", joined)
        self.assertIn("outlook", joined)
        all_lines_fit(self, lines)


class TestOutput(unittest.TestCase):
    def args(self, no_color=False, no_pager=False):
        return SimpleNamespace(no_color=no_color, no_pager=no_pager)

    def test_resolve_output_matrix(self):
        cases = [
            # (args, isatty, env)          -> (color, pager)
            ((self.args(), True, {}), (True, True)),
            ((self.args(), False, {}), (False, False)),
            ((self.args(no_pager=True), True, {}), (True, False)),
            ((self.args(no_color=True), True, {}), (False, True)),
            ((self.args(), True, {"NO_COLOR": "1"}), (False, True)),
            ((self.args(no_color=True), False, {}), (False, False)),
        ]
        for inputs, want in cases:
            with self.subTest(inputs=inputs):
                color, pager = sdwx.resolve_output(*inputs)
                self.assertEqual((color, pager), want)

    def test_pager_defaults_to_less_with_flags(self):
        argv, _ = sdwx.pager_command({})
        self.assertEqual(argv, ["less", "-RFX"])

    def test_pager_keeps_user_less_flags(self):
        argv, _ = sdwx.pager_command({"PAGER": "less -S"})
        self.assertEqual(argv, ["less", "-S", "-RFX"])

    def test_pager_leaves_other_pagers_alone(self):
        argv, env = sdwx.pager_command({"PAGER": "bat -p"})
        self.assertEqual(argv, ["bat", "-p"])
        self.assertNotIn("LESSUTFCHARDEF", env)

    def test_pager_ignores_empty_pager(self):
        argv, _ = sdwx.pager_command({"PAGER": ""})
        self.assertEqual(argv, ["less", "-RFX"])

    def test_pager_declares_pua_printable_for_less(self):
        """Nerd Font glyphs are PUA; less escapes them to <U+E30D> otherwise."""
        _, env = sdwx.pager_command({})
        self.assertEqual(env["LESSUTFCHARDEF"], sdwx.PUA_PRINTABLE)
        self.assertIn("E000-F8FF:p", env["LESSUTFCHARDEF"])

    def test_pager_keeps_existing_chardef(self):
        _, env = sdwx.pager_command({"LESSUTFCHARDEF": "E000-E010:w"})
        self.assertEqual(env["LESSUTFCHARDEF"], "E000-E010:w")

    def test_pager_preserves_rest_of_env(self):
        _, env = sdwx.pager_command({"PATH": "/bin", "LESS": "-i"})
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["LESS"], "-i")

    def test_emit_without_pager_prints(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sdwx.emit("hello", False)
        self.assertEqual(buf.getvalue(), "hello\n")

    def test_emit_feeds_the_pager(self):
        seen = {}

        class FakeProc:
            def communicate(self, text):
                seen["text"] = text

        def fake_popen(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")
            return FakeProc()

        with mock.patch.object(sdwx.subprocess, "Popen", fake_popen):
            sdwx.emit("hello", True, env={})
        self.assertEqual(seen["argv"], ["less", "-RFX"])
        self.assertEqual(seen["env"]["LESSUTFCHARDEF"], sdwx.PUA_PRINTABLE)
        self.assertEqual(seen["text"], "hello\n")

    def test_emit_falls_back_when_pager_missing(self):
        def fake_popen(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        buf = io.StringIO()
        with mock.patch.object(sdwx.subprocess, "Popen", fake_popen):
            with contextlib.redirect_stdout(buf):
                sdwx.emit("hello", True, env={"PAGER": "nope"})
        self.assertEqual(buf.getvalue(), "hello\n")

    def test_emit_survives_reader_quitting_early(self):
        waited = []

        class FakeProc:
            def communicate(self, text):
                raise BrokenPipeError()

            def wait(self):
                waited.append(True)

        with mock.patch.object(sdwx.subprocess, "Popen",
                               lambda argv, **kw: FakeProc()):
            sdwx.emit("hello", True, env={})
        self.assertEqual(waited, [True])


def dead_data():
    return {key: None for key, _ in sdwx.SOURCES}


class TestGather(unittest.TestCase):
    def test_gather_runs_sources_concurrently(self):
        """Serial execution of N blocking sources would take N * delay."""
        sdwx.SOURCES = [(f"s{i}", lambda now: time.sleep(0.2) or i)
                        for i in range(8)]
        try:
            start = time.time()
            data, errors = sdwx.gather(dt(2026, 7, 3, 14, 0))
            elapsed = time.time() - start
        finally:
            sdwx.SOURCES = sdwx._sources()
        self.assertEqual(errors, {})
        self.assertEqual(len(data), 8)
        self.assertLess(elapsed, 0.8, "sources did not overlap")

    def test_gather_isolates_failures(self):
        def boom(now):
            raise ValueError("upstream is down")

        sdwx.SOURCES = [("good", lambda now: 1), ("bad", boom)]
        try:
            data, errors = sdwx.gather(dt(2026, 7, 3, 14, 0))
        finally:
            sdwx.SOURCES = sdwx._sources()
        self.assertEqual(data["good"], 1)
        self.assertIsNone(data["bad"])
        self.assertIsInstance(errors["bad"], ValueError)
        self.assertIn("upstream is down", str(errors["bad"]))

    def test_every_source_key_is_unique(self):
        keys = [k for k, _ in sdwx._sources()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_spot_sources_bind_their_own_spot(self):
        """A bare closure over the loop var would give every spot the last."""
        seen = []
        srcs = dict(sdwx._sources())
        for spot in sdwx.SPOTS:
            with mock.patch.object(sdwx, "src_ndbc",
                                   lambda buoy, **kw: seen.append(buoy)):
                srcs["buoy_" + spot["key"]](None)
        self.assertEqual(seen, [s["buoy"] for s in sdwx.SPOTS])


class TestMain(unittest.TestCase):
    def test_build_report_all_none_still_renders(self):
        sdwx.COLOR = False
        now = dt(2026, 7, 3, 14, 0, tzinfo=timezone.utc)
        lines = sdwx.build_report(dead_data(), now)
        joined = "\n".join(lines)
        self.assertIn("SAN DIEGO", joined)
        self.assertEqual(joined.count("data unavailable"), 7)
        all_lines_fit(self, lines)
        sdwx.COLOR = True

    def test_stale_note_only_when_stale(self):
        sdwx.COLOR = False
        try:
            self.assertEqual(sdwx.render_stale_note(0), [])
            self.assertIn("1 source served", sdwx.render_stale_note(1)[0])
            self.assertIn("3 sources served", sdwx.render_stale_note(3)[0])
        finally:
            sdwx.COLOR = True

    def test_build_report_surfaces_stale_note(self):
        sdwx.COLOR = False
        now = dt(2026, 7, 3, 14, 0, tzinfo=timezone.utc)
        joined = "\n".join(sdwx.build_report(dead_data(), now, stale_count=2))
        self.assertIn("stale cache", joined)
        sdwx.COLOR = True

    def test_all_failed_is_exit_1(self):
        data = dead_data()
        self.assertTrue(sdwx.all_failed(data))
        data["kp"] = {"kp": 1.0}
        self.assertFalse(sdwx.all_failed(data))


if __name__ == "__main__":
    unittest.main()
