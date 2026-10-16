"""Гард CSRF не должен ломать нативные клиенты с bearer-токеном.

Поймано живьём 05.09.2026 на машине владельца (`remote-enabled: true`): ЛЮБОЙ
POST с телефона получал 403 «Cross-site request blocked» — сопряжение,
активность, смена режима. Мобильный Ripster нативный, Origin он не шлёт, а с
включённым удалённым доступом отсутствие Origin трактовалось как атака.

CSRF — это про то, что браузер САМ прикладывает куку к запросу с чужой
страницы. Заголовок Authorization браузер кросс-доменно поставить не даст,
поэтому запрос с bearer-токеном подделать так нельзя, и проверять Origin у него
незачем.
"""
from types import SimpleNamespace

from ripster import auth


def _req(method="POST", headers=None):
    return SimpleNamespace(
        method=method,
        headers={k.lower(): v for k, v in (headers or {}).items()},
        url=SimpleNamespace(path="/api/pair/touch"),
        cookies={},
    )


class TestBearerIsNotCsrfable:
    """`auth._config` — ОБЩЕЕ состояние модуля на весь прогон.

    Его нельзя ни чистить, ни менять по месту: в полном прогоне другой тест
    подставляет туда ConfigService, у которого нет ни `clear`, ни `update`, и
    первые две попытки изоляции разваливались именно об это. Подменяем САМО
    ИМЯ на время теста и возвращаем обратно — тест не должен чинить одно,
    ломая соседа.
    """

    def setup_method(self):
        self._saved = auth._config
        auth._config = {"remote-enabled": True}

    def teardown_method(self):
        auth._config = self._saved

    def test_a_paired_phone_post_is_allowed(self):
        assert auth._csrf_check(_req(headers={"Authorization": "Bearer abc123"})) is True

    def test_case_of_the_scheme_does_not_matter(self):
        assert auth._csrf_check(_req(headers={"Authorization": "bearer abc123"})) is True

    def test_without_a_token_the_guard_still_bites(self):
        """Ради этого гард и стоит: голый POST без Origin при открытом наружу
        доступе — это и есть тот случай, от которого он защищает."""
        assert auth._csrf_check(_req()) is False

    def test_a_cookie_post_from_another_site_is_still_blocked(self):
        assert auth._csrf_check(_req(headers={
            "Origin": "https://evil.example", "Host": "127.0.0.1:7799",
        })) is False

    def test_a_basic_auth_header_is_not_a_bearer(self):
        """Проверяем именно bearer: другие схемы под это исключение не
        подпадают, иначе оно превратится в дыру."""
        assert auth._csrf_check(_req(headers={"Authorization": "Basic dXNlcjpwYXNz"})) is False

    def test_get_was_never_checked(self):
        assert auth._csrf_check(_req(method="GET")) is True


class TestLocalBoxUnchanged:
    def test_without_remote_access_a_plain_post_is_fine(self):
        """На локальной машине без удалённого доступа поведение прежнее."""
        saved = auth._config
        auth._config = {}
        try:
            assert auth._csrf_check(_req()) is True
        finally:
            auth._config = saved
