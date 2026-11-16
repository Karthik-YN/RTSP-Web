"""Stream-name, timestamp and segment validation (EC-02, EC-06)."""

import pytest

from app.config import Settings
from app.services.validation import (ValidationError, format_timestamp, parse_timestamp, validate_segment,
                                     validate_stream_name)

S = Settings(_env_file=None)


def name(n):
    return validate_stream_name(n, S.stream_name_pattern, S.reserved_names)


@pytest.mark.parametrize("ok", ["cam1", "Store_A-entrance", "a", "X" * 64, "2024-01-01_cam_03"])
def test_valid_names(ok):
    assert name(ok) == ok


@pytest.mark.parametrize("bad,fragment", [
    ("", "required"),
    ("my cam", "no spaces"),
    (" cam", "spaces"),
    ("cam/1", "slashes"),
    ("../etc", "slashes"),
    ("cam;rm -rf", "Invalid"),
    ("cam$(id)", "Invalid"),
    ("kamera_ö", "Invalid"),
    ("cam.1", "Invalid"),
    ("cam?x=1", "Invalid"),
    ("X" * 65, "64"),
    ("all", "reserved"),
    ("ALL_OTHERS", "reserved"),
    ("api", "reserved"),
])
def test_invalid_names(bad, fragment):
    with pytest.raises(ValidationError) as e:
        name(bad)
    assert fragment.lower() in str(e.value).lower()


@pytest.mark.parametrize("text,seconds", [
    ("0", 0.0), ("90", 90.0), ("12.5", 12.5), ("01:30", 90.0), ("1:02:03", 3723.0),
    ("01:02:03.250", 3723.25), ("00:00:00", 0.0), ("90:00", 5400.0), (" 00:01:00 ", 60.0),
    (125, 125.0), (0.5, 0.5), ("", None), (None, None),
])
def test_parse_timestamp(text, seconds):
    assert parse_timestamp(text) == seconds


@pytest.mark.parametrize("bad", ["-1", "1:60", "1:00:60", "1:61:00", "abc", "1::2", "1:2:3:4", "1e3",
                                 -5, float("nan"), float("inf")])
def test_parse_timestamp_invalid(bad):
    with pytest.raises(ValidationError):
        parse_timestamp(bad)


def test_format_timestamp_roundtrip():
    assert format_timestamp(3723.25) == "01:02:03.250"
    assert format_timestamp(0) == "00:00:00"
    assert parse_timestamp(format_timestamp(7322.5)) == 7322.5


class TestSegments:
    D = 600.0  # 10-minute file

    def seg(self, start, end, dur=D):
        return validate_segment(start, end, dur, 1.0)

    def test_whole_file(self):
        s = self.seg(None, None)
        assert (s.start_s, s.end_s, s.duration_s) == (0.0, None, self.D)

    def test_start_only(self):
        assert self.seg(120, None).duration_s == 480

    def test_start_and_end(self):
        s = self.seg(120, 300)
        assert (s.start_s, s.end_s, s.duration_s) == (120, 300, 180)

    def test_valid_boundaries(self):
        assert self.seg(0, self.D).duration_s == self.D
        assert self.seg(self.D - 1, None).duration_s == pytest.approx(1)
        assert self.seg(0, self.D + 0.0005).end_s == self.D  # float noise tolerated, clamped

    @pytest.mark.parametrize("start,end,msg", [
        (600, None, "beyond the end"),
        (700, None, "beyond the end"),
        (100, 50, "must be after"),
        (100, 100, "must be after"),
        (0, 601, "beyond the end"),
        (599.5, None, "minimum"),
        (10, 10.5, "minimum"),
        (-1, None, "negative"),
    ])
    def test_invalid(self, start, end, msg):
        with pytest.raises(ValidationError) as e:
            self.seg(start, end)
        assert msg in str(e.value)

    def test_unknown_duration(self):
        with pytest.raises(ValidationError):
            validate_segment(0, None, None, 1.0)


def test_settings_accept_csv_lists_from_env(monkeypatch, tmp_path):
    """Regression: the shipped .env.example uses comma-separated lists."""
    env = tmp_path / "x.env"
    env.write_text((__import__("pathlib").Path(__file__).resolve().parents[2] / ".env.example").read_text())
    monkeypatch.setenv("RTSPM_RESERVED_NAMES", "all,api")
    s = Settings(_env_file=str(env))
    assert s.copy_codecs == ["h264"]
    assert s.copy_profiles == ["Constrained Baseline", "Baseline", "Main", "High"]
    assert s.reserved_names == ["all", "api"]
    monkeypatch.setenv("RTSPM_COPY_CODECS", '["h264","hevc"]')  # JSON still works
    assert Settings(_env_file=None).copy_codecs == ["h264", "hevc"]
