"""tests/test_installers_contract.py — офлайн-контракт системы автоустановщиков
(страж владельца 25.09.2026: «перестать переживать за автоустановщики»).

Ни одного сетевого запроса: тест читает реестр `ripster/installers_manifest.py`
и ИСХОДНИКИ установщиков. Сетевую половину сторожит tools/check_installers.py
fast-режим (еженедельно из healthcheck), живые установки — его же --full.

Что доказывается:
  1. реестр полный и непротиворечивый (id, пины, таймауты, hash_source);
  2. у каждого установщика есть текст отказа ru+en с указанием, что делать;
  3. каждый download_file/download_file_no_ssl в ripster/ передаёт iid из реестра
     (значит получает таймаут и сверку хэша из него же);
  4. каждый irun с git/npm/pip/winget/go имеет потолок timeout — вечного
     «кнопка крутится» больше нельзя завести правкой файла;
  5. pip-пины реестра не противоречат VERSIONS.md / requirements.txt.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from ripster.installers_manifest import (INSTALLERS, BY_ID, FAILURE_MSGS,
                                         Installer)

VALID_KINDS  = {"archive", "installer", "git", "pip", "oci", "none"}
VALID_HASHES = {"pinned", "provider-api", "sidecar", "none-published",
                "git-head", "pypi-pin", "oci-digest"}
HEX64        = re.compile(r"^[0-9a-fA-F]{64}$")


def _src(*rel):
    with open(os.path.join(ROOT, *rel), encoding="utf-8") as f:
        return f.read()


# ─── 1. реестр ────────────────────────────────────────────────────────────────

def test_registry_ids_unique():
    ids = [i.id for i in INSTALLERS]
    assert len(ids) == len(set(ids)), f"дубли id: {ids}"
    assert set(BY_ID) == set(ids)


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda i: i.id)
def test_installer_has_pin_timeout_kind(installer):
    assert installer.pin.strip(),               f"{installer.id}: пустой пин"
    assert installer.timeout_s > 0,             f"{installer.id}: таймаут не задан"
    assert installer.kind in VALID_KINDS,       f"{installer.id}: kind {installer.kind}"
    assert installer.hash_source in VALID_HASHES, f"{installer.id}: hash_source"
    assert installer.msg_key.startswith("setup.err."), f"{installer.id}: msg_key вне неймспейса"


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda i: i.id)
def test_pinned_sha256_is_real(installer):
    """hash_source=pinned без валидного 64-hex — это ложная безопасность:
    установщик либо не сверит ничего, либо отвергнет живой файл (так и было с
    плейсхолдером ffmpeg и нулями SpotiFLAC до 25.09.2026)."""
    if installer.hash_source == "pinned":
        assert HEX64.match(installer.sha256 or ""), \
            f"{installer.id}: pinned без реального SHA256"
    assert not installer.sha256 or HEX64.match(installer.sha256), \
        f"{installer.id}: sha256 не hex64 и не пусто"


# ─── 2. тексты отказа ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda i: i.id)
def test_failure_message_registered_ru_en(installer):
    body = FAILURE_MSGS.get(installer.msg_key)
    assert body, f"{installer.id}: msg_key {installer.msg_key} не в FAILURE_MSGS"
    for lang in ("ru", "en"):
        text = (body.get(lang) or "").strip()
        assert len(text) > 20, f"{installer.id}: текст {lang} пустой/непрочитываемый"
        # «что делать»: ссылка, winget/pip/docker или «повтори»
        assert re.search(r"https?://|winget|pip |docker|повтори|пересоздай|Перезапусти|retry|restart|reinstall|обнов|update",
                         text, re.I), \
            f"{installer.id}: в {lang}-тексте нет указания, что делать"


def test_no_orphan_failure_messages():
    used = {i.msg_key for i in INSTALLERS}
    orphan = set(FAILURE_MSGS) - used
    assert not orphan, f"сиротские тексты отказа (установщик удалён, ключ оставлен): {orphan}"


# ─── 3. продукт обязан пользоваться реестром ──────────────────────────────────

LONG_CMDS    = re.compile(r"clone|pull|npm|pip|winget|go mod|sdkmanager|virtualenv|venv|mod download|powershell")
_SETUP_PY   = os.path.join(ROOT, "ripster", "setup", "__init__.py")
_ROUTES_PY  = os.path.join(ROOT, "ripster", "routes", "setup.py")


def _call_nodes(path, names):
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            fname = (fn.attr if isinstance(fn, ast.Attribute)
                     else fn.id if isinstance(fn, ast.Name) else "")
            if fname in names:
                yield node


def test_every_download_declares_registry_iid():
    """Скачать без iid — значит скачать без таймаута и без сверки хэша из
    реестра. Именно так Living-установщик вешал кнопку навсегда (25.09)."""
    bad = []
    for path in (_SETUP_PY, _ROUTES_PY):
        for call in _call_nodes(path, {"download_file", "download_file_no_ssl"}):
            kw = {k.arg for k in call.keywords}
            if "iid" not in kw:
                bad.append(f"{os.path.basename(path)}:{call.lineno}")
    assert not bad, f"скачивания вне реестра (нет iid=): {bad}"


def test_registry_iids_are_known():
    """Каждый iid= из исходников обязан быть в реестре, иначе таймаут/хэш
    молча берутся дефолтные, а страж про установщик не знает."""
    src = _src("ripster", "setup", "__init__.py") + _src("ripster", "routes", "setup.py")
    used = set(re.findall(r'iid=["\']([\w.-]+)["\']', src))
    unknown = used - set(BY_ID)
    assert not unknown, f"iid не зарегистрированы в installers_manifest: {unknown}"


def test_git_npm_pip_winget_runs_have_timeout():
    """Каждый irun с сетевой/долгой командой обязан иметь потолок timeout —
    после 25.09 ни один вечный `git clone`/`npm install` не переживёт ревью."""
    LONG_CMDS = re.compile(r"clone|\bpull\b|npm|pip|winget|go mod|sdkmanager|virtualenv|venv|mod download|powershell")
    bad = []
    for path in (_SETUP_PY, _ROUTES_PY):
        for call in _call_nodes(path, {"irun"}):
            kw = {k.arg for k in call.keywords}
            if "timeout" in kw:
                continue
            cmd_src = ast.unparse(call.args[0]) if call.args else ""
            if LONG_CMDS.search(cmd_src):
                bad.append(f"{os.path.basename(path)}:{call.lineno} {cmd_src[:60]}")
    assert not bad, f"долгие команды без timeout: {bad}"


# ─── 4. пины против документов ────────────────────────────────────────────────

def test_pip_pins_match_versions_md():
    vm = _src("VERSIONS.md")
    doc = dict(re.findall(r"\|\s*\*\*([\w.-]+)\*\*\s*\|\s*`==([\w.]+)`", vm))
    for i in INSTALLERS:
        if i.kind != "pip":
            continue
        for m in re.finditer(r"([A-Za-z0-9_.-]+)\s*==\s*([0-9][\w.]*)", i.pin):
            pkg, pin = m.groups()
            if pkg in doc:
                assert doc[pkg] == pin, (
                    f"{i.id}: {pkg} запинен как {pin}, а VERSIONS.md предписывает "
                    f"{doc[pkg]} — одно из двух врёт")


def test_requirements_txt_matches_versions_md():
    """requirements.txt — то, что ставит свежую машину; он не имеет права
    противоречить VERSIONS.md (конфликт 2.1.0 в lock стоил владельцу сломанного
    Tidal на каждой чистой установке)."""
    vm  = _src("VERSIONS.md")
    rq  = _src("requirements.txt")
    doc = dict(re.findall(r"\|\s*\*\*([\w.-]+)\*\*\s*\|\s*`==([\w.]+)`", vm))
    req = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([^\s#]+)", rq, re.M))
    for pkg, want in doc.items():
        if pkg in req:
            assert req[pkg] == want, \
                f"requirements.txt {pkg}=={req[pkg]} против VERSIONS.md =={want}"


# ─── 5. зеркало github_setup ──────────────────────────────────────────────────

_MIRROR_SETUP   = os.path.join(ROOT, "github_setup", "ripster", "setup", "__init__.py")
_MIRROR_ROUTES  = os.path.join(ROOT, "github_setup", "ripster", "routes", "setup.py")


def test_mirror_manifest_is_identical():
    """Реестр установщиков обязан совпадать с зеркалом byte-for-byte: страж и
    продукт у пользователей зеркала должны говорить про ОДНИ и те же пины."""
    mirror = os.path.join(ROOT, "github_setup", "ripster", "installers_manifest.py")
    if not os.path.exists(mirror):
        pytest.skip("зеркало без реестра")
    assert _src("ripster", "installers_manifest.py") == open(mirror, encoding="utf-8").read(), \
        "github_setup/ripster/installers_manifest.py отстаёт от продукта"


def test_mirror_installer_files_keep_the_guarantees():
    """Зеркалу не обязательна побайтовая свежесть по чужим правкам, но гарантии
    установщиков (iid из реестра, timeout на долгих командах) обязан нести и оно:
    пользователи зеркала ставят те же бинари тем же кодом."""
    paths = [p for p in (_MIRROR_SETUP, _MIRROR_ROUTES) if os.path.exists(p)]
    assert paths, "в зеркале нет установочных файлов — проверить структуру"
    bad_dl = []
    for path in paths:
        for call in _call_nodes(path, {"download_file", "download_file_no_ssl"}):
            kw = {k.arg for k in call.keywords}
            if "iid" not in kw:
                bad_dl.append(f"{path}:{call.lineno}")
        for call in _call_nodes(path, {"irun"}):
            kw = {k.arg for k in call.keywords}
            cmd_src = ast.unparse(call.args[0]) if call.args else ""
            if "timeout" not in kw and LONG_CMDS.search(cmd_src):
                bad_dl.append(f"{path}:{call.lineno} irun без timeout")
    assert not bad_dl, f"зеркало растеряло гарантии установщиков: {bad_dl}"
