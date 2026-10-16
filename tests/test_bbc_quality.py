"""ripster/bbc_quality.py — единственное место, где у BBC спрашивают числа.

Здесь держатся три обещания, на которых стоит вся честность BBC-записи:

* качество берут ИЗ плейлиста и ИЗ файла, а не из поля «bitrate» у MediaSelector
  (оно для 51 кбит/с умеет писать 320) и не из ярлыка задачи;
* MP3-цель никогда не выше измеренного источника — иначе это апконверт;
* приговор файлу ставится по факту, и тест честно показывает, где факта не
  хватает: раздутый до 320 lossy-файл по одному битрейту неотличим от честных
  320, поэтому gate стоит ДО кодирования.

ffmpeg/ffprobe нужны двум тестам в конце — они измеряют настоящие файлы. Если
их нет, тест НЕ проходит молча: отдельный тест-шлюз краснеет и объясняет, что
проверка качества сегодня не выполнялась вовсе (см. `_require_ffmpeg`).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ripster import bbc_quality as Q
from ripster.ffmpeg_path import ensure_ffmpeg_on_path

# НЕ полагаемся на PATH того shell, откуда позвали pytest: он зависит от того,
# КТО поднял процесс (PowerShell даёт ffmpeg, bash/nohup — теряет; та же ловушка,
# из-за которой у проекта плавали «ffmpeg не найден» в спектрограммах). Резолвим
# тем же способом, что и продукт, — иначе quality-тесты молча пропускались бы
# ровно в тех запусках, где им и следовало ловить регрессии.
# 21.09.2026 так и было: оба измерительных теста стояли в skipped, а suite
# выглядел зелёным.
_FFMPEG_DIR = ensure_ffmpeg_on_path()
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

_SKIP_REASON = "ffmpeg/ffprobe не найдены — см. test_ffmpeg_tooling_is_actually_being_used"

_MISSING = (
    "ffmpeg/ffprobe НЕ ВИДНЫ процессу теста — проверка качества записи сегодня "
    "не выполнялась ни на одном файле.\n"
    f"  искали: PATH (дополнен {_FFMPEG_DIR or 'ничем'}), "
    "config: gamdl-ffmpeg-path, WinGet/choco/scoop/C:\\ffmpeg, tools/ffmpeg\n"
    "  это НЕ пропуск 'как обычно': без этих двух тестов приговор файлу "
    "(as_promised/below) никем не проверен.\n"
    "  положите ffmpeg+ffprobe в PATH либо скажите явно, что на этой машине их "
    "нет: BBC_NO_FFMPEG_OK=1."
)


def _require_ffmpeg():
    """Отсутствие инструментов — красное сообщение, а не зелёный пропуск."""
    if HAVE_FFMPEG:
        return
    if os.environ.get("BBC_NO_FFMPEG_OK") == "1":
        pytest.skip("ffmpeg/ffprobe отсутствуют по явному указанию (BBC_NO_FFMPEG_OK=1)")
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    pytest.fail(_MISSING, pytrace=False)


def test_ffmpeg_tooling_is_actually_being_used():
    """Шлюз: молчаливый skip дороже падения. Пока ffmpeg не найден, измерительные
    тесты формально «skipped», и отличить это от «проверено» может только этот
    тест — поэтому он не пропускается, а валится."""
    _require_ffmpeg()
    assert shutil.which("ffmpeg") and shutil.which("ffprobe")

# Настоящий мастер live-канала (Radio 1 / 6 Music / Radio 3, промер 21.09.2026
# 19:13–19:33Z): две ступени HE-AAC снизу и две AAC-LC сверху, 320 — есть.
LIVE_MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=74000,AVERAGE-BANDWIDTH=48000,CODECS="mp4a.40.5"
https://example.invalid/audio/-audio%3d48000.norewind.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=148000,AVERAGE-BANDWIDTH=96000,CODECS="mp4a.40.5"
https://example.invalid/audio/-audio%3d96000.norewind.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=128000,AVERAGE-BANDWIDTH=128000,CODECS="mp4a.40.2"
https://example.invalid/audio/-audio%3d128000.norewind.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=320000,AVERAGE-BANDWIDTH=320000,CODECS="mp4a.40.2"
https://example.invalid/audio/-audio%3d320000.norewind.m3u8
"""

