"""Живучесть аккаунтов/токенов: детектим смерть, архивируем, освобождаем ресурс.

Зачем (29.08.2026). У владельца много учёток на много сервисов (Apple wrapper-слоты,
Deezer ARL, Tidal/Qobuz токены, media-user-token) — какие-то дохнут (Apple блокирует
аккаунт, ARL протухает), и раньше это просто копилось «красным» в ежедневном отчёте,
ничего не освобождая и не запоминая. Живой пример, из-за которого модуль появился:
`rip-wrapper-3` месяцами зациклен на «Your account is disabled» и ни разу не поднял
порт — контейнер жив, слот занят, а толку ноль, и никто это не заметил, потому что
детектор страны (`apple_accounts.container_storefront`) корректно молчит про мёртвый
слот, а не превращает молчание в диагноз.

Что модуль делает:
1. Считает подряд идущие проверки "не жив" per credential (тот же паттерн, что
   `tools/ripster_healthcheck.py::_streak`, но живёт отдельно — здоровье учёток не то
   же самое, что здоровье инфраструктуры).
2. После N подряд (по умолчанию 3 прохода чекера, т.е. 1.5 дня при расписании 08:00/20:00)
   архивирует запись человекочитаемой строкой в DEAD_ACCOUNTS.txt В КОРНЕ ПРОЕКТА —
   дата, сервис, идентификатор, страна (если знаем), причина. Файл специально не JSON:
   владелец должен уметь открыть его блокнотом и скопировать значение руками, если
   аккаунт потом продлят и захотят вернуть.
3. Отключает credential от активной маршрутизации (конкретный disable_fn зависит от
   типа — для Apple wrapper-слота это docker stop, что заодно и освобождает порт).

Чего модуль НЕ делает: не поднимает новый слот на освободившееся место и не логинит
новый аккаунт автоматически. Каждый логин жжёт лимит устройств у Apple (см. скилл
ripster-apple-wrapper) — автоматически заводить сессии без присмотра это ровно тот
инцидент 2026-07-09, который уже один раз уронил пул. Освободившийся порт просто
перестаёт быть занят мёртвым контейнером — следующий реальный аккаунт владелец
подключает сам, и никакого конфликта портов не будет, потому что старый контейнер
уже остановлен и его порт-биндинг Docker снял сам.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

_CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def dry_run() -> bool:
    """Режим «только рассказать»: ставит healthcheck --no-fix."""
    return os.environ.get("RIPSTER_HEALTH_DRY_RUN") == "1"


DEFAULT_THRESHOLD = 3  # подряд идущих неудачных прохода чекера, не попыток внутри одного


def _base_dir() -> Path:
    return Path(os.environ.get("RIPSTER_BASE_DIR") or Path(__file__).resolve().parent.parent)


def _state_path() -> Path:
    p = _base_dir() / "dist" / "docker" / "credential_health_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _archive_path() -> Path:
    # В КОРНЕ проекта, не в dist/ и не в state — владелец должен найти файл не копаясь.
    return _base_dir() / "DEAD_ACCOUNTS.txt"


_IDENT_KINDS = ("deezer_arl", "qobuz_account")


def _load_state() -> dict:
    try:
        st = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(st, dict):
        return {}
    # Записи старого формата (только хвост секрета, без `#хеш`) больше не
    # опознаются и, что важнее, могли СКЛЕИВАТЬ разные учётки с общим хвостом —
    # см. `_ident`. Выбрасываем их: осиротевший счётчик хуже отсутствующего,
    # потому что выглядит как знание. Ключи Apple-слотов — имена контейнеров,
    # их правило не касается.
    return {k: v for k, v in st.items()
            if "#" in k or not k.startswith(_IDENT_KINDS)}


def _save_state(st: dict) -> None:
    try:
        _state_path().write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _append_archive(service: str, key: str, country: str, reason: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"[{ts}] {service} | {key} | страна: {country or '?'} | причина: {reason}\n"
    p = _archive_path()
    header_needed = not p.exists()
    try:
        with p.open("a", encoding="utf-8") as f:
            if header_needed:
                f.write(
                    "# Архив отключённых аккаунтов/токенов Ripster.\n"
                    "# Каждая строка — то, что автоматика сняла с маршрутизации после "
                    f"{DEFAULT_THRESHOLD} подряд неудачных проверок.\n"
                    "# Если аккаунт потом продлили/оплатили — данные не потеряны, можно "
                    "вернуть их в tokens/*.yaml или через бота вручную.\n\n"
                )
            f.write(line)
    except Exception:
        pass


def record_check(kind: str, key: str, alive: bool, *, country: str = "",
                  reason: str = "", threshold: int = DEFAULT_THRESHOLD,
                  disable_fn=None) -> tuple[int, bool]:
    """Отметить результат одной проверки credential'а.

    kind — тип ("apple_slot", "deezer_arl", "tidal_token", …), key — стабильный
    идентификатор внутри типа (имя контейнера, маскированный хвост ARL и т.п.).
    Возвращает (текущий streak неудач, был ли только что заархивирован и отключён).
    disable_fn(kind, key) — вызывается РОВНО ОДИН РАЗ при достижении порога; исключения
    из него не должны ронять сам чекер.
    """
    state_key = f"{kind}:{key}"
    st = _load_state()
    entry = st.get(state_key) or {"streak": 0, "last_country": ""}
    if dry_run():
        # Проверочный прогон (healthcheck --no-fix) не пишет streak и ничего не
        # снимает: 17.09.2026 такой прогон через две минуты после плановой
        # проверки досчитал учётку Qobuz до порога и снял её.
        return (0 if alive else int(entry.get("streak", 0)) + 1), False
    if alive:
        if entry["streak"]:
            entry["streak"] = 0
            st[state_key] = entry
            _save_state(st)
        return 0, False

    entry["streak"] = int(entry.get("streak", 0)) + 1
    if country:
        entry["last_country"] = country
    st[state_key] = entry
    _save_state(st)

    if entry["streak"] < threshold:
        return entry["streak"], False

    # Порог достигнут — архивируем и отключаем, затем сбрасываем streak, чтобы не
    # архивировать одну и ту же учётку заново на каждом следующем проходе чекера.
    _append_archive(kind, key, entry.get("last_country", ""), reason or "не отвечает")
    if disable_fn is not None:
        try:
            disable_fn(kind, key)
        except Exception:
            pass
    entry["streak"] = 0
    st[state_key] = entry
    _save_state(st)
    return threshold, True


def apple_slot_alive(container: str) -> tuple[bool, str]:
    """Жив ли Apple wrapper-слот. Возвращает (жив?, причина если нет)."""
    from . import apple_accounts as aa
    if not aa.container_running(container):
        return False, "контейнер остановлен"
    raw = aa._account_json(container)
    if not raw:
        # Контейнер жив, но 30020 не отвечает — либо ещё не поднялся, либо застрял
        # в цикле логина. Проверяем логи на явный сигнал блокировки аккаунта.
        try:
            logs = subprocess.run(
                ["docker", "logs", "--tail", "20", container],
                capture_output=True, text=True, timeout=8, creationflags=_CNW,
            ).stdout
        except Exception:
            logs = ""
        if "account has been disabled" in logs.lower() or "account is disabled" in logs.lower():
            return False, "Apple заблокировал аккаунт (account disabled)"
        if "login failed" in logs.lower():
            return False, "не может залогиниться (login failed)"
        return False, "порт 30020 не отвечает"
    # Контейнер жив и отдаёт токены — но это ещё не значит, что по ним что-то
    # скачается. Аккаунт с истёкшей подпиской Apple Music точно так же публикует
    # media-user-token, а на загрузке отдаёт 401 «Failed to rip song» / Invalid
    # CKC. Раньше такой слот проходил как «жив» и месяцами занимал место в пуле
    # (жалоба владельца 03.09.2026: «в эпле вставлено хуева гора токенов, а
    # ошибки сыплются всё равно»). Спрашиваем amp-api про подписку — тем же
    # запросом, что apple_router.local_wrapper_storefront.
    active, why = _apple_subscription_active(raw)
    if active is False:                 # именно явное False, не «не смог проверить»
        return False, why
    return True, ""


def _apple_subscription_active(raw: dict) -> "tuple[bool | None, str]":
    """(True | False | None, причина).

    None — не смогли проверить (нет токенов, сеть, любой не-200): трактуем как
    «жив», чтобы транзиентная ошибка amp-api не отправила в архив живой аккаунт.
    Хоронит только явный ответ ``subscription.active == false`` на 200 — и то не
    сразу, а после DEFAULT_THRESHOLD проходов подряд (см. record_check).
    """
    mut = str((raw or {}).get("music_token") or "")
    dev = str((raw or {}).get("dev_token") or "")
    if len(mut) < 50 or len(dev) < 50:
        return None, ""
    try:
        import httpx
        r = httpx.get("https://amp-api.music.apple.com/v1/me/account",
                      params={"meta": "subscription"},
                      headers={"Authorization": f"Bearer {dev}",
                               "Media-User-Token": mut,
                               "Origin": "https://music.apple.com"},
                      timeout=8.0)
        if r.status_code != 200:
            return None, ""
        sub = ((r.json() or {}).get("meta") or {}).get("subscription") or {}
    except Exception:
        return None, ""
    if not sub:
        return None, ""
    return (bool(sub.get("active")),
            "" if sub.get("active") else "подписка Apple Music неактивна")


def disable_apple_slot(kind: str, container: str) -> None:
    """Останавливает мёртвый wrapper-контейнер — освобождает порт, ничего не создаёт заново."""
    try:
        subprocess.run(["docker", "stop", container], capture_output=True,
                        timeout=20, creationflags=_CNW)
    except Exception:
        pass
    # Снять из мемо страны, иначе pick_slot_for продолжит считать слот существующим.
    try:
        from . import apple_accounts as aa
        memo = aa._memo_load()
        if container in memo:
            del memo[container]
            aa._memo_path().write_text(json.dumps(memo, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    except Exception:
        pass


def _mask(secret: str) -> str:
    """Хвост секрета для человекочитаемого архива/лога — не сам секрет."""
    s = (secret or "").strip()
    return f"...{s[-6:]}" if len(s) > 10 else "???"


def _ident(secret: str) -> str:
    """Опознание учётки в state-файле: хвост ПЛЮС короткий хеш.

    Одного хвоста мало. 04.09.2026 у владельца два разных 86-символьных токена
    Qobuz оканчивались на `0l7E4g` — и делили один счётчик неудач. Мелочь по
    сравнению со вторым следствием: будь одна из пары живой, её успешная
    проверка обнуляла бы streak мёртвой, и авто-снятие не сработало бы НИКОГДА.
    Это ровно тот класс дефекта, где чинить надо не подсистему, а её
    диагностику.

    Хеш короткий и односторонний: опознать запись можно, восстановить секрет —
    нет.
    """
    s = (secret or "").strip()
    if len(s) <= 10:
        return "???"
    return f"...{s[-6:]}#{hashlib.sha256(s.encode()).hexdigest()[:8]}"


def _config_paths():
    base = _base_dir()
    return base / "config.yaml", base / "tokens"


def _load_raw_config() -> dict:
    """Config.yaml СЛИТЫЙ с tokens/*.yaml — не голый yaml.safe_load(config.yaml).
    Реальный ARL/токен нередко живёт только в tokens/ (оверлей, приоритет над
    config.yaml — см. config_service.load_config), а не в самом config.yaml.
    Прочитать только config.yaml значит проверить не ту учётку, что реально
    используется, и рискнуть заархивировать рабочую по ложному сигналу."""
    from . import config_service as _cs
    cfg_path, tokens_dir = _config_paths()
    try:
        return _cs.load_config(cfg_path, tokens_dir)
    except Exception:
        return {}


def deezer_arl_alive(arl: str) -> tuple[bool, str, str]:
    """Жив ли Deezer ARL. Возвращает (жив?, причина если нет, страна если жив)."""
    import asyncio
    from . import deezer_accounts as da
    try:
        info = asyncio.run(da.arl_info(arl, fresh=True))
    except Exception as e:
        return False, f"ошибка проверки: {type(e).__name__}", ""
    if info.get("alive"):
        return True, "", info.get("country", "")
    return False, info.get("reason") or "не отвечает", ""


def _yaml_files_to_check() -> list:
    """config.yaml + каждый tokens/*.yaml — точечная правка идёт ТОЛЬКО в файл,
    где ключ реально лежит, а не в слитый снимок (см. _load_raw_config: снимок
    смешивает дефолты+config.yaml+tokens в одну кучу и не годится для записи —
    сохранить его обратно значило бы раздуть config.yaml всеми дефолтами и
    задублировать секреты из tokens/ в открытый файл)."""
    cfg_path, tokens_dir = _config_paths()
    paths = [cfg_path]
    if tokens_dir.is_dir():
        paths += sorted(tokens_dir.glob("*.yaml"))
    return paths


def disable_deezer_arl(primary: bool, arl: str) -> None:
    """Снимает мёртвый ARL — ищет его ТОЧНО в том файле (config.yaml или
    tokens/*.yaml), где он реально прописан, и правит только этот файл, не
    трогая остальные. Основной ключ очищается до "", пул-запись удаляется
    целиком. Ничего не логинит заново, только убирает из маршрутизации."""
    import yaml
    from . import config_service as _cs
    from . import retired_credentials as _retired
    target = arl.strip()
    # Сначала реестр, потом файл. Порядок важен: правку файла может отменить
    # работающее приложение (пишет config.yaml целиком из памяти), реестр —
    # нет, и он же не даст записи вернуться при следующем сохранении.
    _retired.retire("deezer_arl", target, "ARL отвергнут Deezer")
    for path in _yaml_files_to_check():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        if primary and (data.get("deezer-arl") or "").strip() == target:
            data["deezer-arl"] = ""
            changed = True
        pool = data.get("deezer-accounts")
        if isinstance(pool, list):
            kept = [a for a in pool
                    if not (isinstance(a, dict) and (a.get("arl") or "").strip() == target)]
            if len(kept) != len(pool):
                data["deezer-accounts"] = kept
                changed = True
        if changed:
            _cs._atomic_write_yaml(path, data)


def check_all_deezer_arls(threshold: int = DEFAULT_THRESHOLD) -> list[str]:
    """Прогнать проверку по всем настроенным Deezer ARL (основной + пул).

    Живой ARL проверяется приватным `gw-light.php` (см. deezer_accounts.arl_info) —
    тот же запрос, что уже используют настройки для показа страны/тарифа, здесь
    просто прогнан по расписанию и с памятью streak. ARL — секрет, поэтому в
    архиве и в state-файле хранится только хвост (см. _mask), не сама строка.

    ВСЕ запросы идут внутри ОДНОГО asyncio.run() — arl_info() использует общий
    процесс-долгоживущий httpx-клиент (ripster/http_client.py), который после
    первого asyncio.run() остаётся привязан к уже закрытому циклу; второй
    asyncio.run() в том же процессе получил бы на нём RuntimeError вместо
    настоящего ответа. Наступил на это при первом же живом тесте (29.08.2026) —
    один настоящий, живой ARL был бы засчитан как ошибка проверки."""
    import asyncio
    from . import deezer_accounts as da
    lines: list[str] = []
    cfg = _load_raw_config()
    if not cfg:
        return lines
    entries = da.configured_arls(cfg)
    if not entries:
        return lines

    async def _check_all():
        out = []
        for entry in entries:
            try:
                info = await da.arl_info(entry["arl"], fresh=True)
            except Exception as e:
                info = {"alive": False, "unreachable": True,
                        "reason": f"ошибка проверки: {type(e).__name__}"}
            out.append((entry, info))
        return out

    try:
        results = asyncio.run(_check_all())
    except Exception as e:
        lines.append(f"⚠️ Проверка Deezer ARL не прошла целиком: {type(e).__name__}")
        return lines

    for entry, info in results:
        arl = entry["arl"]
        primary = bool(entry.get("primary"))
        masked = _mask(arl)
        alive = bool(info.get("alive"))
        country = info.get("country", "") if alive else ""
        reason = "" if alive else (info.get("reason") or "не отвечает")

        # Недоступность ≠ приговор. Если до Deezer не достучались (таймаут, 5xx,
        # оборванная сеть), про ARL мы не узнали НИЧЕГО — и права наказывать за
        # это streak'ом у нас нет: три сетевых сбоя подряд сняли бы с
        # маршрутизации совершенно живую учётку, а гость получил бы отказ по
        # вине нашего канала. Пишем строкой в отчёт и идём дальше.
        if not alive and info.get("unreachable"):
            lines.append(f"⚠️ Deezer ARL {masked} ({entry.get('label','?')}): "
                         f"проверить не удалось ({reason}) — учётка не тронута")
            continue

        streak, archived = record_check(
            "deezer_arl", _ident(arl), alive, country=country, reason=reason,
            threshold=threshold,
            disable_fn=lambda _k, _key, _arl=arl, _primary=primary: disable_deezer_arl(_primary, _arl),
        )
        if archived:
            lines.append(f"💀 Deezer ARL {masked} ({entry.get('label','?')}) "
                         f"архивирован и снят из config/tokens ({reason}) — "
                         f"запись в DEAD_ACCOUNTS.txt")
        elif not alive and streak > 0:
            lines.append(f"⚠️ Deezer ARL {masked} ({entry.get('label','?')}): "
                         f"{streak}/{threshold} неудачных проверок подряд ({reason})")
    # Основной Free, а в пуле есть Family с lossless → повышаем автоматически.
    try:
        if not dry_run():
            lines += promote_best_deezer()
    except Exception as e:  # noqa: BLE001
        lines.append(f"⚠️ Deezer: автозамена основного не отработала: {type(e).__name__}")
    return lines


def disable_tidal_account(primary: bool, secret: str) -> None:
    """Снять мёртвую учётку Tidal с маршрутизации.

    Как и у Deezer: сперва реестр снятых, потом файл. Приложение пишет
    config.yaml целиком из памяти и вернуло бы удалённую запись обратно —
    реестр этого не даст.
    """
    import yaml

    from . import config_service as _cs
    from . import retired_credentials as _retired

    target = (secret or "").strip()
    if not target:
        return
    _retired.retire("tidal_account", target, "учётка отвергнута Tidal")
    for path in _yaml_files_to_check():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        if primary and (data.get("tidal-refresh") or "").strip() == target:
            # Чистим ВСЮ основную учётку, а не только refresh: оставленный
            # access-токен живёт ещё сутки и всё это время выглядел бы рабочим.
            for k in ("tidal-refresh", "tidal-token", "tidal-user-id", "tidal-token-expiry"):
                if data.get(k):
                    data[k] = ""
            changed = True
        pool = data.get("tidal-accounts")
        if isinstance(pool, list):
            kept = [a for a in pool
                    if not (isinstance(a, dict)
                            and ((a.get("refresh") or a.get("tidal-refresh") or "").strip() == target
                                 or (a.get("email") or a.get("tidal-email") or "").strip() == target))]
            if len(kept) != len(pool):
                data["tidal-accounts"] = kept
                changed = True
        if changed:
            _cs._atomic_write_yaml(path, data)


def check_all_tidal_accounts(threshold: int = DEFAULT_THRESHOLD,
                             promote: bool = True) -> list[str]:
    """Прогнать проверку по всем учёткам Tidal (основная + пул).

    Меряется тем же способом, что показывают настройки: обновление токена,
    затем подписка (`tidal_accounts.account_info`). Секрет наружу не идёт — ни
    в отчёт, ни в state-файл попадает только хвост.

    Как и у Deezer: сетевая авария НЕ засчитывается в streak. Три обрыва подряд
    иначе сняли бы живую учётку, и виноват был бы наш канал, а не она.

    Всё в ОДНОМ `asyncio.run()`: общий httpx-клиент привязывается к первому же
    циклу, и второй запуск в том же процессе получил бы RuntimeError вместо
    ответа — на этом уже наступали с Deezer 29.08.2026.
    """
    import asyncio

    from . import tidal_accounts as ta
    from . import tidal_pool as tp

    lines: list[str] = []
    cfg = _load_raw_config()
    if not cfg:
        return lines
    accounts = tp.configured_accounts(cfg)
    if not accounts:
        return lines

    async def _check_all():
        out = []
        for i, acct in enumerate(accounts):
            try:
                info = await ta.account_info(acct, fresh=True)
            except Exception as e:  # noqa: BLE001
                info = {"alive": False, "unreachable": True,
                        "reason": f"ошибка проверки: {type(e).__name__}"}
            out.append((i, acct, info))
        return out

    try:
        results = asyncio.run(_check_all())
    except Exception as e:  # noqa: BLE001
        lines.append(f"⚠️ Проверка учёток Tidal не прошла целиком: {type(e).__name__}")
        return lines

    from . import account_roster as _ar
    active_secret = (cfg.get("tidal-refresh") or "").strip()
    roster: list[str] = []

    for i, acct, info in results:
        secret = ta.account_secret(acct)
        masked = _mask(secret)
        label = acct.get("label") or f"слот {i}"
        alive = info.get("alive")
        reason = "" if alive else (info.get("reason") or "не отвечает")

        # Единая карточка учётки: флаг · страна · тариф · срок · статус.
        # active — та, что реально в config; premium у Tidal = отдаёт lossless.

        # Сетевая авария внутри самой проверки (исключение из account_info,
        # перехваченное в _check_all) приходит как alive=False+unreachable=True.
        # Как у Deezer/Qobuz: про учётку не узнали НИЧЕГО — streak не копим и
        # карточку о смерти не выводим, иначе три обрыва подряд сняли бы живую
        # учётку, а виноват был бы наш канал, а не она.
        if not alive and info.get("unreachable"):
            lines.append(f"⚠️ Tidal {label} ({masked}): {reason} — учётка не тронута")
            continue

        if alive is not None:
            _status = _ar.classify(
                info, is_active=(secret and secret == active_secret),
                premium=bool(info.get("lossless")))
            roster.append(_ar.line("Tidal", label, info, _status, premium=bool(info.get("lossless"))))

        if alive is None:
            # «Не знаем» — это не «мертва». Вход по паролю измерить нечем, а
            # сетевой сбой — свойство канала.
            lines.append(f"⚠️ Tidal {label} ({masked}): {reason} — учётка не тронута")
            continue

        streak, archived = record_check(
            "tidal_account", _ident(secret), bool(alive),
            country=info.get("country", "") if alive else "",
            reason=reason, threshold=threshold,
            disable_fn=lambda _k, _key, _s=secret, _p=(i == 0): disable_tidal_account(_p, _s),
        )
        if archived:
            lines.append(f"💀 Tidal {label} ({masked}) архивирована и снята из "
                         f"config/tokens ({reason}) — запись в DEAD_ACCOUNTS.txt")
        elif not alive and streak > 0:
            lines.append(f"⚠️ Tidal {label} ({masked}): {streak}/{threshold} "
                         f"неудачных проверок подряд ({reason})")
        elif alive and not info.get("lossless"):
            # Живая, но без lossless — снимать не за что (AAC она отдаёт), а вот
            # держать такую ОСНОВНОЙ незачем: ниже `promote_best_tidal()`
            # заменит её на полноценную, если та есть.
            lines.append(f"⚠️ Tidal {label} ({masked}): жива, но lossless не отдаёт "
                         f"({info.get('plan') or '?'}, {info.get('quality') or '?'}, "
                         f"до {str(info.get('valid_until') or '?')[:10]})")

    if roster:
        lines.append("Tidal — учётки:")
        lines += [f"  {r}" for r in roster]

    if promote:
        # Замена делается ПОСЛЕ измерений и по их результату — иначе решение
        # принималось бы по вчерашнему кэшу.
        try:
            lines += promote_best_tidal()
        except Exception as e:  # noqa: BLE001
            lines.append(f"⚠️ Tidal: автозамена основной учётки не отработала: {type(e).__name__}")
    return lines


def _notify_app_config_changed() -> bool:
    """Попросить работающее приложение перечитать конфиг с диска.

    Сторож правит файл из ОТДЕЛЬНОГО процесса, а приложение держит конфиг в
    памяти и сохраняет его целиком — без этого звонка первая же запись вернула
    бы снятую учётку обратно. Приложение не запущено — правка просто доживёт
    до следующего старта, и это не ошибка.
    """
    try:
        import urllib.request

        # Origin/Referer обязательны: у приложения стоит гард против запросов
        # с чужих страниц, и POST без них он отбивает как межсайтовый — что и
        # случилось при первой проверке (403 «Cross-site request blocked»).
        # /api/config/reload теперь owner-only (был только loopback, а за туннелем
        # внешний клиент тоже 127.0.0.1 — security 18.09.2026). Сторож свой: читает
        # session-secret из config.yaml и подписывает cookie так же, как приложение
        # (auth._sign_session). Если секрета нет — шлём без cookie: на локальной
        # машине БЕЗ туннеля gate пропустит по loopback.
        headers = {"Origin": "http://127.0.0.1:7799",
                   "Referer": "http://127.0.0.1:7799/"}
        try:
            import yaml as _y, hmac as _hm, hashlib as _hl, time as _tm
            cfg_path, _ = _config_paths()
            _sec = ((_y.safe_load(cfg_path.read_text(encoding="utf-8")) or {}).get("session-secret") or "")
            if _sec:
                _iss = int(_tm.time())
                _mac = _hm.new(_sec.encode(), str(_iss).encode(), _hl.sha256).hexdigest()
                headers["Cookie"] = f"ripster-session={_iss}.{_mac}"
        except Exception:
            pass
        req = urllib.request.Request(
            "http://127.0.0.1:7799/api/config/reload", data=b"", method="POST",
            headers=headers)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def promote_best_tidal() -> list[str]:
    """Поставить основной учёткой Tidal ту, что реально отдаёт hi-res.

    Владелец 12.09.2026: «в таких случаях нужно заменять учётку на полностью
    рабочую, автоматически по результату проверки». Сообщить «основная — INTRO
    без lossless» и оставить всё как есть — это ровно тот случай, когда отчёт
    подменяет починку.

    Замена делается в ДВУХ местах, и второе важнее первого:

    * ключи `tidal-*` в конфиге — из них берут поиск, карточки и сопряжение;
    * **сессия OrpheusDL** (`orpheus/config/loginstorage.bin`) — именно ею
      скачивает движок. Поменять только конфиг значит переставить вывеску:
      качать ПК продолжил бы прежней учёткой.

    Прежняя основная не выбрасывается, а переезжает в пул: она может быть
    жива и годиться на AAC, а её потеря была бы молчаливым уроном.
    """
    import yaml

    from . import config_service as _cs
    from . import tidal_accounts as ta
    from . import tidal_pool as tp
    from . import account_fallback as _afb

    lines: list[str] = []
    cfg = _load_raw_config()
    if not cfg:
        return lines
    accounts = tp.configured_accounts(cfg)
    if len(accounts) < 2:
        return lines

    cur = accounts[0]
    cur_info = ta.known(ta.account_secret(cur)) or {}
    # Менять есть смысл, только если основная НЕ отдаёт lossless. Живая
    # премиум-учётка не трогается никогда: перестановка ради перестановки
    # сбросила бы сессию и ничего не улучшила.
    if cur_info.get("lossless"):
        return lines

    best = None
    for i in _afb.order_indices(accounts):
        if i == 0:
            continue
        info = ta.known(ta.account_secret(accounts[i])) or {}
        if info.get("alive") and info.get("lossless"):
            best = (i, accounts[i], info)
            break
    if not best:
        return lines

    idx, acct, info = best
    new_refresh = (acct.get("tidal-refresh") or "").strip()
    old_refresh = (cur.get("tidal-refresh") or "").strip()
    if not new_refresh or new_refresh == old_refresh:
        return lines

    # 1. Сессия движка. Делаем ПЕРВОЙ: если не выйдет, конфиг лучше не трогать —
    #    иначе вывеска сменится, а качать продолжит старая учётка.
    rep = tp.write_session(0, new_refresh, acct.get("tidal-country") or info.get("country", ""))
    if not rep.get("ok"):
        lines.append(f"⚠️ Tidal: замена основной учётки отменена — сессию выписать не вышло "
                     f"({rep.get('why')})")
        return lines

    # 2. Конфиг: новая — основной, старая — в пул (если жива), без дублей.
    old_country = (cur.get("tidal-country") or cur_info.get("country") or "").upper()
    old_alive = bool(cur_info.get("alive"))
    for path in _yaml_files_to_check():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        if (data.get("tidal-refresh") or "").strip() == old_refresh:
            data["tidal-refresh"] = new_refresh
            data["tidal-country"] = (acct.get("tidal-country") or info.get("country") or "").upper()
            # Прежний access-токен и uid принадлежат СТАРОЙ учётке и живут ещё
            # сутки: оставить их значит отдавать телефону чужую пару.
            for k in ("tidal-token", "tidal-user-id", "tidal-token-expiry"):
                if k in data:
                    data[k] = ""
            changed = True
        pool = data.get("tidal-accounts")
        if isinstance(pool, list):
            kept = [a for a in pool
                    if not (isinstance(a, dict)
                            and (a.get("refresh") or a.get("tidal-refresh") or "").strip() == new_refresh)]
            if old_alive and old_refresh and not any(
                isinstance(a, dict)
                and (a.get("refresh") or a.get("tidal-refresh") or "").strip() == old_refresh
                for a in kept
            ):
                kept.append({"label": f"{cur.get('label') or 'прежняя основная'}"
                                      f" ({cur_info.get('plan') or 'тариф неизвестен'})",
                             "refresh": old_refresh, "country": old_country})
            if kept != pool:
                data["tidal-accounts"] = kept
                changed = True
        if changed:
            _cs._atomic_write_yaml(path, data)

    # 3. Сказать живому приложению перечитать конфиг. Без этого оно сохранит
    #    свой снимок памяти и вернёт старую основную — правка выглядела бы
    #    сделанной и молча откатилась бы.
    _notify_app_config_changed()

    lines.append(
        f"🔁 Tidal: основной назначена «{acct.get('label')}» "
        f"({info.get('plan')}, {info.get('quality')}, {info.get('country')}) — "
        f"прежняя ({cur_info.get('plan') or '?'}, {cur_info.get('quality') or 'без lossless'}) "
        f"{'перенесена в пул' if old_alive else 'снята'}; сессия движка переписана "
        f"({', '.join(rep.get('clients') or [])})")
    return lines


def promote_best_deezer() -> list[str]:
    """Поставить основным ARL Deezer тот, что реально отдаёт lossless.

    Владелец 13.09.2026: активный Deezer оказался BG Free, а Family с FLAC
    простаивали в пуле — «где автоматизм?». Раннер уже берёт Family per-download
    (см. runner.py), но ОСНОВНОЙ в конфиге оставался Free: его видит панель,
    сопряжение отдаёт телефону именно его, а «основной = Free» — это скрытая
    беда. Здесь основной меняется на lossless персистентно, прежний уезжает в
    пул (жив — годится, терять нельзя). У Deezer нет сессии-файла (ARL пишется в
    `.arl` при каждой загрузке), поэтому правим только конфиг.
    """
    import asyncio

    import yaml

    from . import config_service as _cs
    from . import deezer_accounts as da

    lines: list[str] = []
    cfg = _load_raw_config()
    if not cfg:
        return lines
    arls = da.configured_arls(cfg)
    if len(arls) < 2:
        return lines

    async def _survey():
        out = []
        for a in arls:
            info = await da.arl_info(a["arl"])
            out.append((a, info))
        return out

    try:
        results = asyncio.run(_survey())
    except Exception as e:  # noqa: BLE001
        lines.append(f"⚠️ Deezer: автозамена основного не отработала: {type(e).__name__}")
        return lines

    cur, cur_info = results[0]
    # Основное поле пустое — первая запись пула не «прежний основной»: менять
    # нечего, и строка «назначен» была бы неправдой (17.09.2026).
    if not cur.get("primary"):
        return lines
    # Основной уже lossless — не трогаем: перестановка ради перестановки только
    # сбросила бы порядок пула и ничего не улучшила.
    if cur_info.get("lossless"):
        return lines

    best = next(((a, info) for a, info in results[1:]
                 if info.get("alive") and info.get("lossless")), None)
    if not best:
        return lines

    new_arl = best[0]["arl"].strip()
    old_arl = cur["arl"].strip()
    if not new_arl or new_arl == old_arl:
        return lines
    old_alive = bool(cur_info.get("alive"))

    wrote = False
    for path in _yaml_files_to_check():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        if (data.get("deezer-arl") or "").strip() != old_arl:
            continue
        data["deezer-arl"] = new_arl
        pool = data.get("deezer-accounts") or []
        # Убираем повышенный из пула, старый основной (если жив) — в пул.
        kept = [a for a in pool if isinstance(a, dict)
                and (a.get("arl") or "").strip() != new_arl]
        if old_alive and not any(isinstance(a, dict)
                                 and (a.get("arl") or "").strip() == old_arl for a in kept):
            kept.append({"arl": old_arl,
                         "label": f"{cur.get('label') or 'прежняя основная'}"
                                  f" ({cur_info.get('plan') or 'Free'})"})
        data["deezer-accounts"] = kept
        _cs._atomic_write_yaml(path, data)
        wrote = True

    if not wrote:
        return lines
    _notify_app_config_changed()
    lines.append(
        f"🔁 Deezer: основным назначен lossless-ARL "
        f"({best[1].get('country') or '?'}, {best[1].get('plan') or 'Family'}) — "
        f"прежний ({cur_info.get('plan') or 'Free'}) "
        f"{'перенесён в пул' if old_alive else 'снят'}")
    return lines


def disable_yandex_token(primary: bool, token: str) -> None:
    """Снять мёртвый токен Яндекса: реестр, затем тот файл, где он лежит."""
    import yaml

    from . import config_service as _cs
    from . import retired_credentials as _retired

    target = (token or "").strip()
    if not target:
        return
    _retired.retire("yandex_token", target, "токен отвергнут Яндексом")
    for path in _yaml_files_to_check():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        if primary and (data.get("yandex-token") or "").strip() == target:
            data["yandex-token"] = ""
            changed = True
        pool = data.get("yandex-accounts")
        if isinstance(pool, list):
            kept = [a for a in pool
                    if not (isinstance(a, dict)
                            and (a.get("token") or a.get("yandex-token") or "").strip() == target)]
            if len(kept) != len(pool):
                data["yandex-accounts"] = kept
                changed = True
        if changed:
            _cs._atomic_write_yaml(path, data)


def check_all_yandex_tokens(threshold: int = DEFAULT_THRESHOLD) -> list[str]:
    """Прогнать проверку по всем токенам Яндекса (основной + пул).

    Тонкость, которой нет у других сервисов: `account/status` отвечает 403 вне
    России. Это «не смогли спросить», а не «токен мёртв», и в streak такое не
    засчитывается — иначе VPN, упавший на сутки, снял бы все живые учётки.
    """
    import asyncio

    from . import yandex_accounts as ya

    lines: list[str] = []
    cfg = _load_raw_config()
    if not cfg:
        return lines
    entries = ya.configured_tokens(cfg)
    if not entries:
        return lines

    async def _check_all():
        out = []
        for e in entries:
            try:
                info = await ya.token_info(e["token"], fresh=True)
            except Exception as ex:  # noqa: BLE001
                info = {"alive": None, "unreachable": True,
                        "reason": f"ошибка проверки: {type(ex).__name__}"}
            out.append((e, info))
        return out

    try:
        results = asyncio.run(_check_all())
    except Exception as e:  # noqa: BLE001
        lines.append(f"⚠️ Проверка токенов Яндекса не прошла целиком: {type(e).__name__}")
        return lines

    for entry, info in results:
        token = entry["token"]
        masked = _mask(token)
        label = entry.get("label") or "?"
        alive = info.get("alive")
        reason = "" if alive else (info.get("reason") or "не отвечает")

        if alive is None:
            lines.append(f"⚠️ Яндекс {label} ({masked}): {reason} — учётка не тронута")
            continue

        streak, archived = record_check(
            "yandex_token", _ident(token), bool(alive), reason=reason, threshold=threshold,
            disable_fn=lambda _k, _key, _t=token, _p=bool(entry.get("primary")):
                disable_yandex_token(_p, _t),
        )
        if archived:
            lines.append(f"💀 Яндекс {label} ({masked}) архивирован и снят из "
                         f"config/tokens ({reason}) — запись в DEAD_ACCOUNTS.txt")
        elif not alive and streak > 0:
            lines.append(f"⚠️ Яндекс {label} ({masked}): {streak}/{threshold} "
                         f"неудачных проверок подряд ({reason})")
        elif alive and not info.get("plus"):
            lines.append(f"⚠️ Яндекс {label} ({masked}): жив, но без Plus — FLAC не отдаст")
    return lines


def disable_qobuz_account(secret: str) -> None:
    """Снимает мёртвую учётку Qobuz: ищет её ТОЧНО в том файле (config.yaml или
    tokens/*.yaml), где она прописана. Основная учётка чистится по полям,
    запись пула удаляется целиком.

    `secret` — токен, а при входе по паролю почта: то же, чем учётка опознаётся
    в `qobuz_accounts.account_secret`.
    """
    import yaml
    from . import config_service as _cs
    from . import retired_credentials as _retired
    target = (secret or "").strip()
    if not target:
        return
    # Сначала реестр, потом файл: правку файла отменит работающее приложение
    # (пишет config.yaml целиком из памяти), реестр — нет. См. 03.09.2026,
    # когда снятый ARL вернулся в конфиг через сутки.
    _retired.retire("qobuz_account", target, "Qobuz отверг учётные данные")
    for path in _yaml_files_to_check():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        if (data.get("qobuz-auth-token") or "").strip() == target or \
           ((data.get("qobuz-email") or "").strip() == target
                and not (data.get("qobuz-auth-token") or "").strip()):
            for k in ("qobuz-auth-token", "qobuz-user-id", "qobuz-email", "qobuz-password"):
                if k in data:
                    data[k] = ""
            changed = True
        pool = data.get("qobuz-accounts")
        if isinstance(pool, list):
            def _same(a) -> bool:
                if not isinstance(a, dict):
                    return False
                tok = (a.get("qobuz-auth-token") or a.get("auth_token") or "").strip()
                mail = (a.get("qobuz-email") or a.get("email") or "").strip()
                return tok == target or (mail == target and not tok)
            kept = [a for a in pool if not _same(a)]
            if len(kept) != len(pool):
                data["qobuz-accounts"] = kept
                changed = True
        if changed:
            _cs._atomic_write_yaml(path, data)


def check_all_qobuz_accounts(threshold: int = DEFAULT_THRESHOLD) -> list[str]:
    """Прогнать проверку по всем настроенным учёткам Qobuz.

    Три исхода, и путать их нельзя:

    * 401 — учётные данные отвергнуты. Это приговор учётке: копим streak и
      после порога снимаем с маршрутизации.
    * жив, но подписки нет / она истекла — токен ВАЛИДЕН, качать нельзя
      (`credential.parameters` пуст, streamrip падает с IneligibleError).
      Сообщаем владельцу и убираем из очереди перебора (см.
      `qobuz_pool.health_rank`), но НЕ снимаем: подписку продлевают, и удалять
      за это рабочий токен — потеря данных.
    * не достучались (сеть, 5xx, неверный app_id) — про учётку не узнали
      ничего, streak не трогаем. Иначе три сетевых сбоя подряд снесли бы живую
      учётку; на Deezer этот дефект нашёлся 04.09.2026.
    """
    import asyncio
    from . import qobuz_accounts as qa
    lines: list[str] = []
    cfg = _load_raw_config()
    if not cfg:
        return lines
    accounts = qa.configured_accounts(cfg)
    if not accounts:
        return lines
    app_id = (cfg.get("qobuz-app-id") or "").strip()

    async def _check_all():
        out = []
        for a in accounts:
            try:
                info = await qa.account_info(a, app_id=app_id, fresh=True)
            except Exception as e:
                info = {"alive": False, "unreachable": True,
                        "reason": f"ошибка проверки: {type(e).__name__}"}
            out.append((a, info))
        return out

    try:
        results = asyncio.run(_check_all())
    except Exception as e:
        lines.append(f"⚠️ Проверка учёток Qobuz не прошла целиком: {type(e).__name__}")
        return lines

    for acct, info in results:
        secret = qa.account_secret(acct)
        masked = _mask(secret)
        label = acct.get("label") or "?"
        reason = info.get("reason") or ""

        if info.get("alive") and not info.get("eligible"):
            lines.append(f"⚠️ Qobuz {masked} ({label}): токен рабочий, но {reason} — "
                         f"из перебора исключён, НЕ снят (продли подписку)")
            continue
        if not info.get("alive") and info.get("unreachable"):
            lines.append(f"⚠️ Qobuz {masked} ({label}): проверить не удалось "
                         f"({reason}) — учётка не тронута")
            continue

        streak, archived = record_check(
            "qobuz_account", _ident(secret), bool(info.get("alive")),
            country=info.get("country", "") if info.get("alive") else "",
            reason=reason or "не отвечает", threshold=threshold,
            disable_fn=lambda _k, _key, _s=secret: disable_qobuz_account(_s),
        )
        if archived:
            lines.append(f"💀 Qobuz {masked} ({label}) архивирован и снят из "
                         f"config/tokens ({reason}) — запись в DEAD_ACCOUNTS.txt")
        elif not info.get("alive") and streak > 0:
            lines.append(f"⚠️ Qobuz {masked} ({label}): {streak}/{threshold} "
                         f"неудачных проверок подряд ({reason})")
    return lines


def disable_soundcloud_token(token: str) -> None:
    """Снимает мёртвый OAuth-токен SoundCloud из того файла, где он лежит."""
    import yaml
    from . import config_service as _cs
    from . import retired_credentials as _retired
    target = (token or "").strip()
    if not target:
        return
    _retired.retire("soundcloud_token", target, "SoundCloud отверг токен")
    for path in _yaml_files_to_check():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        if (data.get("soundcloud-oauth-token") or "").strip() == target:
            data["soundcloud-oauth-token"] = ""
            changed = True
        pool = data.get("soundcloud-accounts")
        if isinstance(pool, list):
            kept = [a for a in pool
                    if not (isinstance(a, dict) and (a.get("token") or "").strip() == target)]
            if len(kept) != len(pool):
                data["soundcloud-accounts"] = kept
                changed = True
        if changed:
            _cs._atomic_write_yaml(path, data)


def check_all_soundcloud_tokens(threshold: int = DEFAULT_THRESHOLD) -> list[str]:
    """Прогнать проверку по всем токенам SoundCloud.

    Владелец 04.09.2026: «внедрил третий токен, но надо проверять на наличие
    вообще подписки». У SoundCloud это особенно нужно: токен без Go+ ВАЛИДЕН,
    `/me` отвечает 200 — просто HQ-поток (AAC 256) такой учётке не дают, и трек
    приезжает в 128 kbps. По коду ответа это не отличить.

    Три исхода, как у Qobuz: 401 — приговор (копим streak, после порога
    снимаем); жив без Go+ — сообщаем, но НЕ снимаем и из перебора не убираем,
    128 kbps это рабочая загрузка; не достучались — streak не трогаем.

    Отдельно предупреждаем про разные токены ОДНОЙ учётки: у владельца два из
    трёх принадлежали аккаунту `goku`, то есть пул из трёх записей давал две
    независимые учётки — на параллельность и перебор это влияет, а по списку
    токенов не видно.
    """
    import asyncio
    from . import soundcloud_accounts as sa
    lines: list[str] = []
    cfg = _load_raw_config()
    if not cfg:
        return lines
    accounts = sa.configured_accounts(cfg)
    if not accounts:
        return lines

    async def _check_all():
        out = []
        for a in accounts:
            try:
                info = await sa.account_info(a["token"], fresh=True)
            except Exception as e:
                info = {"alive": False, "unreachable": True,
                        "reason": f"ошибка проверки: {type(e).__name__}"}
            out.append((a, info))
        return out

    try:
        results = asyncio.run(_check_all())
    except Exception as e:
        lines.append(f"⚠️ Проверка токенов SoundCloud не прошла целиком: {type(e).__name__}")
        return lines

    from . import account_roster as _ar
    # «Активна» — та учётка, которая обслуживает загрузки. При пуле это не
    # primary-ключ конфига, а выбор пула (soundcloud_pool.active_token):
    # иначе отчёт показывает бесплатную primary активной, хотя движок
    # подменяет токен на Go+-слот (или наоборот).
    active_token = (cfg.get("soundcloud-oauth-token") or "").strip()
    try:
        from . import soundcloud_pool as _scp
        active_token = _scp.active_token(cfg) or active_token
    except Exception:                     # noqa: BLE001
        pass
    roster: list[str] = []

    seen_logins: dict[str, str] = {}
    for acct, info in results:
        token = acct["token"]
        masked = _mask(token)
        label = acct.get("label") or "?"
        reason = info.get("reason") or ""

        # Карточка учётки: у SoundCloud premium = Go+; страна из /me, срока у
        # OAuth-токена нет (non-expiring), поэтому в строке будет «срок ?».
        if info.get("alive") is not None:
            _status = _ar.classify(
                info, is_active=(token and token == active_token),
                premium=bool(info.get("go_plus")))
            roster.append(_ar.line("SoundCloud", label, info, _status, premium=bool(info.get("go_plus"))))

        if info.get("alive"):
            login = (info.get("login") or "").strip().lower()
            if login:
                if login in seen_logins:
                    lines.append(f"⚠️ SoundCloud {masked} ({label}) — тот же аккаунт "
                                 f"«{info.get('login')}», что и {seen_logins[login]}: "
                                 f"две записи, одна учётка")
                else:
                    seen_logins[login] = masked
            if not info.get("go_plus"):
                lines.append(f"⚠️ SoundCloud {masked} ({label}): {reason} — "
                             f"токен рабочий, снимать не за что")
            continue

        if info.get("unreachable"):
            lines.append(f"⚠️ SoundCloud {masked} ({label}): проверить не удалось "
                         f"({reason}) — токен не тронут")
            continue

        streak, archived = record_check(
            "soundcloud_token", _ident(token), False,
            country=info.get("country", ""), reason=reason or "не отвечает",
            threshold=threshold,
            disable_fn=lambda _k, _key, _t=token: disable_soundcloud_token(_t),
        )
        if archived:
            lines.append(f"💀 SoundCloud {masked} ({label}) архивирован и снят из "
                         f"config/tokens ({reason}) — запись в DEAD_ACCOUNTS.txt")
        elif streak > 0:
            lines.append(f"⚠️ SoundCloud {masked} ({label}): {streak}/{threshold} "
                         f"неудачных проверок подряд ({reason})")

    if roster:
        lines.append("SoundCloud — учётки:")
        lines += [f"  {r}" for r in roster]
    return lines


def check_all_apple_slots(threshold: int = DEFAULT_THRESHOLD) -> list[str]:
    """Прогнать проверку по RUNNING Apple wrapper-контейнерам.

    Намеренно НЕ `docker ps -a`: простаивающие слоты штатно гасит отдельный сборщик
    (см. apple_accounts.py) через 5 минут бездействия — остановленный контейнер это
    норма, не признак смерти, и не должен копить streak. Признак реальной смерти —
    контейнер ЖИВ (кто-то его поднял под задачу), но сессия не работает: залип в
    login-цикле или порт 30020 так и не отвечает. Именно так был найден rip-wrapper-3.

    Возвращает список человекочитаемых строк для отчёта чекера (пусто = всё живо
    или ничего не пересекло порог в этом проходе)."""
    from . import apple_accounts as aa
    lines: list[str] = []
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10, creationflags=_CNW,
        ).stdout
    except Exception:
        return lines
    containers = [n for n in out.splitlines() if n.strip() and
                  ("wrapper" in n or n.startswith("rip-"))]
    for c in containers:
        alive, reason = apple_slot_alive(c)
        country = aa.container_storefront(c) if alive else ""
        streak, archived = record_check(
            "apple_slot", c, alive, country=country, reason=reason,
            threshold=threshold, disable_fn=disable_apple_slot,
        )
        if archived:
            lines.append(f"💀 Слот {c} архивирован и остановлен ({reason}) — "
                         f"запись в DEAD_ACCOUNTS.txt")
        elif not alive and streak > 0:
            lines.append(f"⚠️ Слот {c}: {streak}/{threshold} неудачных проверок подряд ({reason})")
    return lines
