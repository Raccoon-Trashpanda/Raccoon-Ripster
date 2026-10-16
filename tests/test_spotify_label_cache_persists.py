"""Подтверждённые лейблы Spotify не должны сгорать при перезапуске.

Сверка лейблов устроена так, что потолок `_SP_SINGLE_VERIFY_CAP` НЕ теряет
данные: «следующий обход досверяет следующую порцию, и через несколько проходов
подтверждено всё». Но кэш жил только в памяти процесса — каждый перезапуск app
обнулял накопленное, и радар заново тратил суточную квоту Spotify на те же
самые релизы.

04.09.2026 это и наблюдалось: у владельца `/v1` отвечал «блокировка на 17 ч
28 мин», при том что Deezer и Tidal в тот же момент отдавали дискографию
нормально. Чинить надо расход, а не блокировку.

Лейбл релиза не меняется, поэтому диск здесь уместен. Ключевая осторожность —
пустое значение НЕ сохраняется: пустое означает «подтвердить не удалось»
(403/429 в тот момент), и записать его на диск значило бы навсегда закрепить
неудачу проверки как факт о релизе.
"""
import json

import pytest

from ripster.routes import discovery as d


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(d, "_sp_label_cache", {}, raising=False)
    monkeypatch.setattr(d, "_sp_label_cache_loaded", False, raising=False)
    return tmp_path


def test_confirmed_labels_survive_a_restart(_sandbox):
    d._sp_label_cache["alb1"] = "XL Recordings"
    d._sp_label_cache_save()

    # «перезапуск»: память пуста, флаг загрузки сброшен
    d._sp_label_cache.clear()
    d._sp_label_cache_loaded = False
    d._sp_label_cache_load()
    assert d._sp_label_cache.get("alb1") == "XL Recordings"


def test_unconfirmed_is_never_written(_sandbox):
    """Пустое — это «не смогли проверить», а не «лейбла нет»."""
    d._sp_label_cache["alb1"] = "XL Recordings"
    d._sp_label_cache["alb2"] = ""
    d._sp_label_cache_save()
    on_disk = json.loads(d._sp_label_cache_path().read_text(encoding="utf-8"))
    assert "alb1" in on_disk
    assert "alb2" not in on_disk, "неудача проверки закреплена на диске как факт"


def test_loading_never_overwrites_a_fresher_value(_sandbox):
    d._sp_label_cache["alb1"] = "Старое"
    d._sp_label_cache_save()
    d._sp_label_cache.clear()
    d._sp_label_cache_loaded = False
    d._sp_label_cache["alb1"] = "Свежее, из этого прогона"
    d._sp_label_cache_load()
    assert d._sp_label_cache["alb1"] == "Свежее, из этого прогона"


def test_load_happens_once(_sandbox):
    d._sp_label_cache["alb1"] = "XL Recordings"
    d._sp_label_cache_save()
    d._sp_label_cache.clear()
    d._sp_label_cache_loaded = False
    d._sp_label_cache_load()
    d._sp_label_cache.pop("alb1")     # как будто значение вытеснили в этом прогоне
    d._sp_label_cache_load()          # повторный вызов не должен возвращать его
    assert "alb1" not in d._sp_label_cache


def test_missing_file_is_not_an_error(_sandbox):
    d._sp_label_cache_load()          # файла ещё нет
    assert d._sp_label_cache == {}


def test_broken_file_does_not_break_verification(_sandbox):
    d._sp_label_cache_path().write_text("{это не json", encoding="utf-8")
    d._sp_label_cache_load()          # не должно бросить
    assert d._sp_label_cache == {}


def test_empty_cache_writes_nothing(_sandbox):
    """Пустой файл на диске только путал бы: его быть не должно."""
    d._sp_label_cache_save()
    assert not d._sp_label_cache_path().exists()