# On-demand выпуск (Essential Mix, vpid m00315gp, промер 21.09.2026): MediaSelector
# при этом отвечает "bitrate": "320". В лестнице 320 нет — никогда.
ONDEMAND_MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=64000,AVERAGE-BANDWIDTH=51200,CODECS="mp4a.40.5"
https://example.invalid/low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=128000,AVERAGE-BANDWIDTH=102400,CODECS="mp4a.40.5"
https://example.invalid/high.m3u8
"""


def _rungs(rows):
    """Пары (kbps, codec) — и по списку вариантов, и по summary."""
    if isinstance(rows, dict):
        rows = rows["variants"]
    return [(v["kbps"], v["codec"]) for v in rows]


# ── Плейлист: что поток предлагает прямо сейчас ───────────────────────────────

def test_parse_variants_reads_band_and_profile():
    v = Q.parse_variants(LIVE_MASTER)
    assert _rungs(v) == [(48, "HE-AAC"), (96, "HE-AAC"), (128, "AAC-LC"), (320, "AAC-LC")]
    assert [x["lc"] for x in v] == [False, False, True, True]
    # kbps берём из имени варианта, а не из пикового BANDWIDTH: у 48k-ступени
    # пик 74000 — по нему «320» нашёлся бы даже там, где его нет.
    assert v[0]["kbps"] == 48 and v[0]["bandwidth"] == 74000


def test_live_ladder_promises_320_lc():
    lad = Q.ladder_summary(Q.parse_variants(LIVE_MASTER), checked_utc="2026-09-21T19:13:00Z")
    assert lad["ok"] is True and lad["has_320_lc"] is True
    assert (lad["best_kbps"], lad["best_codec"], lad["lc"]) == (320, "AAC-LC", True)


def test_ondemand_ladder_has_no_320_even_though_api_says_so():
    """Главный дефект, из-за которого всё это написано: ярлык «320» в ответе
    MediaSelector при лестнице 51/102 HE-AAC."""
    lad = Q.ladder_summary(Q.parse_variants(ONDEMAND_MASTER))
    assert lad["ok"] is True
    assert lad["has_320_lc"] is False
    assert (lad["best_kbps"], lad["best_codec"]) == (102, "HE-AAC")


def test_320_he_aac_is_not_the_honest_320():
    """Профиль решает: HE-AAC на 320 кбит/с — полоса сверху пустая."""
    variants = Q.parse_variants(
        '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=320000,CODECS="mp4a.40.5"\n'
        "https://example.invalid/audio/-audio%3d320000.m3u8\n")
    assert _rungs(variants) == [(320, "HE-AAC")]
    assert Q.offers(variants, 320) is False


def test_ladder_summary_carries_no_urls():
    """Через API прогноза наружу уходят только числа: on-demand-ссылки подписаны
    и короткоживущи, печатать их — раздавать чужие токены."""
    lad = Q.ladder_summary(Q.parse_variants(LIVE_MASTER))
    assert "http" not in json.dumps(lad)


def test_unknown_codec_is_reported_as_is():
    assert Q.codec_label("mp4a.40.2") == "AAC-LC"
    assert Q.codec_label("mp4a.40.5") == "HE-AAC"
    assert Q.codec_label("vorbis") == "vorbis"        # не выдумываем «AAC»
    assert Q.codec_label("") == ""
    # Написанное с нулём «mp4a.40.02» — тот же профиль, но узнавать его на глаз
    # мы не можем: пусть остаётся не-LC, чем притворяется полным звуком.
    padded = Q.parse_variants('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=320000,'
                              'CODECS="mp4a.40.02"\nhttps://example.invalid/x.m3u8\n')
    assert padded[0]["lc"] is False and padded[0]["codec"] == "mp4a.40.02"


# ── best_live_variant: движок обязан писать с той ступени, что есть ───────────

class _Resp:
    def __init__(self, text, code=200):
        self.text, self.status_code = text, code


class _Client:
    def __init__(self, text, code=200):
        self._r, self.calls = _Resp(text, code), []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        self.calls.append(url)
        return self._r


@pytest.fixture
def fake_http(monkeypatch):
    """Подменить общий синхронный клиент у bbc_quality (он же у движка)."""
    from ripster import http_client

    def use(text, code=200):
        client = _Client(text, code)
        monkeypatch.setattr(http_client, "shared", lambda *a, **k: client)
        return client

    return use


def test_best_live_variant_picks_the_wanted_rung(fake_http):
    client = fake_http(LIVE_MASTER)
    pick = Q.best_live_variant("bbc_radio_one", 320)
    assert (pick["kbps"], pick["codec"], pick["lc"]) == (320, "AAC-LC", True)
    assert pick["url"].endswith("-audio%3d320000.norewind.m3u8")
    assert client.calls and client.calls[0].endswith("/master.m3u8")
    assert pick["ladder"]["has_320_lc"] is True


def test_best_live_variant_falls_back_to_the_best_lc_and_says_the_number(fake_http):
    """Ступени 320 нет — пишем лучшее, что есть, и называем числом: сорвать окно
    эфира ошибкой «320 нет» хуже, чем снять 128 честно."""
    ladder = LIVE_MASTER.replace(
        "#EXT-X-STREAM-INF:BANDWIDTH=320000,AVERAGE-BANDWIDTH=320000,CODECS=\"mp4a.40.2\"\n"
        "https://example.invalid/audio/-audio%3d320000.norewind.m3u8\n", "")
    fake_http(ladder)
    pick = Q.best_live_variant("bbc_6music", 320)
    assert (pick["kbps"], pick["lc"]) == (128, True)
    assert pick["ladder"]["has_320_lc"] is False
    assert not pick.get("error")


def test_best_live_variant_never_rises_for_the_sake_of_the_label(fake_http):
    """LC-ступеней нет вовсе — берём лучший вариант как есть (HE-AAC 102), а не
    «дотягиваем до 320»: число на карточке обязано быть числом из плейлиста."""
    fake_http(ONDEMAND_MASTER)
    pick = Q.best_live_variant("bbc_world_service", 320)
    assert (pick["kbps"], pick["lc"]) == (102, False)
    assert pick["ladder"]["has_320_lc"] is False


def test_best_live_variant_dead_master_still_returns_an_url(fake_http):
    """Не смогли прочитать лестницу — не валим запись: отдаём рабочий адрес
    движка и причину. Причина числом/кодом, не текстом для человека."""
    fake_http("", 403)
    pick = Q.best_live_variant("bbc_radio_one", 320)
    assert pick["error"] == "http_403"
    assert pick["url"] and "master.m3u8" not in pick["url"]


def test_best_live_variant_unknown_channel():
    assert Q.best_live_variant("rtesradio", 320) == {"url": "", "error": "unknown_channel"}


# ── MP3-цель: не выше источника ───────────────────────────────────────────────

@pytest.mark.parametrize("src", [0, 32, 48, 51, 56, 64, 80, 96, 97, 102, 112, 128,
                                 160, 192, 224, 256, 320, 500])
def test_mp3_target_never_exceeds_measured_source(src):
    t = Q.mp3_target_kbps(src)
    assert t in Q._CBR_STEPS
    assert Q.is_upconvert(src, t) is False
    if src:
        assert t <= src, f"источник {src} → цель {t}: это апконверт"


def test_mp3_target_ceiling_is_the_ondemand_one_not_320():
    """Лестницу прочитать не удалось (0) — целимся в потолок on-demand, а не в
    320: хуже один раз недооценить, чем снова называть 320 всё на свете."""
    assert Q.mp3_target_kbps(0) == 96 <= Q.ONDEMAND_CEILING
    assert Q.mp3_target_kbps(Q.ONDEMAND_CEILING) == 96
    # 112 из 102 — та ошибка, на которой этот тест и родился
    assert Q.mp3_target_kbps(102) != 112


def test_engine_target_for_the_measured_ondemand_ladder():
    """Сквозная проверка числа, с которым движок выходит в ffmpeg: лестница
    Essential Mix (51/102 HE-AAC) → цель 96K, не 112K и не 320K."""
    lad = Q.ladder_summary(Q.parse_variants(ONDEMAND_MASTER))
    target = Q.mp3_target_kbps(lad["best_kbps"])
    assert (lad["best_kbps"], target) == (102, 96)
    assert Q.is_upconvert(lad["best_kbps"], target) is False


# ── Приговор файлу ────────────────────────────────────────────────────────────

def test_verdict_without_a_file_is_unmeasured_not_ok(tmp_path):
    v = Q.verdict(tmp_path / "нет-такого.m4a", 320)
    assert v["state"] == "unmeasured"
    assert v["measured"] == {} and v["promised_kbps"] == 320


def test_verdict_trusts_the_file_not_the_label(monkeypatch):
    """Ярлык «live320» и папка «320» ничего не стоят: спрашиваем ffprobe."""
    monkeypatch.setattr(Q, "measure_file", lambda p: {
        "codec": "aac", "profile": "HE-AAC", "avg_kbps": 97, "kbps": 96,
        "sample_rate": 48000, "duration": 3600.0, "size": 43_000_000})
    v = Q.verdict(Path("whatever.m4a"), 320)
    assert v["state"] == "below" and v["kbps"] == 97 and v["ratio"] == 0.3


def test_profile_matters_as_much_as_the_number(monkeypatch):
    """320 кбит/с HE-AAC против обещанных 320 — не «как обещано»."""
    monkeypatch.setattr(Q, "measure_file", lambda p: {
        "codec": "aac", "profile": "HE-AAC", "avg_kbps": 320, "kbps": 320})
    assert Q.verdict(Path("x.m4a"), 320)["state"] == "below"
    monkeypatch.setattr(Q, "measure_file", lambda p: {
        "codec": "aac", "profile": "AAC-LC", "avg_kbps": 320, "kbps": 320})
    assert Q.verdict(Path("x.m4a"), 320)["state"] == "as_promised"
    # ±10% — нормальный разброс обёртки контейнера: 290 из 320 ещё «как обещано»
    monkeypatch.setattr(Q, "measure_file", lambda p: {
        "codec": "aac", "profile": "AAC-LC", "avg_kbps": 290, "kbps": 290})
    assert Q.verdict(Path("x.m4a"), 320)["state"] == "as_promised"


def test_low_promises_do_not_demand_lc(monkeypatch):
    """Для on-demand-цели (96 из 102) HE-AAC — нормальный профиль, не провал."""
    monkeypatch.setattr(Q, "measure_file", lambda p: {
        "codec": "aac", "profile": "HE-AAC", "avg_kbps": 97, "kbps": 96})
    assert Q.verdict(Path("x.mp3"), 96)["state"] == "as_promised"


def test_unreadable_probe_is_not_silently_a_pass(monkeypatch):
    monkeypatch.setattr(Q, "measure_file", lambda p: {})
    assert Q.verdict(Path("x.m4a"), 320)["state"] == "unmeasured"


# ── Настоящие измерения (ffmpeg) ──────────────────────────────────────────────

def _encode(tmp_path, name, *args):
    out = tmp_path / name
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, str(out)]
    r = subprocess.run(cmd, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-300:]
    return out


# Два независимых источника белого шума, склеенных в stereo. Именно НЕ «-ac 2»
# от одного канала: L=R кодек складывает в mid/side и боковому сигналу не чего
# нести, — промер 21.09.2026: синус, продублированный в stereo, даёт 225 кбит/с
# вместо 320.
NOISE_L = "anoisesrc=color=white:amplitude=0.9:duration=6:sample_rate=48000"
NOISE_R = "anoisesrc=color=white:amplitude=0.9:duration=6:sample_rate=48000:seed=7"
STEREO_NOISE = ("-f", "lavfi", "-i", NOISE_L,
                "-f", "lavfi", "-i", NOISE_R,
                "-filter_complex", "[0:a][1:a]amerge=inputs=2,pan=stereo|c0=c0|c1=c1[a]",
                "-map", "[a]")
MONO_SINE = ("-f", "lavfi", "-i", "sine=frequency=1000:duration=6")


@pytest.mark.skipif(not HAVE_FFMPEG, reason=_SKIP_REASON)
def test_measured_file_gets_the_verdict_it_earned(tmp_path):
    """Два файла, один и тот же шум, разные ступени: приговор обязан развести их
    по числам, а не по имени папки.

    ПОЧЕМУ ЗДЕСЬ ШУМ В STEREO, А НЕ СИНУСОИДА. Раньше fixture просил
    ``-c:a aac -b:a 320k`` для моно-синуса 1 кГц и ожидал «as_promised». Он
    падал честно: файл и вправду выходит 145 кбит/с (промер 21.09.2026), потому
    что у нативного AAC-кодировщика ffmpeg `-b:a` — это ПОТОЛОК, а не обещание:
    биты тратятся только там, где есть что кодировать. У чистой синусоиды
    спектр — одна линия, тратить 320 кбит/с не на что; у моно ещё и второй
    канал отсутствует, так что даже шум до 320 не дотягивает
    (замерено: mono sine 148, stereo sine 225, mono noise 221 — все «below»).
    Стирать это ослаблением проверки нельзя: тогда и продукт называл бы 148
    тристами двадцатью.
    """
    _require_ffmpeg()
    honest = _encode(tmp_path, "lc320.m4a", *STEREO_NOISE, "-c:a", "aac", "-b:a", "320k")
    lean = _encode(tmp_path, "lc96.m4a", *STEREO_NOISE, "-c:a", "aac", "-b:a", "96k")
    m = Q.measure_file(honest)
    assert m["profile"] == "AAC-LC" and m["channels"] == 2 and m["sample_rate"] == 48000
    v320 = Q.verdict(honest, 320)
    assert v320["state"] == "as_promised" and v320["kbps"] >= 288, v320
    vlow = Q.verdict(lean, 320)
    assert vlow["state"] == "below" and vlow["kbps"] < 200, vlow
    # и тем же числом — «как обещано», если обещали честно
    assert Q.verdict(lean, 96)["state"] == "as_promised"


@pytest.mark.skipif(not HAVE_FFMPEG, reason=_SKIP_REASON)
def test_a_thin_fixture_fails_instead_of_passing_quietly(tmp_path):
    """Сам этот тест — страховка от «починки» предыдущего в обратную сторону.

    Моно-синус на 320k — ровно тот fixture, на котором всё и сломалось. Он НЕ
    дотягивает до обещания, и вердикт обязан это сказать: если someday кто-то
    решит, что «verdict слишком строгий», и ослабит проверку, здесь он увидит
    измеренное число, а не зелёный свет.
    """
    _require_ffmpeg()
    thin = _encode(tmp_path, "sine320.m4a", *MONO_SINE, "-c:a", "aac", "-b:a", "320k")
    v = Q.verdict(thin, 320)
    assert v["state"] == "below" and v["measured"]["profile"] == "AAC-LC"
    assert v["kbps"] < 288, (f"синус дотянул до {v['kbps']} кбит/с — тогда этот тест "
                             f"больше не про уязвимость fixture, а предыдущий надо "
                             f"перепроверять руками")


@pytest.mark.skipif(not HAVE_FFMPEG, reason=_SKIP_REASON)
def test_upconvert_is_invisible_to_the_bitrate_check(tmp_path):
    """Честный предел файловой проверки: lossy-файл, раздутый до 320, по
    битрейту неотличим от настоящих 320 (промер 21.09.2026 на живом объекте —
    «as_promised 320» у файла, приехавшего из 102 кбит/с). Значит gate обязан
    стоять ДО кодирования — в лестнице, а не в вердикте."""
    _require_ffmpeg()
    if "libmp3lame" not in subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True
    ).stdout.decode(errors="replace"):
        # Это не «ffmpeg нет»: кодировщика MP3 нет — проверка апконверта сегодня
        # не запускалась. Громко, но не красным: MP3-кодек продукту не нужен.
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(errors="replace")
            except Exception:
                pass
        print("⚠ test_upconvert_is_invisible_to_the_bitrate_check: в этом ffmpeg нет "
              "libmp3lame — проверка апконверта НЕ выполнена", file=sys.stderr, flush=True)
        pytest.skip("нет libmp3lame — апконверт сегодня не проверялся")
    thin = _encode(tmp_path, "thin.m4a", *STEREO_NOISE, "-c:a", "aac", "-b:a", "96k")
    padded = _encode(tmp_path, "padded.mp3", "-i", str(thin),
                     "-c:a", "libmp3lame", "-b:a", "320k")
    assert Q.verdict(padded, 320)["state"] == "as_promised"   # вот так выглядит
    # ложная победа: файл действительно несёт 320 бит на секунду, и ни одного
    # лишнего герца в нём нет. Отсюда и gate до кодирования, а не после.
    assert Q.is_upconvert(Q.measure_file(thin)["avg_kbps"], 320) is True
