"""Beatport: флаг «не проверяй подписку» НЕ должен резать качество.

Живой разбор 05.09.2026. Владелец: «дали аккаунт, он якобы премиум, а тянет
почему-то не премиум нифига». Аккаунт и правда премиум — introspect отдаёт
`bp_link_pro_plus_2` (Professional+, верхний тариф). А на диске в папке
«FLAC CD» лежал `aac 133 kbps`.

Причина не в аккаунте и не в подписке. В orpheusdl-beatport
(`interface.py::valid_account`) ОДНА И ТА ЖЕ ветка делает два разных дела:
проверяет подписку И поднимает `quality_parse` с дефолтного "medium"
(AAC 128) до "high"/"lossless". Мы когда-то выключили её целиком, чтобы обойти
ложный отказ «нет подписки Link», — и вместе с отказом выключили апгрейд.
Ни одна проверка этого не поймала: скачивание шло «успешно», просто не в том
качестве. Мёртвый детектор наоборот — не гард, который не сработал, а гард,
который унёс с собой чужую работу.

Отсюда правило, которое тут и закрепляется: тумблер ставится по ФАКТУ
(узнали тариф — проверку включаем), а «не знаю» никогда не выдаётся за «нет».
"""
import json

import pytest

from ripster.engines import orpheus_beatport as bp


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Подсовываем движку свой settings.json и не пускаем его в сеть."""
    d = tmp_path / "orpheus" / "config"
    d.mkdir(parents=True)
    sp = d / "settings.json"
    sp.write_text(json.dumps({"global": {"general": {}}}), encoding="utf-8")
    monkeypatch.setattr(bp, "_settings_path", lambda: sp)
    return sp


def _written(sp) -> dict:
    return json.loads(sp.read_text(encoding="utf-8"))["global"]["advanced"]


class TestTheCheckIsDrivenByTheFact:
    def test_a_known_tier_leaves_the_check_on(self, settings, monkeypatch):
        """Тариф известен — проверку включаем: только внутри неё модуль
        поднимает качество до FLAC. Это и есть починка."""
        monkeypatch.setattr(bp, "_tier_now", lambda: "bp_link_pro_plus_2")
        note = bp._update_orpheus_settings("hifi", "", {})
        assert _written(settings)["disable_subscription_checks"] is False
        assert note == ""

    def test_an_unknown_tier_still_lets_the_download_run(self, settings, monkeypatch):
        """Ради чего флаг вводили: сеть отвалилась или токен протух — рвать
        закачку ложным «нет подписки» нельзя."""
        monkeypatch.setattr(bp, "_tier_now", lambda: "")
        note = bp._update_orpheus_settings("hifi", "", {})
        assert _written(settings)["disable_subscription_checks"] is True
        assert "128" in note, "молча отдать 128k — ровно тот баг, что чинили"

    def test_a_non_pro_tier_is_named_out_loud(self, settings, monkeypatch):
        """Essentials — законный ответ «нет прав на FLAC». Человек должен
        узнать это из текста, а не из размера файла."""
        monkeypatch.setattr(bp, "_tier_now", lambda: "bp_basic")
        note = bp._update_orpheus_settings("hifi", "", {})
        assert "bp_basic" in note and "Professional" in note

    def test_asking_for_128_on_a_basic_plan_is_not_a_complaint(self, settings, monkeypatch):
        """Тариф не тянет FLAC, но человек 128k и просил — предупреждать не о чем."""
        monkeypatch.setattr(bp, "_tier_now", lambda: "bp_basic")
        assert bp._update_orpheus_settings("minimum", "", {}) == ""


class TestUnknownIsNotNo:
    def test_a_network_failure_reads_as_unknown(self, monkeypatch):
        """Пустая строка от `_tier_now` обязана означать «не знаю».
        Если бы обрыв сети возвращал что-то похожее на тариф, мы бы включили
        проверку и получили ложный отказ вместо закачки."""
        monkeypatch.setattr(bp, "_read_bp_session", lambda: {"access_token": "x", "refresh_token": ""})

        class Boom(Exception):
            pass

        def _explode(*a, **k):
            raise Boom("сеть")

        import httpx
        monkeypatch.setattr(httpx, "get", _explode)
        assert bp._tier_now() == ""

    def test_no_session_at_all_is_unknown_too(self, monkeypatch):
        monkeypatch.setattr(bp, "_read_bp_session", lambda: None)
        assert bp._tier_now() == ""
