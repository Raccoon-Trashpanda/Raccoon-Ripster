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
   типа). С 24.09.2026 Apple ходит по тому же пути, что Deezer/Qobuz/SoundCloud, с
   ОДНИМ отличием — порог в 5 проходов вместо 3 (`APPLE_THRESHOLD`), и снятие там
   полное: контейнер удаляется (`docker rm -f`), порт освобождается, запись уходит
   из конфига через писатель приложения, учётка попадает в реестр снятых. Раньше
   для Apple здесь был `docker stop`, и он не освобождал ровно ничего:
   остановленный `rip-wrapper-3` три недели держал имя, слот и порт.

Чего модуль НЕ делает: не поднимает новый слот на освободившееся место и не логинит
новый аккаунт автоматически. Каждый логин жжёт лимит устройств у Apple (см. скилл
ripster-apple-wrapper) — автоматически заводить сессии без присмотра это ровно тот
инцидент 2026-07-09, который уже один раз уронил пул. Освободившийся порт просто
перестаёт быть занят мёртвым контейнером — следующий реальный аккаунт владелец
подключает сам, и никакого конфликта портов не будет, потому что контейнер снятой
учётки удаляется, а Docker снимает его порт-биндинг вместе с ним.
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
    # потому что выглядит как знание.
    # `apple_slot:*` — счётчики по ИМЕНИ КОНТЕЙНЕРА (до 24.09.2026). Apple-слоты
    # теперь считаются по учётке (`apple_account:*`), потому что контейнера у
    # мёртвой учётки может уже не быть, а имя к тому же занимает новый аккаунт.
    # Старые ключи выбрасываем разом: держать счётчик того, чего больше нет, —
    # это ровно тот класс мусора, из-за которого учётка могла «почти умереть».
    return {k: v for k, v in st.items()
            if (("#" in k or not k.startswith(_IDENT_KINDS))
                and not k.startswith("apple_slot:"))}


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
                  disable_fn=None, can_retire=None, prune: bool = False) -> tuple[int, bool]:
    """Отметить результат одной проверки credential'а.

    kind — тип ("apple_slot", "deezer_arl", "tidal_token", …), key — стабильный
    идентификатор внутри типа (имя контейнера, маскированный хвост ARL и т.п.).
    Возвращает (текущий streak неудач, был ли только что заархивирован и отключён).
    disable_fn(kind, key) — вызывается РОВНО ОДИН РАЗ при достижении порога; исключения
    из него не должны ронять сам чекер.
    can_retire() — страж, которого спросить ПЕРЕД снятием (см. `_apple_retire_guard`):
    вернёт ложь — streak остаётся на пороге и попытка повторится в следующем проходе.
    Порог без права снятия — это не «почти сняли», а «ждём явного сигнала», и
    врать об этом в отчёте нельзя.
    prune=True — запись снимается (учётки больше нет), счётчик удаляется вовсе:
    оставить его значило бы «ключ мёртвой учётки вечно помнит её streak».
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

    if can_retire is not None:
        try:
            allowed = bool(can_retire())
        except Exception:
            allowed = False
        if not allowed:
            # Держим streak на пороге: сигнал не «потерян», а «ещё не достаточен».
            entry["streak"] = threshold
            st[state_key] = entry
            _save_state(st)
            return threshold, False

    # Порог достигнут — архивируем и отключаем, затем сбрасываем streak, чтобы не
    # архивировать одну и ту же учётку заново на каждом следующем проходе чекера.
    _append_archive(kind, key, entry.get("last_country", ""), reason or "не отвечает")
    if disable_fn is not None:
        try:
            disable_fn(kind, key)
        except Exception:
            pass
    if prune:
        st.pop(state_key, None)
    else:
        entry["streak"] = 0
        st[state_key] = entry
    _save_state(st)
    return threshold, True


#: Apple — порог НЕ `DEFAULT_THRESHOLD`, и это не вежливость. Учётка Apple
#: дороже любого ARL: каждый логин жжёт слот устройства (скилл
#: ripster-apple-wrapper), а ложное снятие ещё и перенумеровывает слоты,
#: заставляя остальных перелогиниться. Пять проходов = 2.5 суток при графике
#: 08:00/20:00 — переживает ночную аварию Docker Desktop и выходной Apple.
APPLE_THRESHOLD = 5

#: Классы «учётка мертва ЯВНО». Только они дают право снять основной слот
#: (страж `apple_retire_guard`); прочие — симптом, а не приговор.
APPLE_HARD_CLASSES = ("account_disabled", "subscription_inactive", "token_rejected")

_REASON_TEXT = {
    "account_disabled":     "Apple заблокировал аккаунт («account is disabled»)",
    "device_limit":         "Apple: лимит устройств (device limit / lease 3062)",
    "login_failed":         "Apple отвергает логин (login failed / response type 4)",
    "subscription_inactive": "подписка Apple Music неактивна",
    "token_rejected":       "Apple отвергает media-user-token (403)",
    "port_dead":            "порт слота не отвечает",
    "":                     "не отвечает",
}


def _verdict(state: str, klass: str = "", reason: str = "", country: str = "") -> dict:
    return {"state": state, "klass": klass, "reason": reason or _REASON_TEXT.get(klass, ""),
            "country": country}


def apple_container_verdict(container: str, port: int = 0) -> dict:
    """Что делает контейнер сейчас: alive | failed | idle | unverified | missing.

    `idle` — остановлен сборщиком простоя, проверкой НЕ считается: штатно
    погашенный через 5 минут слот — норма для большинства пула в любой момент
    суток, а не смерть учётки (доксказано 24.09.2026, иначе сторож выжигал бы
    всё, чем не качали ночь).
    """
    from . import apple_accounts as aa
    info = aa.container_stop_info(container)
    if not info["exists"]:
        return _verdict("missing")
    if not info["running"]:
        br = info["block_reason"]
        if br in aa.HARD_BLOCK_REASONS:
            return _verdict("failed", br)
        if info["idle"]:
            return _verdict("idle")
        return _verdict("unverified",
                        reason=f"остановлен (код выхода {info['exit_code']}), причины нет")
    raw = aa._account_json(container)
    if raw:
        active, why = _apple_subscription_active(raw)
        if active is False:                 # именно явное False, не «не смогли»
            return _verdict("failed", "subscription_inactive", why)
        if active is None:
            if port and not aa._port_open(port):
                return _verdict("failed", "port_dead")
            return _verdict("unverified", reason="ответа о подписке нет — amp-api молчит")
        return _verdict("alive", country=aa.container_storefront(container))
    # Контейнер жив, но 30020 не отдаёт учётку — слушаем его же журнал.
    br = aa.container_block_reason(container)
    if br in aa.HARD_BLOCK_REASONS:
        return _verdict("failed", br)
    if port and not aa._port_open(port):
        return _verdict("failed", "port_dead")
    return _verdict("unverified", reason="порт учётки 30020 молчит — слот прогревается")


def apple_login_block(acct: dict) -> str:
    """УСТОЙЧИВЫЙ сигнал смерти учётки, когда её контейнера уже нет.

    Без этого мёртвая учётка `device_limit_2026-08-01` (владелец, 24.09) жила в
    настройках с августа: контейнера давно нет → проверять нечего → «не
    проверена» → никогда не накапливает неудачи. Пауза входа и метка в конфиге —
    это и есть тот след, который оставил враппер; его и читаем."""
    from . import apple_accounts as aa
    from . import wrapper_pool as wp
    entry = {}
    try:
        entry = wp._blocks_load().get(wp._acct_key(acct or {})) or {}
    except Exception:  # noqa: BLE001
        entry = {}
    reason = str(entry.get("reason") or "")
    if reason in aa.HARD_BLOCK_REASONS:
        return reason
    trace = f"{(acct or {}).get('label') or ''} {(acct or {}).get('id') or ''}".lower()
    for name in aa.HARD_BLOCK_REASONS:
        if name in trace:
            return name
    return ""


def _apple_token_verdict(acct: dict) -> dict:
    """media-user-token спрашиваем у amp-api напрямую: контейнера у такой
    записи нет и быть не может (врапперу токеном расшифровывать нечем)."""
    from . import apple_cookies
    try:
        probe = apple_cookies.probe_media_user_token(str((acct or {}).get("token") or ""))
    except Exception:  # noqa: BLE001
        return _verdict("unverified", reason="проба токена не выполнена")
    state = str((probe or {}).get("state") or "")
    if state == "ok":
        return _verdict("alive", country=str((probe or {}).get("storefront") or ""))
    if state == "token_rejected":
        return _verdict("failed", "token_rejected", str((probe or {}).get("reason") or ""))
    # bad_request — претензия к НАШЕМУ запросу; unknown — нет dev_token или сеть.
    return _verdict("unverified", reason=str((probe or {}).get("reason") or "токен не проверен"))


def apple_account_verdict(acct: dict, slot: int) -> dict:
    """Один проход проверки по ОДНОЙ НАСТРОЕННОЙ учётке (не по контейнеру).

    До 24.09.2026 сторож обходил `docker ps` и видел только поднятые контейнеры,
    поэтому из шести учётки владельца проверялось три — и мёртвая учётка, у
    которой контейнера уже не было, не получала ни одной неудачи никогда."""
    from . import apple_accounts as aa
    if str((acct or {}).get("kind") or "login") == "token":
        return _apple_token_verdict(acct)
    v = apple_container_verdict(aa.container_for_slot(slot), aa.slot_port(slot))
    if v["state"] != "missing":
        return v
    block = apple_login_block(acct)
    if block:
        return _verdict("failed", block)
    return _verdict("unverified",
                    reason="контейнера нет — враппер под учётку не поднимали")


def disable_apple_slot(kind: str, container: str) -> None:
    """УДАЛЯЕТ мёртвый wrapper-контейнер и освобождает его порт.

    Раньше здесь был `docker stop`, и он ничего не освобождал: остановленный
    `rip-wrapper-3` продолжал существовать, занимать имя, слот и порт 10023 три
    недели, пока соседние учётки не могли их получить. `rm -f` — единственный
    способ, которым Docker сам снимает port binding; новый аккаунт вместо
    мёртвого пул не поднимает и не логинит (см. docstring модуля)."""
    try:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True,
                       timeout=30, creationflags=_CNW)
    except Exception:
        pass
    try:
        from . import apple_accounts as aa
        aa.release_container(container)
    except Exception:
        pass


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

    # Мера — сессия движка, а не только `tidal-refresh` из config.yaml: 23.09
    # этот отчёт писал «✅ активна, PREMIUM» про учётку из конфига, пока
    # загрузка в 19:37 падала с TidalAuthError, потому что качает ОрpheusDL
    # своей сессией из loginstorage.bin.
    eng = ta.engine_session_secret()
    eng_secrets = {ta.account_secret(a) for a in accounts}

    async def _check_all():
        out = []
        for i, acct in enumerate(accounts):
            try:
                info = await ta.account_info(acct, fresh=True)
            except Exception as e:  # noqa: BLE001
                info = {"alive": False, "unreachable": True,
                        "reason": f"ошибка проверки: {type(e).__name__}"}
            out.append((i, acct, info))
        # Сессия движка обязана быть измерена в ЭТОМ же прогоне циклов: общий
        # httpx-клиент привязывается к первому asyncio.run, второй кидает
        # RuntimeError вместо ответа (см. докстринг функции).
        if eng and eng not in eng_secrets:
            try:
                out.append((-1, {"tidal-refresh": eng},
                            await ta.account_info({"tidal-refresh": eng}, fresh=True)))
            except Exception as e:  # noqa: BLE001
                out.append((-1, {"tidal-refresh": eng},
                            {"alive": False, "unreachable": True,
                             "reason": f"ошибка проверки: {type(e).__name__}"}))
        return out

    try:
        results = asyncio.run(_check_all())
    except Exception as e:  # noqa: BLE001
        lines.append(f"⚠️ Проверка учёток Tidal не прошла целиком: {type(e).__name__}")
        return lines

    from . import account_roster as _ar
    active_secret = (cfg.get("tidal-refresh") or "").strip()
    eng_info = next((inf for _i, a, inf in results
                     if eng and ta.account_secret(a) == eng), None)
    roster: list[str] = []

    for i, acct, info in results:
        if i < 0:
            continue          # проба сессии движка: она нужна для меры, не для отчёта
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

        # Та же правда, что и в JSON-ростере: «активна» без шанса на следующую
        # загрузку не показывается.
        drift = _ar.engine_drift(is_active=bool(secret and secret == active_secret),
                                 engine_secret=eng, own_secret=secret,
                                 own_alive=alive, engine_info=eng_info or info)
        if drift:
            info["session_drift"] = drift

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
    жива и годиться на AAC, а её потеря была бы молчаливым уроном. Мёртвую
    держать в пуле нельзя, но и терять молча — тем более: она попадает в
    реестр снятых и в DEAD_ACCOUNTS.txt (23.09.2026 новозеландская учётка
    пропала из маршрутизации именно без этой строки).
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
    if old_refresh and not old_alive:
        # Мёртвая прежняя основная уходит из конфига — и это обязано быть
        # записано. 23.09.2026 так исчезла единственная новозеландская учётка:
        # её refresh отозвал Tidal, автопромоут снял её с маршрутизации, и ни
        # в DEAD_ACCOUNTS.txt, ни в реестре снятых не осталось ни строки.
        # Владелец узнал об этом только потому, что релизы перестали приходить.
        from . import retired_credentials as _retired
        why = cur_info.get("reason") or "учётка отвергнута Tidal"
        _retired.retire("tidal_account", old_refresh, f"автопромоут: {why}")
        _append_archive("tidal_account", _ident(old_refresh), old_country, why)
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
        f"{'перенесена в пул' if old_alive else 'снята и записана в DEAD_ACCOUNTS.txt + реестр снятых'}; "
        f"сессия движка переписана "
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


def _wrapper_container_names() -> list[str]:
    """Имена ВСЕХ wrapper-контейнеров, включая остановленные (`docker ps -a`).

    Фильтр — по слоту, а не по подстроке «wrapper»: `wrapper-manager` и чужие
    контейнеры с похожим именем автоматике удалять нельзя."""
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15, creationflags=_CNW).stdout
    except Exception:  # noqa: BLE001
        return []
    from . import apple_accounts as aa
    return [n.strip() for n in out.splitlines()
            if n.strip() and aa.slot_of_container(n.strip()) >= 0]


def apple_container_owner(container: str, accounts: list[dict]) -> str:
    """Кому принадлежит контейнер: 'owned' | 'orphan' | 'unknown'.

    Вопрос «не пора ли удалить контейнер» до 24.09.2026 решался по ИМЕНИ, и это
    был единственный доступный способ — карты аккаунт↔контейнер не было. Теперь
    читаем РЕЕСТР: `-L` из env контейнера и карту `apple_account_map.json`.
    `unknown` (env не прочитался, Docker молчит) — удалять запрещено: молчание
    докера неотличимо от «контейнер чужой», а цена ошибки — живой слот."""
    from . import apple_accounts as aa, retired_credentials as _retired, wrapper_pool as wp
    ids = set()
    digests = set()
    for a in accounts or []:
        for val in (str(a.get("id") or ""), str(a.get("token") or ""),
                    str(a.get("label") or "")):
            if val:
                ids.add(val)
        digests.add(_retired._digest(wp.account_identity(a)))
    env_id = aa.container_env_id(container)
    if env_id:
        return "owned" if env_id in ids else "orphan"
    mapped = aa.account_map()["containers"].get(container)
    if mapped:
        return "owned" if mapped in digests else "orphan"
    slot = aa.slot_of_container(container)
    if slot >= len(accounts or []):
        return "orphan"          # под этот номер учётки в конфиге уже нет
    return "unknown"


def _sweep_apple_orphans(accounts: list[dict], *, dry: bool,
                         live_logins: int) -> list[str]:
    """Контейнер, к которому нет учётки, держит имя, слот и порт даром. В отличие
    от мёртвой УЧЁТКИ, решать тут некому — аккаунта в конфиге больше нет, и пять
    проходов ждать не за чем. Убирается сразу, строкой в отчёт.

    Единственная уздечка — страж (a): живой осиротевший контейнер при нуле
    живых учётки в конфиге не удаляется. Это ровно тот случай, когда список
    поручился в конфиге, а качать всё ещё нечем, кроме как этим контейнером."""
    from . import apple_accounts as aa
    lines: list[str] = []
    for name in _wrapper_container_names():
        if apple_container_owner(name, accounts) != "orphan":
            continue
        running = aa.container_running(name)
        if running and live_logins <= 0:
            lines.append(f"⚠️ Осиротевший {name} жив, но живых учёток в конфиге нет — "
                         f"не трогаем: удалив его, пул лишится последней расшифровки")
            continue
        if dry:
            lines.append(f"🧪 dry-run: осиротевший контейнер {name} НЕ удалён "
                         f"(учётки под него в конфиге нет)")
            continue
        rc = subprocess.run(["docker", "rm", "-f", name],
                            capture_output=True, timeout=30,
                            creationflags=_CNW).returncode
        aa.release_container(name)
        port = aa.slot_port(aa.slot_of_container(name))
        lines.append(f"🧹 Осиротевший контейнер {name} удалён (docker: код {rc}), "
                     f"порт {port} освобождён — учётки под него в конфиге нет")
    return lines


def _drop_apple_account_from_files(digest: str, primary: bool) -> list[str]:
    """Вычеркнуть учётку из того файла (config.yaml или tokens/*.yaml), где она
    реально лежит, — через писатель конфига приложения.

    `config.yaml` руками не правим НИКОГДА: приложение держит его копию в памяти
    и пишет при любом сохранении ЦЕЛИКОМ (см. docstring модуля и
    `retired_credentials`), поэтому точечная правка файла без реестра снятых
    обратима чужой рукой."""
    import yaml
    from . import config_service as _cs
    from . import retired_credentials as _retired
    from . import wrapper_pool as wp
    notes: list[str] = []
    for path in _yaml_files_to_check():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        if primary:
            prim = {"id": data.get("wrapper-apple-id"),
                    "password": data.get("wrapper-password")}
            if (prim["id"] or prim["password"]) and \
                    _retired._digest(wp.account_identity(prim)) == digest:
                data["wrapper-apple-id"] = ""
                data["wrapper-password"] = ""
                changed = True
                notes.append("основная учётка (slot 0) вычищена из wrapper-apple-id")
        pool = data.get("wrapper-accounts")
        if isinstance(pool, list):
            kept = [a for a in pool
                    if not (isinstance(a, dict)
                            and _retired._digest(wp.account_identity(a)) == digest)]
            if len(kept) != len(pool):
                data["wrapper-accounts"] = kept
                changed = True
                notes.append(f"запись удалена из wrapper-accounts ({path.name})")
        if changed:
            _cs._atomic_write_yaml(path, data)
    return notes


def retire_apple_account(acct: dict, slot: int, reason: str) -> list[str]:
    """Полное снятие мёртвой Apple-учётки — то, ради чего всё и затевалось.

    Отличие от «остановить слот» (что делал сторож до 24.09): учётка уходит изо
    ВСЕХ мест, где она занимает ресурс — реестр снятых (чтобы не воскресла при
    следующем сохранении конфига), config/tokens, Docker-контейнер вместе с
    портом, мемо страны и карта аккаунт↔слот. Больше всего это похоже на
    `disable_deezer_arl` + `disable_soundcloud_token`, сложенные в один шаг:
    те же два письма (реестр ДО файла), тот же писатель конфига, тот же
    `_notify_app_config_changed()` в конце.

    Ничего не логинит и нового аккаунта на освободившееся место не ставит."""
    from . import apple_accounts as aa
    from . import retired_credentials as _retired
    from . import wrapper_pool as wp
    identity = wp.account_identity(acct)
    if not identity:
        return []
    digest = _retired._digest(identity)
    container = aa.container_for_slot(slot)
    notes: list[str] = []
    # Порядок как у Deezer: сначала реестр, потом файл.
    _retired.retire("apple_account", identity, reason or "учётка Apple мертва")
    notes += _drop_apple_account_from_files(digest, slot == 0)
    if aa.container_exists(container):
        disable_apple_slot("apple_account", container)
        notes.append(f"контейнер {container} удалён, порт {aa.slot_port(slot)} свободен")
    else:
        aa.release_container(container)
    aa.account_map_forget(digest=digest)
    if not _notify_app_config_changed():
        notes.append("⚠️ приложение не перечитало конфиг: пока оно живёт в памяти "
                     "со старой копией, первое же сохранение вернёт учётку — "
                     "реестр снятых не даст ей вернуться в маршрутизацию, но "
                     "перезапустите приложение")
    return notes


def apple_retire_guard(slot: int, klass: str, remaining: int, *,
                       hard_classes=APPLE_HARD_CLASSES) -> tuple[bool, str]:
    """Страж снятия. Возвращает (можно ли снимать, почему нет).

    a) `remaining` — сколько учётки ОСТАНЕТСЯ в конфиге после снятия. Нуль
       запрещён: автоматика не имеет права оставить пул пустым, даже если каждая
       учётка в нём мертва. Пустой список неотличим от «Apple не настроен», а
       узнаёт владелец об этом только на первой же задаче. Последняя ЖИВАЯ учётка
       под запретом и сама по себе: живая неудач не копит, а если живых не
       осталось вовсе, «пересадить пул с нуля» решает человек.
    d) Основной слот (0) снимается ТОЛЬКО по явному сигналу смерти —
       `subscription.active == false` или «account is disabled». Все прочие
       классы (device_limit, login_failed, мёртвый порт) на пороге в пять
       проходов дают отчёт, но не удаление: 24.09.2026 слот 0 ловил Invalid CKC
       на всём подряд при ЖИВОЙ подписке, и по старому поведению это означало бы
       потерю последней рабочей учётки пула."""
    if remaining <= 0:
        return False, ("последняя учётка Apple в конфиге: после снятия пул "
                       "опустеет — решение за владельцем")
    if slot == 0 and klass not in hard_classes:
        return False, (f"основной слот: сигнала смерти нет ({klass or 'не выяснили'}), "
                       f"нужны subscription.active=false или account_disabled")
    return True, ""


def _apple_note_state(key: str, state: str, reason: str, container: str) -> None:
    """Запомнить последний вердикт учётки — чтобы список в настройках показывал
    состояние, НЕ требуя docker/сети на каждый GET."""
    st = _load_state()
    entry = st.get(key) or {"streak": 0, "last_country": ""}
    entry["last_state"] = state
    entry["last_reason"] = reason or ""
    entry["container"] = container or ""
    entry["at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    st[key] = entry
    _save_state(st)


def apple_account_states(cfg: dict | None = None) -> list[dict]:
    """Состояние каждой НАСТРОЕННОЙ Apple-учётки для `/api/wrapper/accounts`.

    До 24.09.2026 список строился по `_configured_accounts` (шесть записей), а
    диагностике видели только контейнеры (три) — мёртвая учётка выглядела
    «обычной строкой» независимо от того, мертва она или никто её не проверял.
    Здесь честные четыре состояния, и «не проверена» отделена от «мертва».

    Секретов не возвращает: метка маскируется (`wrapper_pool.display_label`),
    ни Apple ID целиком, ни токен, ни хеш опознания наружу не идут."""
    from . import apple_accounts as aa
    from . import retired_credentials as _retired
    from . import wrapper_pool as wp
    if cfg is None:
        cfg = _load_raw_config()
    st = _load_state()
    out: list[dict] = []
    for i, acct in enumerate(wp._configured_accounts(cfg) or []):
        identity = wp.account_identity(acct)
        key = f"apple_account:{_ident(identity)}"
        entry = st.get(key) or {}
        retired = bool(identity) and _retired.is_retired("apple_account", identity)
        streak = int(entry.get("streak") or 0)
        if retired:
            state = "removed"
        elif streak >= APPLE_THRESHOLD:
            state = "dead"
        elif streak > 0:
            state = "failing"
        else:
            state = entry.get("last_state") or "unverified"
            if state == "idle":
                state = "unverified"
        out.append({
            "slot": i,
            "state": state,
            "streak": streak,
            "threshold": APPLE_THRESHOLD,
            "reason": entry.get("last_reason") or "",
            "container": entry.get("container") or aa.container_for_slot(i),
            "label": wp.display_label(acct),
            "kind": acct.get("kind") or "login",
            "checked_at": entry.get("at") or "",
        })
    return out


def check_all_apple_slots(threshold: int = APPLE_THRESHOLD) -> list[str]:
    """Жизненный цикл ВСЕХ настроенных Apple-учёток: неудачи, снятие, порты.

    Обходим НАСТРОЙКИ, а не `docker ps` (см. docstring выше): у владельца шесть
    учётки в списке, контейнеров три, и ровно поэтому учётка
    «device_limit_2026-08-01» числилась живой с августа — проверялись только
    поднятые контейнеры.

    Четыре исхода проверки, и два из них НЕ наказуемы:
      * `idle` — контейнер штатно погашен сборщиком
        простоя (5 минут без дела). Неудача здесь означала бы, что пул
        вымирает за ту ночь, когда никто ничего не качал.
      * `unverified` — спросить не удалось (нет контейнера, молчит amp-api,
        слот ещё прогревается). Честное «не проверена» в списке настроек.
    Накажутся только явные классы смерти: device_limit, login_failed,
    «account is disabled», subscription.active == false, мёртвый порт, 403 по
    media-user-token.

    Стража (см. `apple_retire_guard` и детектор общей аварии `global_klass`
    ниже в этом же теле) переживает только
    то, что дошло до порога; dry-run (`--no-fix`) не пишет, не снимает и не
    удаляет ничего."""
    from . import apple_accounts as aa
    from . import retired_credentials as _retired
    from . import wrapper_pool as wp
    lines: list[str] = []
    cfg = _load_raw_config()
    accounts = wp._configured_accounts(cfg) if cfg else []
    dry = dry_run()
    pending_notes: list[str] = []

    verdicts: list[dict] = []
    for i, acct in enumerate(accounts):
        try:
            v = apple_account_verdict(acct, i)
        except Exception as e:  # noqa: BLE001
            # Ошибка проверки — это «не спросили», а не «мертва»: чекер обязан
            # досмотреть до конца и не имеет права жечь streak за свой сбой.
            v = _verdict("unverified", reason=f"ошибка проверки: {type(e).__name__}")
        v["slot"] = i
        v["kind"] = str(acct.get("kind") or "login")
        verdicts.append(v)

    live_logins = sum(1 for v in verdicts
                      if v["state"] == "alive" and v["kind"] == "login")

    # b) Общая авария ≠ смерть каждой учётки. Если ВСЕ проверяемые учётки
    #    умерли ОДНИМ И ТЕМ ЖЕ симптомом — это сеть, Apple или Docker, а не
    #    шесть трупов за один вечер. Считаем только «failed»: их должно быть не
    #    меньше двух, иначе правило совпало бы с «осталась одна учётка».
    judged = [v for v in verdicts if v["state"] in ("alive", "failed")]
    failed = [v for v in verdicts if v["state"] == "failed"]
    global_klass = ""
    if len(failed) >= 2 and len(failed) == len(judged) and \
            len({v["klass"] for v in failed}) == 1:
        global_klass = failed[0]["klass"]
        lines.append(
            f"🌐 Apple: {len(failed)} учёток одновременно с одним симптомом "
            f"(«{_REASON_TEXT.get(global_klass, global_klass)}») — похоже на общую "
            f"аварию (сеть/Apple/Docker), неудачи НИКОМУ не засчитываем")

    lines += _sweep_apple_orphans(accounts, dry=dry, live_logins=live_logins)

    if not accounts:
        return lines

    known_digests = set()
    for acct, v in zip(accounts, verdicts):
        identity = wp.account_identity(acct)
        key = _ident(identity)
        state_key = f"apple_account:{key}"
        shown = wp.display_label(acct)
        container = aa.container_for_slot(v["slot"]) if v["kind"] == "login" else ""
        digest = _retired._digest(identity)
        known_digests.add(digest)
        if not dry:
            aa.account_map_put(digest, slot=v["slot"], container=container,
                               kind=v["kind"], label=shown)
            _apple_note_state(state_key, v["state"], v["reason"], container)

        if v["state"] == "alive":
            record_check("apple_account", key, True, country=v.get("country", ""),
                         threshold=threshold)
            continue
        if v["state"] in ("idle", "unverified"):
            if v["state"] == "unverified":
                lines.append(f"· Apple {shown} (слот {v['slot']}): не проверена — "
                             f"{v['reason']}")
            continue
        if global_klass:
            continue                      # страж (b): всё умерло одинаково

        veto = ""
        notes: list[str] = []

        def _allowed(_v=v, _rest=len(accounts) - 1) -> bool:
            nonlocal veto
            ok, veto = apple_retire_guard(_v["slot"], _v["klass"], _rest)
            return ok

        streak, retired = record_check(
            "apple_account", key, False, country=v.get("country", ""),
            reason=v["reason"] or _REASON_TEXT.get(v["klass"], "не отвечает"),
            threshold=threshold, can_retire=_allowed, prune=True,
            disable_fn=lambda _k, _key, _a=accounts[v["slot"]], _s=v["slot"], _v=v, _n=notes:
            _n.extend(retire_apple_account(_a, _s,
                                           _v["reason"] or _REASON_TEXT.get(_v["klass"], ""))))
        if retired:
            lines.append(f"💀 Apple {shown} (слот {v['slot']}): {threshold} неудач "
                         f"подряд ({v['reason']}) — учётка СНЯТА")
            lines += [f"   ↳ {n}" for n in notes]
        elif veto:
            lines.append(f"⛔ Apple {shown} (слот {v['slot']}): порог {threshold} "
                         f"достигнут ({v['reason']}), снятия нет — {veto}")
        elif streak > 0:
            lines.append(f"⚠️ Apple {shown} (слот {v['slot']}): {streak}/{threshold} "
                         f"неудачных проверок подряд ({v['reason']})")

    if not dry:
        _prune_apple_map(known_digests)
    return lines


def _prune_apple_map(keep: set) -> None:
    """Вычистить из карты учётки, которых больше нет в конфиге (владелец убрал
    их руками либо снятие уже отработало)."""
    from . import apple_accounts as aa
    d = aa.account_map()
    for digest in [k for k in d["accounts"] if k not in keep]:
        aa.account_map_forget(digest=digest)
