"""Две разные учётки не должны делить один счётчик неудач.

04.09.2026, найдено на живых учётках владельца: два РАЗНЫХ 86-символьных токена
Qobuz оканчиваются на `0l7E4g`. Сторож опознавал учётку по хвосту секрета, и в
state-файле у них был общий ключ — вторая учётка отчиталась «2/3 неудачных
проверок подряд» на своей ПЕРВОЙ проверке.

Сбитый счёт — меньшая беда. Хуже обратный случай: если из пары с общим хвостом
одна живая, её успешная проверка обнуляет streak — и мёртвая не доберётся до
порога НИКОГДА. То есть авто-снятие тихо перестаёт работать, продолжая выглядеть
исправным. Класс «мёртвых детекторов»: чинить надо диагностику, а не подсистему.
"""
import json

import pytest

from ripster import credential_health as ch


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    return tmp_path


# Хвост общий, сами секреты разные — ровно случай из конфига владельца.
A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0l7E4g"
B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0l7E4g"


def test_shared_tail_gets_separate_identities():
    assert ch._mask(A) == ch._mask(B)          # хвост совпадает — так и есть
    assert ch._ident(A) != ch._ident(B)        # а опознание обязано различать


def test_identity_keeps_the_tail_readable_and_hides_the_secret():
    ident = ch._ident(A)
    assert ident.startswith("...0l7E4g#")
    assert A not in ident
    assert A[:20] not in ident


def test_a_live_twin_no_longer_resets_the_dead_ones_streak():
    """Главное следствие: без этого авто-снятие не срабатывало бы вовсе."""
    for _ in range(2):
        ch.record_check("qobuz_account", ch._ident(A), False, reason="401")
    ch.record_check("qobuz_account", ch._ident(B), True)      # живая соседка
    streak, archived = ch.record_check("qobuz_account", ch._ident(A), False, reason="401")
    assert (streak, archived) == (3, True), "живая учётка снова обнулила чужой счётчик"


def test_each_account_counts_only_its_own_failures():
    s1, _ = ch.record_check("qobuz_account", ch._ident(A), False, reason="401")
    s2, _ = ch.record_check("qobuz_account", ch._ident(B), False, reason="401")
    assert (s1, s2) == (1, 1)


def test_legacy_tail_only_entries_are_dropped(_sandbox):
    """Осиротевший счётчик хуже отсутствующего: он выглядит как знание."""
    ch._state_path().write_text(json.dumps({
        "qobuz_account:...0l7E4g": {"streak": 2, "last_country": ""},
        "deezer_arl:...947f15":    {"streak": 1, "last_country": ""},
        "apple_slot:rip-wrapper-3": {"streak": 0, "last_country": ""},
    }), encoding="utf-8")
    st = ch._load_state()
    assert "qobuz_account:...0l7E4g" not in st
    assert "deezer_arl:...947f15" not in st
    # Слоты Apple опознаются именем контейнера — их правило не касается.
    assert "apple_slot:rip-wrapper-3" in st


def test_a_fresh_account_starts_from_zero_after_the_migration(_sandbox):
    ch._state_path().write_text(json.dumps(
        {"qobuz_account:...0l7E4g": {"streak": 2, "last_country": ""}}), encoding="utf-8")
    streak, archived = ch.record_check("qobuz_account", ch._ident(A), False, reason="401")
    assert (streak, archived) == (1, False), "унаследован чужой счёт из старой записи"
