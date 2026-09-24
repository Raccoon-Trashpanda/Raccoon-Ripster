# -*- coding: utf-8 -*-
"""Apple Wrapper Lite: тот же Go-загрузник, но ключи с локального lite-сервера.

Движок — опциональный (``apple-wrapper = lite``, по умолчанию выключен) и
устроен как надстройка над zhaarey: команда, разбор лога и качества общие,
различается только источник расшифровки — вместо Docker-враппера с TCP-
протоколом ours ``ripster/lite_shim.py`` (он же шифрованный кэш ключей
``ripster/lite.py``, он же Temari). Публичный wm.wol.moe к Lite не имеет
отношения и остаётся manual-only.
"""
from __future__ import annotations

from .registry import register
from .zhaarey import ZhaereyEngine
from ripster import lite, lite_shim


@register
class WrapperLiteEngine(ZhaereyEngine):
    name = "lite"

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        # Поднимаем оракул ДО запуска Go: падение здесь — честная ошибка
        # задачи («Lite-оракул не поднялся: …»), а не тихий уход на враппер.
        lite_shim.ensure(config)
        return super().build_cmd(url, quality, config)

    def working_dir(self) -> str | None:
        # config.yaml с портами оракула пишет ensure(); без него — обычный cwd.
        return lite_shim.cwd_dir()
