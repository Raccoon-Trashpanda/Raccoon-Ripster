"""Shared pytest setup: put the project root on sys.path so `import ripster`
and `import app` resolve regardless of where pytest is invoked from."""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _sidecar_isolation(monkeypatch, tmp_path):
    """Ни один тест не видит и не трогает ЖИВЫЕ сайдкар-файлы репозитория.

    Класс дефекта, пойманный 24.09.2026 дважды за вечер: `owner_feedback` без
    `configure()` пишет в `Path(".")` — то есть в рабочий каталог, а `notify`
    после `import app` (вотчлист зовёт `configure(ctx.base_dir)`) включал
    журнал/ограничитель на НАСТОЯЩЕМ `toast_ledger.json`. Тесты, писавшие
    вердикты и тосты туда, потом сами и падали: «не мой» с чужого фикстурами
    файла глушил карточку в `test_artist_identity`, а 17 тестовых тостов за
    окно включаляли suppression в `test_notify_contract`. Владелец в это время
    кликал «не то» по-настоящему — отличить было нельзя.

    Патчим только НЕ настроенные модули (дефолт = живые файлы): тесты, которые
    сами зовут `configure(tmp)`, перекрывают подмену и работают как раньше.
    """
    from ripster import notify as _n
    from ripster import owner_feedback as _of

    if _of._BASE is None:
        monkeypatch.setattr(_of, "_BASE", tmp_path)
    monkeypatch.setattr(_n, "_BASE_DIR", None)
    monkeypatch.setattr(_n, "_ENABLED", False)
    monkeypatch.setattr(_n, "_ledger", None)
