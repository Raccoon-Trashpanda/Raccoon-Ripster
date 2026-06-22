"""Amazon Music (amz CLI) and Yandex Music (ymd) engines — quality lists, log
classification, and `is_finished` auth/availability paths."""
import pytest

from ripster.engines.amazon import AmazonEngine
from ripster.engines.yandex import YandexEngine, _ym_cover


# ════════════════════════════ Amazon ════════════════════════════
@pytest.fixture
def az():
    return AmazonEngine()


def test_amazon_qualities(az):
    qs = az.qualities()
    assert len(qs) == 4
    assert all(q["engine"] == az.name for q in qs)
    assert {"Max", "Master", "High", "Atmos_EC-3"} == {q["id"] for q in qs}


@pytest.mark.parametrize("line,expected", [
    ("an error occurred", "error"),
    ("token expired", "error"),
    ("downloaded track 1", "success"),
    ("neutral", "stdout"),
])
def test_amazon_classify(az, line, expected):
    assert az.classify_line(line) == expected


@pytest.mark.parametrize("line,expected", [
    ("50%", (50, 100)),
    ("3/12", (3, 12)),
    ("nothing", (1, 2)),
])
def test_amazon_parse_progress(az, line, expected):
    assert az.parse_progress(line, 1, 2) == expected


def test_amazon_finished_auth(az):
    r = az.is_finished("token expired")
    assert r.success is False and "токен" in r.error.lower()


def test_amazon_finished_success(az):
    r = az.is_finished("downloaded\ncompleted", rc=0)
    assert r.success is True and r.tracks_ok == 2


def test_amazon_finished_error_exit(az):
    r = az.is_finished("some failure on line", rc=1)
    assert r.success is False


# ════════════════════════════ Yandex ════════════════════════════
@pytest.fixture
def ya():
    return YandexEngine()


def test_ym_cover():
    assert _ym_cover("avatars.yandex/x%%.jpg") == "https://avatars.yandex/x600x600.jpg"
    assert _ym_cover("http://x%%.jpg", "300x300") == "http://x300x300.jpg"
    assert _ym_cover("") == ""


def test_yandex_qualities(ya):
    qs = ya.qualities()
    assert len(qs) == 3
    assert all(q["engine"] == ya.name for q in qs)
    assert {"flac", "aac_192", "aac_64"} == {q["id"] for q in qs}


@pytest.mark.parametrize("line,expected", [
    ("трек не доступен для скачивания", "error"),
    ("Traceback (most recent call last)", "error"),
    ("Загружается трек", "info"),
    ("neutral", "stdout"),
])
def test_yandex_classify(ya, line, expected):
    assert ya.classify_line(line) == expected


def test_yandex_parse_progress(ya):
    assert ya.parse_progress("[3/12]", 0, 0) == (3, 12)
    assert ya.parse_progress("nothing", 2, 5) == (2, 5)


def test_yandex_finished_token_fail(ya):
    r = ya.is_finished("unauthorized", rc=1)
    assert r.success is False and "токен" in r.error.lower()


def test_yandex_finished_bad_url(ya):
    r = ya.is_finished("ссылка в неверном формате", rc=1)
    assert r.success is False and "формат" in r.error.lower()


def test_yandex_finished_success(ya):
    r = ya.is_finished("Загружается A\nЗагружается B", rc=0)
    assert r.success is True and r.tracks_ok == 2


def test_yandex_finished_unavailable(ya):
    r = ya.is_finished("трек не доступен для скачивания", rc=0)
    assert r.success is False and "недоступн" in r.error.lower()


def test_yandex_finished_nothing(ya):
    r = ya.is_finished("", rc=0)
    assert r.success is False
