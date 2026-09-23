"""Один артист подписки — одна личность, подтверждённая данными.

Проблема, которую это решает (жалоба владельца, живая с 03.09.2026): радар
подмешивал релизы ЧУЖИХ артистов-однофамильцев («BOP», «Solomon Grey»). Прежняя
сверка решала «кто это» по ИМЕНИ — точное совпадение нормализованной строки, при
неоднозначности жребий по популярности. Данные решают иначе: два каталога говорят
об одном человеке ровно тогда, когда у них ОБЩИЕ РАБОТЫ (те же ISRC/UPC,
пересечение дискографий, общие лейблы). Имя — только способ найти КАНДИДАТА, но
не доказательство. Резолвер «имя → id» из `artist_xref` удалён: ловить нечего.

Здесь живут:
  classify()      — чистое решение «тот / не тот / не знаю» по признакам;
  home_show()     — пускать ли релиз со страницы подписки (лепка Apple-страниц);
  bind()/bind_pass() — сетевой обход: кандидаты по имени → подтверждение
                      пересечением → карта подтверждённых id ХРАНится на самой
                      подписке (entry["identity"]["services"]).

Принципы (ripster-adaptive-modules):
  • «не знаю» — ответ: релизы, приписанные подписке НЕ её собственным id, по
    неподтверждённой личности не показываются, а подписка помечается владельцу;
    сама по себе незакрытая сверка НЕ делает артиста «чужим» — чужим его делает
    противоречие с подтверждённым профилем. Держать в ленте карточку, у которой
    на подписку указывает ОДНА СТРОКА ИМЕНИ, когда её личность уже разбирали и
    ни одна витрина не подтвердила (`pending`), — значит оставить ровно ту
    дыру, из-за которой жалоба живёт: «BOP» pending на четырёх витринах, а его
    dnb-релиз в подписке на рэпера (21.09.2026). Такой карточке нечем
    подтверждать принадлежность, поэтому `pending` для неё = не показывать
    (`name_claim_shows`); карточки по подтверждённому id это не касается;
  • провал сети не кэшируется (pending протухает и перепроверяется);
  • никаких списков имён/жанров: признаки берутся из каталогов;
  • доказательством личности НЕ служит работа, под которой стоит толпа
    (сборник, диджей-мик, саундтрек): такие строки исключены из пересечения
    дискографий (`compilations.proves_identity`), и одного выжившего
    совпадения мало — нужно два;
  • каждый ответ объясним: у каждого id лежит evidence с числами.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional

from . import compilations as _comps
from .artist_xref import norm

CONFIRMED = "confirmed"
PENDING = "pending"
CONFLICT = "conflict"

# Подтверждённый id не перепроверяем; неразобранное — раз в две недели,
# ровно как чужие промахи у artist_xref.
_RETRY_TTL = 14 * 24 * 3600.0
# Сколько подписок привязывать за один проход радара (tempo: только на скан).
DEFAULT_BIND_CAP = 20

_TITLES_CAP = 500


def tkey(title: str) -> str:
    """Ключ релиза для сверки между каталогами: «X - Single» и «X (Single)» —
    одна работа, витрины называют её по-разному."""
    t = re.sub(r"\s*[-–—]\s*(single|ep)\s*$", "", str(title or ""), flags=re.I)
    t = re.sub(r"\s*\((single|ep)\)\s*$", "", t, flags=re.I)
    return norm(t)


_LABEL_YEAR_RE = re.compile(r"\b(19|20)\d{2}s?\b")
# Юрдыка авторской строки: то, что витрины пишут вокруг лейбла, а не сам лейбл.
_LABEL_BOILER_RE = re.compile(
    r"[℗©]"
    r"|\b(?:all\s+)?rights\s+reserved\b|\breserv\w*\b|\bcourtesy\b|\bincl\b|\bfeat\.?\b"
    r"|\b(?:manufactur|market|distribut|publish|copyright|control|licens)\w*\b"
    r"|\b(?:under\s+)?(?:exclusive|sole)\s+licen[sc]e[ds]?\b"
    r"|\bdivision\b|\bfloor\b|\bvia\b"
    r"|\ban?\s+(?:new\s+)?(?:release|recording|album)\b|\b(?:release|recordings)\s+(?:of|by)\b"
    r"|\bpresents\b",
    re.IGNORECASE,
)
_LABEL_LEGAL_RE = re.compile(
    r"\b(?:ltd|llc|llp|inc|incorporated|co|corp|corporation|gmbh|mbH|ag|sarl|sas|sa|bv|nv|"
    r"ooo|pty|plc|srl|oy|ab|ltda|company|companies|holdings?|enterprises?|international|"
    r"management|group|music|entertainment)\b\.?",
    re.IGNORECASE,
)
_LABEL_STOP_RE = re.compile(r"\b(?:a|an|the|to|from|of|at|by|and|for|in|on|with|under|is)\b")


def label_key(label: str, artist: str = "") -> str:
    """Лейбл как признак личности — без каймы авторских прав.

    Витрины кладут в поле «label» строку правообладания целиком:
    «under exclusive licence to warner music uk limited an atlantic records
    uk 2024 fred gibson» и та же строка 2025-го — это один лейбл и один
    человек, но как ключ кластера это два разных ярлыка. На такой кайме
    needs_owner плодил «второго человека» у Fred again.., Ólafur Arnalds,
    Moby и остальных, где менялся только год (21.09.2026 — из 10 подписок в
    списке владельца настоящих splits было одна).

    Поэтому из строки выкидывается всё, что описывает не бренд: годы,
    «under exclusive licence to», «a division of», дистрибьюторов, юридические
    хвосты и имя самого артиста (в авторской строке он — правообладатель, а не
    издатель). Пустой результат — значит из строки не осталось ничего, кроме
    имени артиста: оставляем нормализованную строку как есть, чтобы кластеры
    собственного imprint не сливались в одну корзину.
    """
    raw = norm(label)
    k = _LABEL_BOILER_RE.sub(" ", raw)
    k = _LABEL_YEAR_RE.sub(" ", k)
    own = norm(artist)
    if own and len(own) > 2:
        k = re.sub(r"(?<!\w)" + re.escape(own) + r"(?!\w)", " ", k)
        for part in {p for p in own.split() if len(p) > 3}:
            k = re.sub(r"(?<!\w)" + re.escape(part) + r"(?!\w)", " ", k)
    k = _LABEL_LEGAL_RE.sub(" ", k)
    k = _LABEL_STOP_RE.sub(" ", k)
    k = re.sub(r"[^\w\s]", " ", k)
    k = re.sub(r"\s+", " ", k).strip()
    return k or raw


def _labels(rows: list) -> set:
    return {norm(str(r.get("label") or "")) for r in rows if r.get("label")}


def _evidence_split(rows: list) -> tuple[set, set]:
    """Заголовки витрины: (годные как доказательство личности, годные только
    как «у двух людей есть релиз с таким названием»).

    Сборник, диджей-мик и саундтрек несут чужие работы под одной обложкой,
    поэтому совпадение по ним — совпадение названий, а не людей: их кладём во
    вторую корзину и в вердикт не пускаем.
    """
    ok, neutral = set(), set()
    for r in rows or []:
        title = str(r.get("title") or "").strip()
        k = tkey(title)
        if not k:
            continue
        bucket = ok if _comps.proves_identity(
            album_type=str(r.get("type") or r.get("album_type") or ""),
            album_artist=str(r.get("album_artist") or ""),
            title=title,
            track_artist=str(r.get("artist") or "")) else neutral
        bucket.add(k)
    return ok, neutral


def _shared_titles(a_rows: list, b_rows: list) -> tuple[list, list]:
    """(общие работы, общие ТОЛЬКО-сборники) — первое доказывает, второе нет."""
    a_ok, a_bad = _evidence_split(a_rows)
    b_ok, b_bad = _evidence_split(b_rows)
    shared = a_ok & b_ok
    return sorted(shared), sorted((a_bad & b_bad) - shared)


def _shared_field(a_rows: list, b_rows: list, field: str) -> int:
    b = {str(r.get(field) or "").lstrip("0") for r in b_rows if r.get(field)}
    a = {str(r.get(field) or "").lstrip("0") for r in a_rows if r.get(field)}
    return len((a & b) - {""})


# Идентификаторы выпуска живут НЕОДНИМИ пространствами имён, и путать их
# нельзя: `collectionId` Apple — каталожный номер ЯБЛОКА (у «Manos Abiertas al
# Cielo» это 6773048157), а не штрихкод, и сравнивать его с UPC бессмысленно.
# Поля одного namespace сводятся в одну корзину, потому что витрины называют
# один и тот же номер по-разному: Qobuz отдаёт `upc`, Tidal — `ean`, Deezer —
# `barcode`, Spotify — `upc` наруже, а внутри — id альбома.
_ID_NS: dict[str, tuple] = {
    "upc": ("upc", "ean", "barcode"),
    "isrc": ("isrc",),
    "album": ("album", "album_id"),
}


def _ok_rows(rows: list) -> list:
    """Строки, годные как доказательство личности (не сборники/миксы/саундтреки).

    Номера выпуска под чужой обложкой — чужие номера: делюкс сборника несёт
    треки десяти человек, и по его штрихкоду нельзя решить, что два каталога
    говорят об одном человеке.
    """
    out = []
    for r in rows or []:
        title = str(r.get("title") or "").strip()
        if _comps.proves_identity(
                album_type=str(r.get("type") or r.get("album_type") or ""),
                album_artist=str(r.get("album_artist") or ""),
                title=title,
                track_artist=str(r.get("artist") or "")):
            out.append(r)
    return out


def _idbag(rows: list, ns: str) -> set:
    out = set()
    for r in _ok_rows(rows):
        for f in _ID_NS[ns]:
            v = re.sub(r"\s+", "", str(r.get(f) or "")).lstrip("0")
            if len(v) >= 6:
                out.add(v)
    return out


def _shared_ids(a_rows: list, b_rows: list) -> dict:
    """Сколько ОБЩИХ выпусков два каталога знают по номерам (по namespace)."""
    return {ns: len(_idbag(a_rows, ns) & _idbag(b_rows, ns)) for ns in _ID_NS}



def _barcode_of(r: dict) -> str:
    """Любой идентификатор КОНКРЕТНОГО выпуска: штрихкод, ISRC или номер
    коллекции. Витрины не отдают ISRC/UPC в списке релизов, поэтому узкий
    артист с единственным релизом дома оставался бы без доказательства вовсе."""
    return (str(r.get("upc") or "").lstrip("0")
            or str(r.get("isrc") or "").lstrip("0")
            or str(r.get("collection") or "").lstrip("0"))


def _shared_title_with_barcode(a_rows: list, b_rows: list) -> int:
    """Сколько общих НЕсборниковых работ названы одинаково и обведены у ЯКОРЯ
    идентификатором выпуска. Название без номера — совпадение слов; название,
    которое якорь знает под своим collectionId/ISRC, — та же самая работа, и
    второй каталог, повторяющий её заголовок, говорит о том же человеке.

   Сборник тут уже отсеян: `_shared_titles` возвращает только годные
    работы, поэтому штрихкод чужого диджей-мита личность подтвердить не может.
    """
    out = 0
    for key in _shared_titles(a_rows, b_rows)[0]:
        if any(tkey(r.get("title", "")) == key and _barcode_of(r)
               for r in a_rows or []):
            out += 1
    return out


def classify(anchor_rows: list, cand_rows: list) -> dict:
    """Тот же ли это человек. Чистая функция, сеть не трогает.

    anchor_rows — релизы подписки; cand_rows — релизы кандидата из другой
    витрины. Строки: {title, label?, upc?, isrc?, album?}. Возвращает
    {verdict, evidence}; «не знаю» — полноценный вердикт, а не отказ.
    """
    titles, neutral = _shared_titles(anchor_rows, cand_rows)
    ids = _shared_ids(anchor_rows, cand_rows)
    upc, isrc, album = ids["upc"], ids["isrc"], ids["album"]
    labeled = _shared_title_with_barcode(anchor_rows, cand_rows)
    labels_shared = bool(_labels(anchor_rows) & _labels(cand_rows))
    an, cn = len(anchor_rows), len(cand_rows)
    ev = {"titles": titles[:12], "titles_n": len(titles),
          "titles_shared_compilations": len(neutral),
          "upc": upc, "isrc": isrc, "album_ids": album, "ids": ids,
          "titled_by_release_id": labeled,
          "labels_shared": labels_shared,
          "anchor_n": an, "cand_n": cn}
    if upc or isrc:
        return {"verdict": CONFIRMED, "evidence": ev}
    # ДВЕ общие работы. Одна — это и есть тот класс совпадений, на котором
    # прежняя сверка вязла: узкий артист, у которого дома один релиз,
    # сшивался с полным каталогом снаружи по одному названию, а этим
    # названием оказывался мик либо фит (21.09.2026: «Solomon Grey» +
    # «Group Therapy Anjuna25 Special», 65 подтверждённых связок в складе
    # держатся на одном совпадении или на сборнике).
    if len(titles) >= 2:
        return {"verdict": CONFIRMED, "evidence": ev}
    # ДВЕ работы, которые обе стороны знают под ОДНИМ id выпуска внутри
    # витрины: это дубли профиля одного человека (так «Noisia», «Mat Zo» и
    # Robert Hood живут по два-три id на одном сервисе). Разъединить их —
    # значит потерять релизы настоящего артиста, а это ошибка дороже.
    if album >= 2:
        return {"verdict": CONFIRMED, "evidence": ev}
    # Одно название — ещё не доказательство; название, которое якорь знает под
    # своим номером выпуска (collectionId/ISRC/UPC), — доказательство: это
    # конкретный релиз, а не набор одноимённых.
    if labeled:
        return {"verdict": CONFIRMED, "evidence": ev}
    if not titles and not neutral and an >= 3 and cn >= 3:
        return {"verdict": CONFLICT, "evidence": ev}
    return {"verdict": PENDING, "evidence": ev}


# ── Хранилище: карта подтверждённых id живёт НА подписке ─────────────────────
# Единый владелец факта — запись вишлиста (entry["identity"]), как и требует
# правило «у каждого факта один хозяин». Никаких параллельных реестров.

# Версия ПРАВИЛ, по которым складывалась личность. Правило ужесточается, а
# уже записанное «confirmed» само не поумнеет: 22.09.2026 разбор склада показал,
# что 354 из 358 подписок были привязаны ДО появления кластерной цензуры и с
# пустым `witness_overlaps`, — то есть ни одно строгое правило по ним не
# проходило, и склейка жила вечно. Отметка версии — единственный способ мягко
# загнать старые вердикты обратно в перепроверку (темпом радара, не одним
# прогоном), и она же отвечает за честный счётчик «сколько склеек мы разобрали».
# Версия 4 (22.09.2026): носителей стали считать по ОБЪЕДИНЁННЫМ работам
# личности, а не по каждому импринту (карьера, разложенная по четырём лейблам,
# без этого не получала ни одного чистого свидетеля), и с id однофамильца стало
# возможно снять подтверждение (`namesake_demotions`).
IDENTITY_RULE_VERSION = 4

def identity_of(entry: dict) -> dict:
    ident = entry.get("identity")
    return ident if isinstance(ident, dict) else {}


def confirmed_id(entry: dict, service: str) -> str:
    rec = (identity_of(entry).get("services") or {}).get(service) or {}
    return str(rec.get("id") or "") if rec.get("status") == CONFIRMED else ""


def confirmed_ids(entry: dict, service: str) -> set:
    """Все id ЭТОГО ЖЕ человека в витрине: принятый плюс доказанные дубли.

    Витрины плодят вторые профили на одного человека («Noisia», «Mat Zo»,
    Robert Hood живут по два-три id), и-release с соседнего id — законный
    релиз подписки. Пускать только один id — та же ошибка в другую сторону:
    настоящий артист теряется из ленты.
    """
    rec = (identity_of(entry).get("services") or {}).get(service) or {}
    if rec.get("status") != CONFIRMED:
        return set()
    return {str(x) for x in [rec.get("id")] + list(rec.get("aliases") or []) if x}


def confirmed_map(entries: list, service: str) -> dict:
    """{имя подписки: id} только по ПОДТВЕРЖДЁННЫМ связкам."""
    out = {}
    for e in entries:
        cid = confirmed_id(e, service)
        if cid:
            out[str(e.get("name") or "").strip()] = {"id": cid,
                                                     "name": e.get("name", "")}
    return out


def rules_stale(entry: dict) -> bool:
    """Личность собрана по БОЛЕЕ СТАРЫМ правилам, чем те, что в коде сейчас.

    Не «мы сомневаемся», а «эти данные проверялись другим алгоритмом»: чужую
    склейку, записанную в сентябре, нынешняя цензура не увидит, пока её не
    пересобрать.
    """
    return int(identity_of(entry).get("rule_version") or 0) < IDENTITY_RULE_VERSION


def services_bound(entry: dict) -> bool:
    ident = identity_of(entry)
    recs = (ident.get("services") or {})
    if not recs:
        return False
    if rules_stale(entry):
        # Прежний вердикт пересобирается ЦЕЛИКОМ, а не дописывается: иначе
        # «confirmed» из старой версии правила оставалось бы подтверждённым,
        # сколько бы раз мы его ни перепроверяли.
        return False
    if any((r or {}).get("status") == PENDING for r in recs.values()):
        return (time.time() - float(ident.get("ts") or 0)) < _RETRY_TTL
    return True


# ── Пускать ли релиз ──────────────────────────────────────────────────────────

_NO_ANCHOR = object()


def _anchor_of(entry: dict):
    """Якорь подписки — то, что владелец качал/отмечал/слушал.

    Считается вне нашего круга (файлы владельца, локальные базы) и обязан не
    уметь ломать ленту: нет каталога, нет базы, битый JSON — «не знаем», и
    дальше решает профиль витрины.
    """
    try:
        from . import owner_anchor
        return owner_anchor.for_entry(entry)
    except Exception:                                          # noqa: BLE001
        return None


def _home_families(anchor: dict) -> set:
    """Семьи якоря вместе с совместимыми: витрины мажут через релиз, и «Trance»
    якоря не делает «House» чужим (см. `owner_anchor.FAMILY_COMPAT`)."""
    from . import owner_anchor
    fams = set(anchor.get("families") or ())
    if not fams:
        return fams
    out = set(fams)
    for cand, _pats in owner_anchor.GENRE_FAMILIES:
        if any(owner_anchor.compatible(cand, f) for f in fams):
            out.add(cand)
    return out


def anchor_show(entry: dict, rel: dict, anchor: Optional[dict] = None) -> tuple:
    """Правило v5 «якорь владельца»: пускать ли релиз по ДЕЙСТВИЯМ владельца.

    Профиль витрины для этого непригоден — он склеен из всех однофамильцев
    страницы (у Aruna в жанрах подписки trance живёт рядом с telugu и gangsta
    rap). Якорь — объединение людей, которого у витрины нет: он лежит только в
    фонотеке владельца.

    Возвращает ("show"|"hide"|"", причина). "" — судить нечем, и это НЕ молчаливое
    «свой»: карточка проходит, но попадает в счётчик `anchor_unknown`, чтобы в
    отчёте «не тронуто» было отличимо от «проверено и принято».

    Правила решения — ровно те, что требует честность цензуры:
      • лейбл релиза лежит среди лейблов якоря — свой;
      • семья жанра релиза своя или совместимая — свой;
      • жанр ТОЧНЫЙ и чужой, а лейбл либо тоже чужой, либо неизвестен — чужой
        однофамилец;
      • жанр — ведро («Pop», «Dance», «Singer/Songwriter») или молчит: ведром
        не прячут, но чужой ЛЕЙБЛ при молчаливом жанре — уже два довода;
      • жанр неизвестен и лейбл неизвестен (или своих лейблов у якоря нет) —
        показываем и считаем: без доказательства не цензуруем;
      • диджейский микс — не работа артиста: его жанр — про сет, а не про
        личность, и прячем его только по чужому лейблу (`is_mix`, `label_foreign`).
    """
    from . import owner_anchor

    anchor = anchor if anchor is not None else _anchor_of(entry)
    if not anchor:
        return "", ""
    artist = str(entry.get("name") or "")
    fams = owner_anchor.families_of(rel)
    lbl = label_key(str(rel.get("label") or ""), artist)
    home_labels = set(anchor.get("labels") or ())
    home_fams = _home_families(anchor)
    # Микс — полка витрины, а не работа артиста: его жанр описывает сет,
    # который поставил кто-то ещё, и по нему нельзя сказать, ЧЕЙ это человек.
    # Замер 23.09: 16 из 27 «скрытых» карточек оказались диджейскими миксами
    # подписчиков (Skrillex → «Turn Up Select», жанр «Hip-Hop»). Правилам про
    # личность к ним не применять — кроме лейбла: он-то настоящий.
    mix = owner_anchor.is_mix(rel)

    if lbl and lbl in home_labels:
        return "show", "якорь: лейбл совпал с тем, что качал владелец"
    if fams and home_fams and not mix and (fams & home_fams):
        return "show", f"якорь: жанр {'/'.join(sorted(fams))} свой"

    foreign_label = owner_anchor.label_foreign(
        lbl, home_labels, artist, str(rel.get("label") or ""))
    if mix:
        if foreign_label:
            return "hide", (f"авто: сет выпущен лейблом, которого нет среди "
                            f"лейблов владельца ({rel.get('label')} против "
                            f"{'/'.join(sorted(home_labels))})")
        return "", ""

    if fams and anchor.get("families") and not (fams & home_fams):
        why = (f"авто: жанр/лейбл не совпадает с тем, что владелец качал "
               f"(жанр релиза {'/'.join(sorted(fams))}"
               f"{', лейбл ' + str(rel.get('label')) if lbl else ', лейбл неизвестен'}; "
               f"в якоре {'/'.join(sorted(anchor.get('families') or ()))})")
        return "hide", why
    if foreign_label:
        # Ведро («Pop», «Singer/Songwriter») само по себе не довод против
        # артиста — но в паре с чужим лейблом это два независимых
        # свидетельства, и именно так ловится однофамилец, которого витрина
        # положила в общую корзину.
        gtxt = ", ".join(owner_anchor.genres_of(rel)) or "жанра нет"
        why = (f"авто: {gtxt}"
               f"{'' if fams else ' — ведро, о личности молчит'}"
               f", а лейбл «{rel.get('label')}» не похож на те, что качал "
               f"владелец ({'/'.join(sorted(home_labels))})")
        return "hide", why
    return "", ""                                 # судить нечем — не считаем чужим


def home_show(entry: dict, rel: dict, anchor=_NO_ANCHOR) -> tuple[bool, str]:
    """Релиз со СТРАНИЦЫ подписки (Apple/Deezer/Qobuz склеивают однофамильцев в id).

    Порядок доказательств — от действия владельца к данным витрины:

      1. решение владельца (`choice.hide_titles`, возврат `show_titles`) — оно
         не обсуждается и не перекрывается ни авто-правилом, ни профилем;
      2. якорь владельца (правило v5, `anchor_show`) — единственный признак,
         который НЕ лепится из однофамильцев;
      3. профиль подписки: работу, известную чистой витрине, её лейбл, её жанр;
      4. судить нечем — не трогаем.

    Скрываем ровно то, против чего говорит доказанное, и карточка помечается
    владельцу (`hidden_add`), а не удаляется: склад остаётся складом.
    """
    from . import owner_anchor

    prof = identity_of(entry).get("profile") or {}
    choice = identity_of(entry).get("choice") or {}
    artist = str(entry.get("name") or "")
    titles = set(prof.get("titles") or [])
    hidden = set(prof.get("hidden_titles") or [])
    t = tkey(rel.get("title", "") or rel.get("name", ""))
    if t and t in {tkey(x) for x in (choice.get("show_titles") or ())}:
        return True, "владелец вернул вручную"
    if t and t in hidden:
        return False, "cluster not attested by any clean catalog"
    if anchor is _NO_ANCHOR:
        anchor = _anchor_of(entry)
    if anchor:
        dec, why = anchor_show(entry, rel, anchor)
        if dec == "show":
            owner_anchor.note("anchor_show")
            return True, why
        if dec == "hide":
            owner_anchor.note("anchor_hide")
            return False, why
        # Якорь есть, но против карточки он не высказался: «не тронуто» обязан
        # быть отличим от «проверено и принято».
        owner_anchor.note("anchor_unknown")
    else:
        owner_anchor.note("no_anchor")
    if not (titles or hidden):
        return True, ""                       # судить нечем — не трогаем
    if t and t in titles:
        return True, "discography"
    raw_lbl = norm(str(rel.get("label") or ""))
    lbl = label_key(raw_lbl, artist)
    stored = set(prof.get("labels") or [])
    # Профили, собранные до нормализации, держат лейблы строкой из каталога —
    # сравниваем обе формы, пока такие подписки не перепривязались.
    if lbl and (lbl in stored or raw_lbl in stored):
        return True, "label"
    g = {norm(str(x)) for x in owner_anchor.genres_of(rel)}
    if g & set(prof.get("genres") or []):
        return True, "genre"
    if not prof.get("merged"):
        return True, ""          # единичный промах на несклеенной странице — не цензура
    # Склеенная страница и релиз, не похожий ни на одно подтверждённое множество:
    # судим только по лейблу карточки — чужой кластер опознан именно им.
    if lbl and lbl not in stored and raw_lbl not in stored:
        return False, "label absent from the confirmed catalogs"
    return True, ""


def stitch_shows(rel: dict, entries: list) -> bool:
    """Релиз из чужой витрины попадает в ленту, только если среди его
    артистов есть id, ПОДТВЕРЖДЁННЫЙ для какой-нибудь подписки.
    Записи, пришедшие не по name-сшивке (собственные подписки сервисов —
    свои id аккаунта), помечены не были и проходят: gate ровно на «via_xref».
    """
    if not rel.get("via_xref"):
        return True
    svc = str(rel.get("service") or "")
    aid = str(rel.get("artist_id") or "")
    if not aid:
        return False
    return any(aid in confirmed_ids(e, svc) for e in (entries or []))


def _label_groups(rows: list, artist: str = "") -> dict:
    """Работы витрины, сгруппированные по её собственным лейблам.

    Лейбл — единственный признак, который разделяет людей внутри одного
    «артиста»: у Solomon Grey австралийский композитор ходит по Mercury
    Classics / Decca, а однофамилец — пачкой испанских синглов на «Spaceman
    Recordings». Списков имён нет: группы берутся из данных витрин.

    Ключ — `label_key`, а не строка из каталога: иначе один лейбл,
    подписанный в этом году «…2024 fred gibson», а в следующем «…2025 fred
    gibson», даёт два кластера и «второго человека» на пустом месте.
    """
    groups: dict[str, set] = {}
    for r in rows or []:
        lbl = label_key(str(r.get("label") or ""), artist)
        t = tkey(r.get("title", ""))
        if lbl and t:
            groups.setdefault(lbl, set()).add(t)
    return groups


# Носителем кластера считается витрина, у которой с этим кластером ≥2 общие
# НЕсборниковые работы: одна (пусть и верная) находка — то самое «single
# coincidence», на котором прежняя привязка вязла. Опровергает кластер витрина,
# которая знает ≥3 работы страницы и не знает ни одной работы кластера.
_SUPPORT_MIN = 2
_ATTEST_MIN = 3
# Доля работ страницы, которую должна занимать ЛИЧНОСТЬ, чтобы на неё стоило
# смотреть: 21.09.2026 восемь из десяти «склеенных» подписок дёргали владельца
# из-за осколка в 3–5 релизов (у Julia Michaels — саундтреки Disney, у Moby —
# один из импринтов). Два человека — это два тела работ, а не карьера и её
# боковая ветка.
_PERSON_SHARE = 0.25


def _families(gs: set) -> set:
    """Жанры, сведённые к СЕМЬЯМ. Витрины называют одну и ту же музыку
    по-разному: та же страница лепит «Dance» одним релизам trance-артиста,
    «Electronic» — другим, «House» — третьим. Мерить группы знаком по строке —
    значит находить «двух человек» у каждого электронщика с длинной карьерой
    (так и ловили Ronski Speed / Rinzen / Jiminy Hop / Michael Cassette
    21.09.2026). Семья берётся не из нового списка жанров, а из того же
    распознавателя, что у всего радара (`genre_resolver.is_dance`): всё
    танцевальное — одна семья; «hip hop» против «dnb», «classical» против
    «gospel» — разные.
    """
    from .genre_resolver import is_dance
    return {"dance" if is_dance(g) else g for g in gs}


def _token(name: str) -> str:
    """Носитель/свидетель одним ключом: `label_key` из «hot-rail» делает
    «hot rail», и если одну сторону склеить, а другую оставить — взаимное
    опровержение двух личностей пропадает на ровном месте (21.09.2026: так
    перестал делиться BOP)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(name or "").lower())).strip()


def _persons_by_labels(clusters: dict, drop=()) -> dict:
    """Профили без свидетелей: носители берутся строкой из самого кластера
    (`c["sup"]`/`c["con"]`). Так живут складские записи, у которых нет
    `witness_overlaps`; `drop` — склеенные витрины, вычитаемые из носителей.
    """
    drop_t = {_token(x) for x in drop}
    out: dict[frozenset, dict] = {}
    for lbl, c in (clusters or {}).items():
        sup = frozenset(_token(s) for s in (c.get("sup") or ())
                        if _token(s) not in drop_t)
        p = out.setdefault(sup, {"titles": set(), "genres": [], "absent": [],
                                 "labels": []})
        p["titles"] |= set(c.get("titles") or ())
        p["genres"].append(set(c.get("genres") or ()))
        p["absent"].append({_token(s) for s in (c.get("con") or ())})
        p["labels"].append(str(lbl))
    for p in out.values():
        # Жанр личности — то, чем её метят ВСЕ её кластеры: витрины мажут
        # через одну, и объединение жанров размывает границу между людьми.
        p["family"] = _families(set.intersection(*p["genres"]) if p["genres"] else set())
        p["con"] = set.intersection(*p["absent"]) if p["absent"] else set()
    return out


def _persons(clusters: dict, overlaps: Optional[dict] = None,
             drop=()) -> dict:
    """Кластеры, сведённые по НОСИТЕЛЯМ: {носители: {titles, genres, family,
    con, labels}}.

    Личность аттестует не лейбл, а набор витрин, которые этот лейбл знают.
    У одной карьеры импринты меняются (Mercury Classics, Decca, собственный
    Solomon Grey Music), а свидетели остаются те же — без сведения по носителям
    каждая такая ветка выглядит вторым человеком, и их набиралось по девять на
    страницу. Разные личности, наоборот, дают РАЗНЫЕ наборы витрин: испанского
    госпел-Solomon Grey не знает ни одна витрина, несущая австралийского.

    Носителей считаем по ОБЪЕДИНЁННЫМ работам личности, а не по каждому импринту
    в отдельности (22.09.2026, живой Solomon Grey). Австралийский композитор
    разложен витринами по четырём лейблам, и tidal — чистый свидетель, знающий
    ТОЛЬКО его, — попадает на каждый из четырёх импринтов ровно ОДНОЙ работой.
    Порог `_SUPPORT_MIN` на импринте его не видит, композитор остаётся без
    чистого свидетеля, взаимного опровержения не построить, и `_merged_witnesses`
    не может начать отклеивать deezer/qobuz: склейка прячется от владельца ровно
    тем движком, который обязан был её показывать. По объединению работ tidal
    даёт три совпадения — и становится честным носителем личности.

    `overlaps` — {витрина: годные работы страницы}; без неё (профили, собранные
    до того, как свидетелей стали сохранять) возвращаемся к `_persons_by_labels`.
    `drop` — склеенные витрины, исключаемые из носителей на этом шаге итерации
    `_merged_witnesses`.
    """
    if not overlaps:
        return _persons_by_labels(clusters, drop)
    drop_t = {_token(x) for x in drop}
    ov = {str(s): {tkey(x) for x in (tk or ())} for s, tk in overlaps.items()
          if _token(s) not in drop_t}

    def carriers(ts: set) -> frozenset:
        return frozenset(_token(s) for s, tk in ov.items()
                         if len(tk & ts) >= _SUPPORT_MIN)

    def absent(ts: set) -> set:
        return {_token(s) for s, tk in ov.items()
                if len(tk) >= _ATTEST_MIN and not (tk & ts)}

    by_token = {_token(s): tk for s, tk in ov.items()}

    # Фаза 1 — предварительная группировка по носителям ОДНОГО импринта.
    pre: dict[frozenset, list] = {}
    for _lbl, c in (clusters or {}).items():
        ts = {tkey(x) for x in (c.get("titles") or ())}
        if not ts:
            continue
        gs = {norm(str(x)) for x in (c.get("genres") or ())}
        pre.setdefault(carriers(ts), []).append((str(_lbl), ts, gs))
    # Фаза 2 — переносим носителей на объединение работ личности и склеиваем
    # ветки, которые поодиночке не дотягивали до порога, а вместе носятся одними
    # и теми же витринами (одна карьера на нескольких импринтах).
    out: dict[frozenset, dict] = {}
    for items in pre.values():
        union = set().union(*[ts for _lbl, ts, _gs in items]) if items else set()
        k = carriers(union)
        p = out.setdefault(k, {"titles": set(), "genres": [], "labels": []})
        for lbl, ts, gs in items:
            p["titles"] |= ts
            p["labels"].append(lbl)
            # Жанр личности доказывают только те кластеры, которые знает хоть
            # одна витрина-носитель: осколок страницы, не подтверждённый никем
            # («anjunadeep» у Solomon Grey — три микса, которых нет ни у tidal,
            # ни у spotify), о личности ничего не говорит, но в пересечение
            # жанров он ложится и обнуляет его — и пара личностей перестаёт
            # быть доказанной.
            if any(s in by_token and (by_token[s] & ts) for s in k):
                p["genres"].append(gs)
    for k, p in out.items():
        gs = [x for x in p["genres"] if x]
        p["family"] = _families(set.intersection(*gs) if gs else set())
        p["con"] = absent(p["titles"])
    return out


def _person_pairs(persons: dict) -> list:
    """Пары личностей, которые ДАННЫЕ доказывают как двух разных человек.

    Личности должны взаимно опровергаться свидетелями: каждую несёт витрина,
    которую у другой нет, и наоборот. Одного этого мало: у артиста с длинной
    историей каждый период живёт на своём лейбле и «Group Therapy» не носит
    релизов с mau5trap — это не два человека, это одна карьера. Поэтому в
    добавок жанры личностей не должны пересекаться СЕМЬЯМИ, и каждая из них
    должна занимать заметную часть страницы (`_PERSON_SHARE`): у BOP это
    Hip-Hop/Rap против Drum & Bass, у Solomon Grey — electronic-карьера
    Mercury/Decca против десяти испанских госпел-синглов.
    """
    total = len(set().union(*[p["titles"] for p in persons.values()])) if persons else 0
    if not total:
        return []
    keys = list(persons)
    out = []
    for i, k1 in enumerate(keys):
        for k2 in keys[i + 1:]:
            a, b = persons[k1], persons[k2]
            if not k1 or not k2:
                continue                     # носителей нет — судить нечем
            if len(a["titles"]) < total * _PERSON_SHARE:
                continue
            if len(b["titles"]) < total * _PERSON_SHARE:
                continue
            if not a["family"] or not b["family"] or (a["family"] & b["family"]):
                continue
            if not (a["con"] & k2) or not (b["con"] & k1):
                continue
            out.append((k1, k2))
    return out


def _merged_witnesses(clusters: dict, overlaps: Optional[dict] = None) -> set:
    """Витрины, на чьём id лежат ОБЕ спорящие личности.

    Такой свидетель не подтверждает никого: он и есть та склейка, из-за которой
    якорь принимал однофамильца за однофамильца (22.09.2026: у Solomon Grey
    deezer 4949652 и qobuz 1240683 несут и австралийского композитора, и
    испанского госпел-певца, и именно на их «подтверждении» общими заголовками
    висела вся склейка; чистыми свидетелями остаются tidal — только свой, и
    spotify — только чужой). Итерация до неподвижной точки: витрина,
    выглядевшая склеенной только благодаря другой склеенной витрине, перестаёт
    ею быть, когда та исключена.
    """
    merged: set = set()
    for _ in range(3):
        newly = set()
        for k1, k2 in _person_pairs(_persons(clusters, overlaps, merged)):
            newly |= (k1 & k2)
        if not (newly - merged):
            return merged | newly
        merged |= newly
    return merged


def _split(clusters: dict, overlaps: Optional[dict] = None
           ) -> tuple[dict, list, set]:
    """Разбор страницы целиком: (личности, спорящие пары, склеенные витрины).

    Одна функция, а не три: `_two_people` и профиль обязаны видеть ОДНИН и тот
    же набор свидетелей, иначе «склейка есть» и «склейка скрыта от владельца»
    будут решаться по-разному. Свидетелей (`overlaps`) передаём обоим путям —
    живому бинду и офлайновому пересчёту склада, — иначе экзамен и радар
    разойдутся ровно там, где живёт жалоба.
    """
    merged = _merged_witnesses(clusters, overlaps)
    persons = _persons(clusters, overlaps, merged)
    return persons, _person_pairs(persons), merged


def _two_people(clusters: dict, overlaps: Optional[dict] = None) -> bool:
    """Два человека на одной странице? Чистый предикат по кластерам.

    Склеенная витрина свидетелем не считается (`_merged_witnesses`): иначе
    носители обеих личностей пересекаются, взаимного опровержения не видно,
    и склейка прячется от владельца — ровно то, на чём жалоба и живёт.
    """
    return bool(_split(clusters, overlaps)[1])



def _profile_of(anchor: list, witnesses: dict, choice: Optional[dict] = None,
                artist: str = "") -> dict:
    """Сколько человек на странице подписки и чьи работы показывать.

    Метка личности — лейбловая группа работ (витрины дают лейблы чище, чем
    каталожные строки правообладания, поэтому группа собираем по `label_key`).
    Группа «чужая», если ХОТЬ ОДНА витрина с ≥3 работами этого артиста не несёт
    ни одной её работы; несёт — если у неё с группой ≥2 общих работ, годных как
    доказательство личности (`compilations.proves_identity`).

    Что показывать решает не догадка, а решение владельца: пока обе личности
    лежат на id, который он выбрал сам, скрывать нечего — терялись бы законные
    релизы. Поэтому страница только помечается (`two_people`), а составы групп
    (`group_titles`) уходят в UI, где владелец указывает, что считать чужим.
    Ничего не удаляется: скрытое видно в /api/identity и в складе.

    Состав свидетелей (`witness_overlaps`) остаётся в профиле рядом с кластерами
    сознательно: только так `needs_owner` можно пересчитать по сохранённым
    данным, когда правило клейки станет строже, — не дожидаясь новой сетевой
    привязки.
    """
    page_all = {tkey(r.get("title", "")) for r in anchor if r.get("title")}
    page_all.discard("")
    # Для кластеров и свидетелей остаются только работы, годные как доказательство
    # личности; витрине владельца (titles/hidden_titles) — все, иначе законный
    # саундтрек или мик исчез бы из подтверждённого списка работ.
    page = _evidence_split(anchor)[0]
    overlaps = {}
    for svc, rows in (witnesses or {}).items():
        tk = _evidence_split(rows or [])[0] & page
        if tk:
            overlaps[svc] = tk

    groups: dict[str, set] = {}
    for rows in (witnesses or {}).values():
        for lbl, ts in _label_groups(rows, artist).items():
            on_page = ts & page
            if len(on_page) >= 3:
                groups.setdefault(lbl, set()).update(on_page)
    for lbl, ts in _label_groups(anchor, artist).items():
        on_page = ts & page
        if len(on_page) >= 3:
            groups.setdefault(lbl, set()).update(on_page)

    def support(ts: set) -> set:
        return {s for s, tk in overlaps.items()
                if len(tk & ts) >= _SUPPORT_MIN}

    def contradicted(ts: set) -> set:
        return {s for s, tk in overlaps.items()
                if len(tk) >= _ATTEST_MIN and not (tk & ts)}

    def genres_of(ts: set) -> set:
        """Жанры работ группы — из всех витрин, где эти работы видны."""
        out = set()
        for rows in [anchor or []] + list((witnesses or {}).values()):
            for r in rows:
                if r.get("genre") and tkey(r.get("title", "")) in ts:
                    out.add(norm(str(r["genre"])))
        return out

    sup = {lbl: support(ts) for lbl, ts in groups.items()}
    con = {lbl: contradicted(ts) for lbl, ts in groups.items()}
    gs = {lbl: genres_of(ts) for lbl, ts in groups.items()}
    clusters = {lbl: {"titles": ts, "genres": gs[lbl],
                      "sup": sup[lbl], "con": con[lbl]}
                for lbl, ts in groups.items()}
    live, pairs, merged_w = _split(clusters, overlaps)
    two_people = bool(pairs)

    # Кто из двух «нужен» — данные не решают: обе личности лежат на id, который
    # выбрал владелец. Значит решение отдаём владельцу: подписка помечается,
    # а он указывает, какие лейбловые группы считать своими (choice). Пока
    # выбора нет, скрывать нечего — иначе терялись бы законные релизы.
    hidden: set = set()
    for lbl in ((choice or {}).get("hide") or []):
        hidden |= groups.get(label_key(str(lbl), artist), set())
    for ts in ((choice or {}).get("hide_titles") or []):
        hidden.add(tkey(str(ts)))
    if hidden:
        two_people = True

    genres = {norm(str(r.get("genre") or "")) for r in anchor if r.get("genre")}
    labels = {label_key(str(r.get("label") or ""), artist)
              for r in anchor if r.get("label")}
    visible = page_all - hidden
    # Что видно о каждой группе — не только владельцу в UI, но и нам при разборе:
    # «кто её несёт» и «кто её не несёт» ровно и есть доказательство личности.
    detail = {lbl: {"titles": sorted(ts)[:50],
                    "genres": sorted(gs[lbl])[:12],
                    "carriers": sorted(sup[lbl]),
                    "absent": sorted(con[lbl])}
              for lbl, ts in groups.items()}
    # Личности страницы — тем, кому их показывать надо, а не нам для догадки:
    # у каждой свои носители, свои лейблы и свои отсутствующие свидетели.
    # Именно по этому списку `bind` понимает, что на одном сервисе под этим
    # именем живут ДВОЕ, и не выбирает «первого попавшегося».
    persons_out = [
        {"labels": p["labels"],
         "carriers": sorted(k),
         "absent": sorted(p["con"]),
         "families": sorted(p["family"]),
         "titles": sorted(p["titles"])[:_TITLES_CAP],
         "n": len(p["titles"])}
        for k, p in sorted(live.items(),
                           key=lambda kv: (-len(kv[1]["titles"]), sorted(kv[0])))
    ]
    return {
        "titles": sorted(visible)[:_TITLES_CAP],
        "hidden_titles": sorted(hidden)[:_TITLES_CAP],
        "labels": sorted(x for x in labels if x)[:40],
        "genres": sorted(x for x in genres if x)[:40],
        "merged": bool(hidden),
        "two_people": bool(two_people),
        "persons": persons_out,
        "person_pairs": [[sorted(k1), sorted(k2)] for k1, k2 in pairs],
        "merged_witnesses": sorted(merged_w),
        "groups": {lbl: len(ts) for lbl, ts in sorted(groups.items())},
        "group_titles": {lbl: sorted(ts)[:50] for lbl, ts in sorted(groups.items())},
        "group_detail": detail,
        "witnesses": {svc: len(tk) for svc, tk in overlaps.items()},
        "witness_overlaps": {svc: sorted(tk)[:_TITLES_CAP]
                             for svc, tk in overlaps.items()},
    }


def clusters_of(entry: dict) -> dict:
    """Кластеры страницы по СОХРАНЁННОМУ профилю: {ключ: {titles, genres,
    carriers, absent}}.

    Нужен затем, чтобы вердикт «тут два человека» можно было пересмотреть по
    данным, не дожидаясь новой сетевой привязки: правило клейки со временем
    станет строже, а `two_people`, записанное месяц назад, само не поумнеет.
    Ключи кластеров прогоняются через `label_key` — в старых профилях группы
    нарезаны строкой авторского права, и «…2024 fred gibson» с «…2025 fred
    gibson» лежит там двумя кластерами одного лейбла.
    """
    prof = identity_of(entry).get("profile") or {}
    detail = prof.get("group_detail") or {}
    artist = str(entry.get("name") or "")
    overlaps = {svc: set(tk or ())
                for svc, tk in (prof.get("witness_overlaps") or {}).items()}
    merged: dict[str, dict] = {}
    for lbl, d in detail.items():
        k = label_key(str(lbl), artist)
        m = merged.setdefault(k, {"titles": set(), "genres": set(),
                                  "sup": set(), "con": None})
        ts = set(d.get("titles") or ())
        gs = {norm(str(g)) for g in (d.get("genres") or ())}
        if not gs and not ts:
            continue
        m["titles"] |= ts
        m["genres"] |= gs
        if overlaps:
            # Пересчёт по свидетелям: прежние carriers/absent считаны старым
            # правилом (одно совпадение уже «носит»), доверять им нельзя.
            m["sup"] |= {s for s, tk in overlaps.items()
                         if len(tk & ts) >= _SUPPORT_MIN}
            con = {s for s, tk in overlaps.items()
                   if len(tk) >= _ATTEST_MIN and not (tk & ts)}
            m["con"] = con if m["con"] is None else (m["con"] & con)
        else:
            m["sup"] |= set(d.get("carriers") or ())
            con = set(d.get("absent") or ())
            m["con"] = con if m["con"] is None else (m["con"] & con)
    for m in merged.values():
        m["con"] = m["con"] or set()
    return merged


def witness_overlaps_of(entry: dict) -> dict:
    """{витрина: годные работы страницы} из СОХРАНЁННОГО профиля — те самые
    свидетели, по которым `_persons` считает носителей. Нужны затем, чтобы
    офлайновый пересчёт склада (`two_people`, экзамен) шёл тем же путём, что
    живой бинд: и там, и тут в `_persons` кладут ОДНИ и те же overlaps, иначе
    экзамен и радар разойдутся — а расхождение и есть сегодняшний урок
    (22.09.2026)."""
    prof = identity_of(entry).get("profile") or {}
    return {str(svc): {tkey(x) for x in (tk or ())}
            for svc, tk in (prof.get("witness_overlaps") or {}).items()}


def two_people(entry: dict) -> bool:
    """Сколько человек лежит на id подписки — по её кластерам, а не по флагу
    прошлой привязки (см. `clusters_of`).
    """
    ident = identity_of(entry)
    clusters = clusters_of(entry)
    if not clusters:
        return bool((ident.get("profile") or {}).get("two_people")
                    or ident.get("two_people"))
    return _two_people(clusters, witness_overlaps_of(entry))


def needs_owner(entry: dict) -> bool:
    """Склейку должен разобрать владелец: на id два человека, а он ещё не
    сказал, какой из них подписывал.

    Второй случай того же рода: в витрине под этим именем зарегистрированы
    ДВОЕ (`namesakes`) и оба подтверждены общими работами — данные не решают,
    кого из них имел в виду подписчик, а молча выбрать значит однажды показать
    чужой релиз.
    """
    ident = identity_of(entry)
    choice = ident.get("choice") or {}
    if choice.get("hide") or choice.get("hide_titles"):
        return False
    if ident.get("ambiguous_names"):
        return True
    return two_people(entry)


def namesake_demotions(entry: dict) -> dict:
    """{витрина: id} — чей подтверждённый id на деле принадлежит НЕ тому
    человеку, которого подписчик назвал своим.

    Одна и та же функция для живого бинда и для пересчёта склада (её зовёт
    `tools/namesake_exam.py`): вердикт «это однофамилец» обязан считаться по
    одним правилам, иначе экзамен проверяет догадку, а не то, что делает радар.

    Основание — не «мало совпадений» (это «не знаю»), а доказанные два человека
    на странице: `_split` разобрал склейку, и работы принятого id лежат У ВЕРХА
    в том кластере, который владелец перевёл в чужие. Пока владелец не выбрал,
    разъединять нечего: обе личности всё ещё кандидаты на «его» (данные этого не
    решают), и подписка ждёт его в `needs_owner` — там автозакачка и стоит.

    Склеенную витрину (`merged_witnesses`) не трогаем: на её id лежат обе
    личности, она подтверждает и его тоже — это не однофамилец, это та самая
    склейка, и её лечит выбор владельца, а не снятие подтверждения.
    """
    ident = identity_of(entry)
    prof = ident.get("profile") or {}
    choice = ident.get("choice") or {}
    if not (prof.get("two_people") or (choice.get("hide") or choice.get("hide_titles"))):
        return {}
    hidden = {tkey(str(x)) for x in (prof.get("hidden_titles") or ()) if x}
    own = {tkey(str(x)) for x in (prof.get("titles") or ()) if x}
    if not hidden or not own:
        return {}                       # чужое не названо — разъединять нечем
    merged = {_token(x) for x in (prof.get("merged_witnesses") or ())}
    out: dict[str, str] = {}
    for svc, rec in (ident.get("services") or {}).items():
        rec = rec or {}
        if rec.get("status") != CONFIRMED:
            continue
        aid = str(rec.get("id") or "")
        shared = {tkey(str(x)) for x in ((prof.get("witness_overlaps") or {}).get(svc) or ()) if x}
        if not aid or not shared or _token(svc) in merged:
            continue
        if shared & own:
            continue                    # несёт и его работы тоже — не однофамилец
        if shared <= hidden:
            out[str(svc)] = aid
    return out


def apply_namesake_demotions(entry: dict, services_map: dict,
                             demotions: dict, prof: dict) -> None:
    """Снять подтверждение с id однофамильца и записать это как починку.

    Снимаем до статуса `pending`: «не подтверждено для ЭТОЙ подписки» — честный
    вердикт, и он же обязывает перепроверить (через `_RETRY_TTL` витрины могут
    отдать и настоящий id нашего человека). Сам id и числа остаются в записи и
    уходят в `namesakes` — владельцу видно, КОГО именно радар считал им и
    перестал считать, а `repairs` переживает перезапуск.
    """
    for svc, aid in (demotions or {}).items():
        rec = dict(services_map.get(svc) or {})
        if str(rec.get("id") or "") != aid or rec.get("namesake"):
            continue
        why = ("подтверждение держалось на склейке однофамильцев: работы этого "
               "id лежат в кластере, который владелец назвал чужим")
        services_map[svc] = {
            "id": aid, "name": rec.get("name") or str(entry.get("name") or ""),
            "status": PENDING, "namesake": True, "namesakes": [aid],
            "evidence": rec.get("evidence") or {}, "ts": rec.get("ts"),
            "why": why,
        }
        ident = identity_of(entry)
        rep = ident.setdefault("repairs", [])
        if not any((r.get("service"), r.get("from")) == (svc, aid) and r.get("why") == why
                   for r in rep):
            rep.append({"service": svc, "from": aid, "to": "",
                        "name": str(entry.get("name") or ""), "why": why,
                        "ts": time.time()})
            del rep[:-20]
        prof.setdefault("demoted_namesakes", {})[svc] = aid


def hidden_add(entry: dict, rel: dict, reason: str) -> None:
    """Скрытое MUST быть видно владельцу: список на подписке + счётчик.

    Вместе с причиной пишем сервис и id карточки: владелец решает «вернуть или
    нет» по конкретной карточке, а не по названию, под которым на этом же id
    лежат ещё и чужие релизы.
    """
    ident = identity_of(entry)
    hid = ident.setdefault("hidden", [])
    k = tkey(rel.get("title", "") or rel.get("name", ""))
    if k and not any(h.get("key") == k for h in hid):
        from . import owner_anchor
        hid.append({"key": k, "title": str(rel.get("title") or rel.get("name") or ""),
                    "date": str(rel.get("date") or ""), "reason": reason,
                    "service": str(rel.get("service") or ""),
                    "id": str(rel.get("id") or ""),
                    "label": str(rel.get("label") or ""),
                    "genres": owner_anchor.genres_of(rel),
                    "ts": time.time()})
        if len(hid) > 80:
            del hid[:len(hid) - 80]
    entry["identity"] = ident


def _sub_index(entries: list) -> tuple[dict, dict]:
    """(подписки по Apple-id, подписки по (витрина, подтверждённый id)).

    Склеенные страницы бывают не только у Apple: Deezer и Qobuz держат
    однофамильцев на одном id так же (Solomon Grey, 23.09.2026 — испанский
    «Refugio en la Tormenta» шёл в ленту Deezer ×1 и Qobuz ×6, хотя владелец
    скрыл весь этот кластер). Подписку ищем по паре (сервис, id ПОДТВЕРЖДЁННОЙ
    витрины) для любого сервиса.
    """
    by_aid, by_svc_aid = {}, {}
    for e in entries or []:
        aid = str(e.get("artist_id") or "")
        if aid and e.get("kind") != "label":
            by_aid.setdefault(aid, e)
        if e.get("kind") == "label":
            continue
        for svc, rec in ((identity_of(e).get("services") or {}).items()):
            rec = rec or {}
            if rec.get("status") == CONFIRMED and rec.get("id"):
                by_svc_aid.setdefault((str(svc), str(rec["id"])), e)
    return by_aid, by_svc_aid


def feed_filter(rels: list, entries: list, base_dir=None, save=None) -> list:
    """Единый фильтр ленты — для ВСЕХ витрин, а не только для склада радара.

    Прежняя дверь жила в `routes/radar.py` и стояла только на складе
    (apple/bbc/soundcloud/labels): кросс-сервисные ленты Deezer/Qobuz/Tidal
    из `routes/releases.py` проходили мимо неё, и именно поэтому жалоба
    владельца на «не того Соломона» пережила правку 23.09 — кластер, который
    он уже назвал чужим, всё равно приезжал из Deezer и Qobuz.

    Самоизлечение при ЧТЕНИИ: склад не трогается, а в ленту не выходит то, чью
    принадлежность нельзя подтвердить. Ничего не удаляется: запись остаётся в
    складе (см. docstring модуля ripster-radar-persistence) и становится видна
    владельцу в /api/identity как скрытая с причиной.
    """
    from . import owner_anchor

    # Диск с добытыми ранее доказательствами читаем ВСЕГДА: иначе вердикт
    # зависел бы от того, каким именно путём карточку сегодня обогатили.
    owner_anchor.apply_card_cache(rels or [])
    by_aid, by_svc_aid = _sub_index(entries)
    catalog = load_catalog(base_dir) if base_dir else {}
    touched, out = False, []
    for r in rels or []:
        if not stitch_shows(r, entries):
            owner_anchor.note("stitch_dropped")
            continue
        ok, why = name_claim_shows(r, entries, catalog)
        if not ok:
            owner_anchor.note("name_claim_dropped")
            _remember_dropped(r, why)
            continue
        entry = _entry_for(r, by_aid, by_svc_aid)
        if entry is not None and entry.get("name"):
            keep, why = home_show(entry, r)
            if not keep:
                hidden_add(entry, r, why)
                touched = True
                continue
        out.append(r)
    if touched and save:
        try:
            save(entries)
        except Exception as e:                                 # noqa: BLE001
            print(f"[identity] вишлист не сохранён: {e}", flush=True)
    return out


def _entry_for(rel: dict, by_aid: dict, by_svc_aid: dict) -> Optional[dict]:
    """Подписка, к которой относится карточка, — по id витрины."""
    rs, ra = str(rel.get("service") or ""), str(rel.get("artist_id") or "")
    if rs == "apple":
        e = by_aid.get(ra)
        if e is not None:
            return e
    return by_svc_aid.get((rs, ra)) if ra else None


# Карточки, отсечённые сверкой по имени (у лейбловой ленты нет id подписки):
# писать `hidden` им некуда — держим короткий список для отчёта, иначе
# «пропало молча» неотличимо от «ничего не было».
_DROPPED: list = []


def _remember_dropped(rel: dict, why: str) -> None:
    item = {"title": str(rel.get("title") or rel.get("name") or ""),
            "artist": str(rel.get("artist") or ""),
            "service": str(rel.get("service") or ""),
            "date": str(rel.get("date") or ""), "reason": why, "ts": time.time()}
    if not any(d.get("title") == item["title"] and
               d.get("artist") == item["artist"] for d in _DROPPED):
        _DROPPED.append(item)
    del _DROPPED[:-200]


def dropped_cards(n: int = 50) -> list:
    return list(_DROPPED)[-n:]


# ── Сетевой обход: кандидаты → признаки → карта на подписке ─────────────────

def _rows_apple(releases: list) -> list:
    """Строки якоря из iTunes-lookup: у него релиз называется `collectionName`,
    а не `name` (проверено на живом ответе 21.09.2026)."""
    out = []
    for r in releases or []:
        title = (r.get("collectionName") or r.get("name")
                 or r.get("trackName") or "")
        row = {"title": title,
               "collection": str(r.get("collectionId") or ""),
               "date": (r.get("releaseDate") or r.get("date") or "")[:10]}
        cp = str(r.get("copyright") or "")
        m = re.sub(r"^℗\s*\d{4}\s+", "", cp).strip()
        artist = r.get("artistName") or r.get("artist") or ""
        if m and norm(m) != norm(title) and norm(m) != norm(artist):
            row["label"] = m
        if r.get("primaryGenreName"):
            row["genre"] = r["primaryGenreName"]
        out.append(row)
    return out


def _rows_catalog(service: str, raw: list) -> list:
    out = []
    for r in raw or []:
        row = {"title": r.get("title", ""), "date": str(r.get("releaseDate")
               or r.get("release_date") or r.get("release_date_original") or "")[:10]}
        lbl = r.get("label")
        if isinstance(lbl, dict):
            lbl = lbl.get("name")
        row["label"] = str(lbl or r.get("label_name") or "")
        for f in ("upc", "ean", "barcode", "isrc"):
            if r.get(f):
                row[f] = str(r[f])
        out.append(row)
    return out


async def _fetch_anchor(entry: dict, client, cfg: dict) -> list:
    """Дискография самой подписки — то, чем мы сверяем кандидатов.

    Страница Apple — публичный lookup без токена, поэтому она — якорь по
    умолчанию (так добавлены почти все подписки). Но у подписки из другой
    витрины яблочный id может быть чужим числом, и тогда сверять не с чем:
    берём релизы её собственного сервиса.

    Одного `entity=album` мало: он никогда не отдаёт сборники, где у артиста
    один трек, — а для узкого артиста это ровно те релизы, по которым его и
    можно опознать (у «BOP» яблочная страница США выдаёт один сингл из всего
    каталога). Второй lookup — по трекам, как в `watchlist.
    _apple_artist_collections`: трек всегда несёт поля своей коллекции.
    """
    aid = str(entry.get("artist_id") or "")
    if not aid:
        return []
    if (entry.get("service") or "apple") in ("deezer", "tidal", "qobuz"):
        return await _candidate_rows(entry["service"], aid, client, cfg)
    rows: dict[str, dict] = {}
    for entity in ("album", "song"):
        try:
            r = await client.get("https://itunes.apple.com/lookup",
                                 params={"id": aid, "entity": entity, "limit": 200,
                                         "country": "us", "sort": "recent"})
            if r.status_code != 200:
                continue
            got = [x for x in ((r.json() or {}).get("results") or [])[1:]
                   if x.get("wrapperType") in ("collection", "track")]
        except Exception:                                     # noqa: BLE001
            continue
        for row in _rows_apple(got):
            k = row.get("collection") or tkey(row.get("title", ""))
            if k and k not in rows:
                rows[k] = row
    return list(rows.values())


async def _candidates(service: str, name: str, client, cfg: dict) -> list:
    """Кандидаты с тем же нормализованным именем — ВСЕ, а не «самый популярный».
    Дальше их разводит classify(), а не очередь популярности."""
    try:
        if service == "deezer":
            r = await client.get("https://api.deezer.com/search/artist",
                                 params={"q": name, "limit": 10})
            items = (r.json() or {}).get("data") or [] if r.status_code == 200 else []
        elif service == "tidal":
            tok = str((cfg or {}).get("tidal-token") or "").strip()
            if not tok:
                return []
            cc = str((cfg or {}).get("tidal-country") or "US").strip().upper() or "US"
            r = await client.get("https://api.tidal.com/v1/search",
                                 params={"query": name, "types": "ARTISTS",
                                         "limit": 10, "countryCode": cc},
                                 headers={"Authorization": f"Bearer {tok}"})
            items = (r.json().get("artists") or {}).get("items") or [] if r.status_code == 200 else []
        elif service == "qobuz":
            tok = str((cfg or {}).get("qobuz-auth-token") or "").strip()
            if not tok:
                return []
            app_id = str((cfg or {}).get("qobuz-app-id") or "312369995").strip()
            r = await client.get("https://www.qobuz.com/api.json/0.2/artist/search",
                                 params={"query": name, "limit": 10, "app_id": app_id},
                                 headers={"X-User-Auth-Token": tok, "X-App-Id": app_id})
            items = (r.json().get("artists") or {}).get("items") or [] if r.status_code == 200 else []
        else:
            return []
    except Exception:                                     # noqa: BLE001
        return []                                          # сбой — не «кандидатов нет»
    want = norm(name)
    return [{"id": str(a.get("id")), "name": a.get("name", "")}
            for a in items if a.get("id") and norm(a.get("name", "")) == want][:6]


async def _candidate_rows(service: str, cid: str, client, cfg: dict) -> list:
    """Релизы кандидата — те же витрины, что питает радар, пачкой по 50."""
    try:
        if service == "deezer":
            r = await client.get(f"https://api.deezer.com/artist/{cid}/albums",
                                 params={"limit": 50})
            raw = (r.json() or {}).get("data") or [] if r.status_code == 200 else []
            return _rows_catalog(service, raw)
        if service == "qobuz":
            tok = str((cfg or {}).get("qobuz-auth-token") or "").strip()
            if not tok:
                return []
            app_id = str((cfg or {}).get("qobuz-app-id") or "312369995").strip()
            r = await client.get("https://www.qobuz.com/api.json/0.2/artist/get",
                                 params={"artist_id": cid, "extra": "albums",
                                         "limit": 50, "app_id": app_id},
                                 headers={"X-User-Auth-Token": tok, "X-App-Id": app_id})
            raw = (r.json().get("albums") or {}).get("items") or [] if r.status_code == 200 else []
            return _rows_catalog(service, raw)
        if service == "tidal":
            tok = str((cfg or {}).get("tidal-token") or "").strip()
            if not tok:
                return []
            cc = str((cfg or {}).get("tidal-country") or "US").strip().upper() or "US"
            r = await client.get(f"https://api.tidal.com/v1/artists/{cid}/albums",
                                 params={"limit": 50, "countryCode": cc},
                                 headers={"Authorization": f"Bearer {tok}"})
            raw = (r.json() or {}).get("items") or [] if r.status_code == 200 else []
            return _rows_catalog(service, raw)
    except Exception:                                     # noqa: BLE001
        return []
    return []


def _spotify_rows(state: dict, cid: str) -> list:
    """Spotify НЕ спрашиваем: его дискография уже лежит в складе радара.

    Владельца обложки берём из `album_meta` — это ответ `getAlbum`, то есть
    строка кредитования конкретного релиза, а не догадка по имени. Для
    доказательств личности она и есть решающая: под «Group Therapy Anjuna25
    Special with Above & Beyond (DJ Mix)» лежит Above & Beyond, и совпадение
    этого заголовка с подпиской не говорит о Solomon Grey ничего.
    """
    rec = ((state or {}).get("artists") or {}).get(cid) or {}
    am = (state or {}).get("album_meta") or {}
    out = []
    for r in rec.get("releases") or []:
        row = {"title": r.get("title", ""), "date": str(r.get("date") or "")[:10],
               "artist_id": str(r.get("artist_id") or cid),
               # id ВЫПУСКА, а не артиста: два профиля одного человека в
               # Spotify узнаются именно по общим альбомам (`_ID_NS["album"]`).
               "album": str(r.get("id") or ""),
               "artist": r.get("artist", "")}
        meta = am.get(str(r.get("id") or "")) or {}
        if meta.get("label"):
            row["label"] = meta["label"]
        if meta.get("type"):
            row["type"] = meta["type"]
        if meta.get("artist"):
            row["album_artist"] = meta["artist"]
        out.append(row)
    return out


def _spotify_candidates(state: dict, name: str) -> list:
    want = norm(name)
    fol = ((state or {}).get("followed") or {}).get("artists") or []
    return [{"id": str(a.get("id")), "name": a.get("name", "")}
            for a in fol
            if a.get("id") and norm(a.get("name", "")) == want][:6]


def load_spotify_state(base_dir) -> dict:
    """Локальный склад Spotify-радера — источник его id без единого запроса.

    `base_dir` принимает и Path, и строку: `base_dir / "…"` на строке бросает
    TypeError, а он здесь глушится — молчаливый `{}` означал бы «кандидатов в
    Spotify нет», то есть ровно ту же слепоту, из-за которой BOP и Solomon Grey
    и не перепривязывались (21.09.2026).
    """
    from pathlib import Path

    try:
        return json.loads((Path(base_dir) / "spotify_artist_state.json")
                          .read_text(encoding="utf-8")) or {}
    except Exception:                                     # noqa: BLE001
        return {}


_catalog_cache: dict = {}


def load_catalog(base_dir) -> dict:
    """Что радар знает о релизах сам: какой артист какой работой владеет.

    Карточке лейбловой ленты нечем сказать, КТО она, кроме строки имени. Но
    если этот же релиз радар уже видел на странице какого-то артиста, то
    владельца работы мы знаем — и можем сверить его с подпиской ДАННЫМИ, без
    единого запроса: источник — локальный склад `spotify_artist_state.json`.
    Кэш по mtime: файл пишет радар, а читает их обоих — и лента, и разбор.
    """
    from pathlib import Path

    f = Path(base_dir) / "spotify_artist_state.json"
    try:
        stamp = f.stat().st_mtime
    except OSError:
        return {}
    hit = _catalog_cache.get(str(f))
    if hit and hit[0] == stamp:
        return hit[1]
    state = load_spotify_state(f.parent)
    owners: dict[str, set] = {}
    names: dict[str, str] = {}
    for cid, rec in ((state or {}).get("artists") or {}).items():
        names[str(cid)] = str(rec.get("name") or "")
        for r in rec.get("releases") or []:
            rid = str(r.get("id") or "")
            if rid:
                owners.setdefault(rid, set()).add(str(r.get("artist_id") or cid))
    cat = {"service": "spotify", "owners": owners, "names": names, "state": state}
    _catalog_cache[str(f)] = (stamp, cat)
    return cat


def _owner_clusters(entry: dict) -> tuple[list, list]:
    """(свои работы, чужие работы) склеенной страницы — по решению владельца.

    Профиль склеенной подписки содержит ОБОИХ однофамильцев: пока владелец не
    сказал, какой кластер его, `profile["titles"]` — не профиль личности, а список
    всего, что страница про себя наплела. Чужими считаются ровно те работы,
    которые владелец сам перевёл в `hidden_titles` (или которых нет в группах,
    названных им своими) — ничто другое чужим не бывает: на законный appear-on
    кластерная разметка не распространяется.
    """
    ident = identity_of(entry)
    prof = ident.get("profile") or {}
    base = {t for t in (prof.get("titles") or []) if t}
    hidden = {t for t in (prof.get("hidden_titles") or []) if t}
    choice = ident.get("choice") or {}
    hide_keys = {label_key(str(x), str(entry.get("name") or ""))
                 for x in (choice.get("hide") or [])}
    own: set = set(base)
    if hide_keys:
        clusters = clusters_of(entry)
        named = set()
        for lbl, c in clusters.items():
            if lbl in hide_keys:
                continue
            named |= {t for t in (c.get("titles") or ()) if t}
        if named:
            own = (base | named) - hidden
    foreign = hidden - own
    return sorted(own), sorted(foreign)


def name_claim_shows(rel: dict, entries: list,
                     catalog: Optional[dict] = None) -> tuple[bool, str]:
    """Пускать ли карточку, у которой на подписку указывает ТОЛЬКО имя.

    Лейбловая лента (`via_label`) приходит без id артиста: её нечем подтвердить,
    кроме строки «BOP». Прежний гейт смотрел ровно на `via_xref`, поэтому такие
    карточки проходили молча — так и жил «BOP» с dnb-релизом Hospital Records
    в подписке на рэпера (21.09.2026, жалоба живёт с 03.09).

    Судит не имя и не жанр, а каталог: если тот же релиз радар видел на
    странице артиста с тем же именем, личность проверяется работами — как
    везде. Порядок доказательств от сильнейшего к слабейшему:

      1. витрина подтвердила для подписки id, которому принадлежит эта работа, —
         пускаем (это не догадка, это пересечение работ);
      2. работа лежит в кластере, который владелец на этой же подписке назвал
         чужим, — не пускаем: чужество уже доказано и решено, а не угадано;
      3. подписка НЕ разобрана (сверку никто не начинал) — пускаем: «не знаю»
         не есть «чужой»;
      4. подписка разобрана и ни одна витрина её личность не подтвердила
         (`pending`) — не пускаем: карточка держится только на строке имени, а
         имя ровно то, на чём эта жалоба и выросла (см. docstring модуля);
      5. настоящий владелец работы подтверждён ДРУГИМ id, а с работами
         подписки его каталог не пересекается вовсе — не пускаем.
    """
    if rel.get("via_xref") or str(rel.get("artist_id") or ""):
        return True, ""                        # у этих путей свой гейт — по id
    if not rel.get("via_label"):
        return True, ""
    name = str(rel.get("artist") or rel.get("alb_artist") or "").strip()
    if not name or _comps.is_various_artists(name):
        return True, ""            # «Various Artists» — не утверждение о человеке
    svc = str(rel.get("service") or "")
    cat = catalog or {}
    if cat.get("service") != svc:
        return True, ""            # витрины, о которых мы ничего не знаем, не судим
    owners = (cat.get("owners") or {}).get(str(rel.get("id") or "")) or set()
    want = norm(name)
    state = cat.get("state") or {}
    names = cat.get("names") or {}
    same_name = [e for e in (entries or [])
                 if e.get("kind") != "label" and e.get("artist_id")
                 and norm(str(e.get("name") or "")) == want]
    if not same_name:
        return True, ""
    confirmed = [{s: r for s, r in (identity_of(e).get("services") or {}).items()}
                 for e in same_name]
    # 1 — id, подтверждённый для подписки, владеет этой работой.
    for e, recs in zip(same_name, confirmed):
        rec = recs.get(svc) or {}
        if rec.get("status") == CONFIRMED and str(rec.get("id") or "") in owners:
            return True, "confirmed id owns this release"
    # 2 — кластер, названный владельцем чужим, обратно не выпускаем.
    for e in same_name:
        _own, foreign = _owner_clusters(e)
        t = tkey(rel.get("title", "") or rel.get("name", ""))
        if t and t in set(foreign):
            return False, (f"«{e.get('name')}»: работа принадлежит кластеру, "
                           f"который владелец назвал чужим")
    # 3 — разбора не было: судить нечем.
    examined = [recs for recs in confirmed if recs]
    if not examined:
        return True, ""
    # 4 — разбирали, не подтвердили: имени верить нельзя.
    if not any((r or {}).get("status") == CONFIRMED for recs in examined for r in recs.values()):
        return False, (f"{name} подписан(а) по имени, но ни одна витрина его id "
                       f"общими работами не подтвердила (pending)")
    if not owners:
        return True, ""                        # релиз нам не знаком — судить нечем
    # 5 — подтверждённый чужой id + ни одной общей работы.
    for e, recs in zip(same_name, confirmed):
        rec = recs.get(svc) or {}
        if rec.get("status") != CONFIRMED:
            continue
        own, _f = _owner_clusters(e)
        if len(own) < 2:
            continue                           # сверять не с чем
        for o in owners:
            if norm(names.get(o, "")) != want:
                continue   # не однофамилец: чужая работа на чужом имени не наша история
            if str(rec.get("id") or "") == o:
                return True, "confirmed id owns this release"
            v = classify([{"title": t} for t in own], _spotify_rows(state, o))
            if v["verdict"] == CONFIRMED:
                return True, "release owner shares the subscription's works"
            if v["verdict"] == CONFLICT:
                return False, (f"работы {name} из {svc} ({o}) не пересекаются с "
                               f"подтверждённым профилем подписки "
                               f"(общих работ: 0 из {len(own)})")
    return True, ""


def _rank(ev: dict) -> tuple:
    e = ev or {}
    ids = e.get("ids") or {}
    return (int(ids.get("upc") or 0) + int(ids.get("isrc") or 0)
            + int(ids.get("album") or 0),
            int(e.get("titles_n") or 0), bool(e.get("labels_shared")))


def _strong(ev: dict) -> bool:
    """Доказательство, после которого можно НЕ смотреть других кандидатов.

    Номера выпуска (штрихкод/ISRC/id альбома) или работа, которую обе стороны
    знают под своим номером, — это про конкретные релизы. Две совпавшие
    строчки названия — то, на чём живёт однофамилец: у «BOP» так сшивались
    рэпер и dnb-продюсер, поэтому после слабого подтверждения кандидатов
    досматриваем, а не останавливаемся на первом везении.
    """
    e = ev or {}
    ids = e.get("ids") or {}
    return bool(ids.get("upc") or ids.get("isrc") or ids.get("album")
                or int(e.get("titled_by_release_id") or 0)
                or int(e.get("titles_n") or 0) >= 4)


def _relink(prev: Optional[dict], rec: dict, service: str, name: str,
            ident: dict) -> None:
    """Что изменилось против ПРОШЛОГО вердикта этой витрины.

    Две вещи, и обе — про самоизлечение, а не про протокол:

      • `aliases`/`namesakes` переписываемого id не теряем: дубли профиля и
        однофамильцы в витрине — её свойство, а не удача конкретного прогона
        (в этом проге кандидаты могли просто не успеть подтвердиться);
      • сменившийся id — это РАЗОБРАННАЯ склейка: раньше на подписке висел
        другой человек. Такое событие переживает перезапуск только записанным
        на подписку, иначе «сколько мы починили» никто не скажет.
    """
    prev = prev or {}
    old_id = str(prev.get("id") or "")
    if (prev.get("status") == CONFIRMED and old_id
            and old_id == str(rec.get("id") or "")):
        rec["aliases"] = sorted(set(rec.get("aliases") or [])
                                | set(prev.get("aliases") or []))
        rec["namesakes"] = sorted(set(rec.get("namesakes") or [])
                                  | set(prev.get("namesakes") or []))
        return
    if prev.get("status") != CONFIRMED or not old_id:
        return
    log = ident.setdefault("repairs", [])
    key = (service, old_id, str(rec.get("id") or ""))
    if any((r.get("service"), r.get("from"), r.get("to")) == key for r in log):
        return
    log.append({"service": service, "from": old_id,
                "to": str(rec.get("id") or ""), "name": name,
                "why": "прежнее подтверждение не выдержало перепроверки по "
                       "номерам выпуска и кластерам страницы",
                "ts": time.time()})
    del log[:-20]


async def bind(entry: dict, client, cfg: dict, *, services: tuple =
               ("deezer", "tidal", "qobuz"), spotify_state: Optional[dict] = None,
               genre_hint: str = "") -> dict:
    """Привязать подписку к витринам ДАННЫМИ. Пишет entry["identity"].

    Кандидат, подтверждённый только вслепую (именем), не получает здесь НИЧЕГО:
    статус только за пересечение работ/штрихкодов/лейблов. Не подтвердился —
    pending (перепроверится через две недели) или conflict; в ленту не пускаем.
    Кандидаты, подтверждённые транзитивно (общие работы с уже принятым id),
    сшиваются тем же classify — личность строится из пересечений, а не из имён.
    """
    name = str(entry.get("name") or "").strip()
    ident = identity_of(entry)
    if ident.get("services") and services_bound(entry):
        return ident
    anchor = await _fetch_anchor(entry, client, cfg)
    # Жанровая подсказка для MusicBrainz берётся из ЯКОРЯ, а не из списка
    # «что считается dnb»: та же страница подписки и есть источник признака.
    if not genre_hint:
        gs = [r.get("genre") for r in anchor if r.get("genre")]
        genre_hint = max(set(gs), key=gs.count) if gs else ""
    services_map: dict = dict(ident.get("services") or {})
    profile_rows: list = list(anchor)
    witnesses: dict = {}          # service → строки принятого id (свидетели склейки)
    any_conflict = False

    for service in services:
        cands = await _candidates(service, name, client, cfg)
        for m in await asyncio.to_thread(_mb_ids, name, service, genre_hint):
            if not any(c["id"] == m["id"] for c in cands):
                cands.append(m)
        cands = cands[:6]
        # Дискографию кандидата качаем ПОСЛЕДОВАТЕЛЬНО и стоп на первом
        # подтверждённом СИЛЬНО: каталоги отдают поиск по своей популярности, а
        # платим мы запросом на каждого неподтверждённого кандидата. Слабое
        # подтверждение (две общие строки названия и ни одного номера выпуска) —
        # ровно то, чем живёт однофамилец, поэтому после него смотрим остальных.
        # Цена — несколько запросов на редкой подписке, выигрыш — два человека
        # под одним именем в одной витрине видны, а не выбран первый, кому
        # повезло подтвердиться.
        rows_by_cand: dict = {}
        confirmed: list = []
        for c in cands:
            rows = await _candidate_rows(service, c["id"], client, cfg)
            rows_by_cand[c["id"]] = rows
            v = classify(anchor, rows)
            any_conflict = any_conflict or v["verdict"] == CONFLICT
            if v["verdict"] != CONFIRMED:
                continue
            confirmed.append({"c": c, "verdict": v, "rows": rows})
            if _strong(v["evidence"]):
                break
        best = (max(confirmed, key=lambda x: _rank(x["verdict"]["evidence"]))
                if confirmed else None)
        if not best and len(profile_rows) > len(anchor):
            # второй круг: пересечение с уже набранным профилем личности — так
            # узкий артист сшивается транзитивно через подтверждённого соседа
            grown = [{"title": r.get("title", "")} for r in profile_rows]
            for c in cands:
                v = classify(grown, rows_by_cand.get(c["id"]) or [])
                if v["verdict"] == CONFIRMED and (
                        not best or _rank(v["evidence"]) > _rank(best["verdict"]["evidence"])):
                    best = {"c": c, "verdict": v,
                            "rows": rows_by_cand.get(c["id"]) or []}
        # Один ли человек стоит за подтверждёнными кандидатами? Два каталога,
        # которые классифицируются как КОНФЛИКТ, — два однофамильца в одной
        # витрине: кто из них подписан, данные не знают, и молча выбирать
        # нельзя. Кто согласуется с принятым — дубль профиля того же человека
        # (так живут «Noisia», «Mat Zo», Robert Hood), и его релизы своих
        # подписок терять нельзя: они едут в `aliases`.
        twins: list = []
        rivals: list = []
        for other in confirmed:
            if best is None or other is best:
                continue
            if classify(best["rows"], other["rows"])["verdict"] == CONFLICT:
                rivals.append(str(other["c"]["id"]))
            else:
                twins.append(str(other["c"]["id"]))

        if best:
            rec = {"id": best["c"]["id"],
                   "name": best["c"].get("name", name),
                   "status": CONFIRMED,
                   "evidence": best["verdict"]["evidence"],
                   "ts": time.time()}
            if twins:
                # дубли профиля ОДНОГО человека: пускать их надо, иначе
                # законный релиз с соседнего id пропадёт из ленты
                rec["aliases"] = sorted(set(twins))
            if rivals:
                # тот же номер на той же витрине — ДРУГОЙ человек с тем же
                # именем. Кого из них подписывал владелец, данные не знают:
                # это вопрос к нему, а не повод выбирать наугад.
                rec["namesakes"] = sorted(set(rivals))
            _relink(services_map.get(service), rec, service, name, ident)
            services_map[service] = rec
            witnesses[service] = best["rows"]
            profile_rows.extend(best["rows"])
        elif (services_map.get(service) or {}).get("status") != CONFIRMED:
            services_map[service] = {"name": name, "status": PENDING, "ts": time.time()}

    if spotify_state is not None:
        s_conf: list = []
        for c in _spotify_candidates(spotify_state, name):
            rows = _spotify_rows(spotify_state, c["id"])
            v = classify(profile_rows or anchor, rows)
            if v["verdict"] == CONFIRMED:
                s_conf.append({"c": c, "verdict": v, "rows": rows})
        sbest = (max(s_conf, key=lambda x: _rank(x["verdict"]["evidence"]))
                 if s_conf else None)
        if sbest:
            s_twins, s_rivals = [], []
            for other in s_conf:
                if other is sbest:
                    continue
                if classify(sbest["rows"], other["rows"])["verdict"] == CONFLICT:
                    s_rivals.append(str(other["c"]["id"]))
                else:
                    s_twins.append(str(other["c"]["id"]))
            rec = {"id": sbest["c"]["id"], "name": name,
                   "status": CONFIRMED,
                   "evidence": sbest["verdict"]["evidence"], "ts": time.time()}
            if s_twins:
                rec["aliases"] = sorted(set(s_twins))
            if s_rivals:
                rec["namesakes"] = sorted(set(s_rivals))
            _relink(services_map.get("spotify"), rec, "spotify", name, ident)
            services_map["spotify"] = rec
            witnesses["spotify"] = sbest["rows"]
        elif "spotify" not in services_map:
            services_map["spotify"] = {"name": name, "status": PENDING, "ts": time.time()}

    # Профиль личности — по лейбловым кластерам страницы и чистым свидетелям:
    # так видно склейку двух однофамильцев на одном id и что из них настоящее.
    ident["services"] = services_map
    ident["profile"] = _profile_of(anchor, witnesses,
                                   ident.get("choice") or {}, name)
    # Профиль пересобран по нынешним правилам — отметку ставим ДО запроса
    # владельца: `needs_owner` читает кластеры, а право на перепроверку
    # (`services_bound`) — эту отметку, и зависеть они должны на один проход.
    ident["rule_version"] = IDENTITY_RULE_VERSION
    ident["ambiguous_names"] = {s: sorted(r.get("namesakes") or [])
                                for s, r in services_map.items()
                                if (r or {}).get("namesakes")}
    entry["identity"] = ident
    # Подтверждение, державшееся на склейке, снимаем сразу же — по тому же
    # профилю, по которому её увидели: иначе id однофамильца ещё один проход
    # питал бы ленту и автозакачку.
    demotions = namesake_demotions(entry)
    if demotions:
        apply_namesake_demotions(entry, services_map, demotions, ident["profile"])
    ident["two_people"] = two_people(entry)
    ident["needs_owner"] = needs_owner(entry)
    ident["anchor"] = {"service": entry.get("service") or "apple",
                       "artist_id": str(entry.get("artist_id") or ""),
                       "rows": len(anchor)}
    ident["ts"] = time.time()
    if any_conflict:
        ident["conflict"] = True
    else:
        ident.pop("conflict", None)
    entry["identity"] = ident
    return ident


def _mb_ids(name: str, service: str, genre_hint: str) -> list:
    """id кандидата через MusicBrainz (разводит однофамильцев по жанру)."""
    try:
        from . import musicbrainz as _mb
        hit = _mb.resolve_service_id(name, service, genre_hint)
        return [{"id": str(hit["id"]), "name": hit.get("name", name)}] if hit else []
    except Exception:                                     # noqa: BLE001
        return []


async def bind_pass(entries: list, client, cfg: dict, *, services: tuple =
                    ("deezer", "tidal", "qobuz"), spotify_state: Optional[dict] = None,
                    cap: int = DEFAULT_BIND_CAP, genre_hints: Optional[dict] = None,
                    budget_sec: float = 25.0) -> int:
    """Привязать до `cap` неподвязанных подписок за проход. Темп — радарный:
    вызывается только из скана, не из каждого открытия вкладки.

    `budget_sec` — потолок времени на весь обход. Радар не должен ждать
    привязки: не успели — продолжит следующий проход (очередь идёт от самых
    давно не тронутых, поэтому за несколько сканов перебирает всех).
    """
    todo = [e for e in entries
            if e.get("artist_id") and e.get("kind") != "label"
            and not services_bound(e)]
    todo.sort(key=lambda e: float(identity_of(e).get("ts") or 0))
    own = client is None
    if own:
        import httpx
        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=20,
                                                         write=10, pool=5))
    done, started = 0, time.time()
    try:
        # Якорь владельца (23.09) живёт локально, но лейбл/жанр скачанного
        # добирается витриной — здесь, в сетевом темпе радара, а не в ленте:
        # всё добытое ложится на диск и больше не спрашивается.
        try:
            from . import owner_anchor
            await owner_anchor.enrich(entries, cap=6, budget_sec=4.0)
        except Exception as ex:                           # noqa: BLE001
            print(f"[identity] обогащение якоря: {ex}", flush=True)
        for e in todo[:max(0, int(cap))]:
            if time.time() - started > budget_sec:
                break
            try:
                await bind(e, client, cfg, services=services,
                           spotify_state=spotify_state,
                           genre_hint=str((genre_hints or {}).get(str(e.get("name")), "")))
                done += 1
            except Exception as ex:                           # noqa: BLE001
                print(f"[identity] bind «{e.get('name')}»: {ex}", flush=True)
    finally:
        if own:
            await client.aclose()
    if done or len(todo) > cap:
        print(f"[identity] привязано {done} (из {len(todo)} ждущих)", flush=True)
    return done


def set_choice(entry: dict, hide: Optional[list] = None,
               hide_titles: Optional[list] = None,
               show_titles: Optional[list] = None) -> dict:
    """Решение владельца о склеенной странице — без нового запроса к витринам.

    Данные не выбирают, какую из двух личностей человек и имел в виду, подписывая
    id: обе лежат на нём. Поэтому скрываем ровно то, что он назвал сам, — по
    составу лейбловых групп, сохранённому в профиле. Выбор переживает следующий
    привязочный проход (`identity["choice"]`).

    `show_titles` — обратный ход: релиз, спрятанный автоправилом якоря, владелец
    вернул себе. Он выигрывает у автоматики и у `hide_titles` этого же релиза.
    """
    ident = identity_of(entry)
    choice = dict(ident.get("choice") or {})
    if hide is not None:
        choice["hide"] = [str(x) for x in hide]
    if hide_titles is not None:
        choice["hide_titles"] = [str(x) for x in hide_titles]
    if show_titles is not None:
        keep = {tkey(str(t)) for t in show_titles}
        back = sorted(keep | {tkey(str(t)) for t in (choice.get("show_titles") or [])})
        choice["show_titles"] = back
        # Возвращённое больше не может числиться спрятанным по имени релиза.
        choice["hide_titles"] = [str(t) for t in (choice.get("hide_titles") or [])
                                 if tkey(str(t)) not in keep]
    ident["choice"] = choice

    prof = dict(ident.get("profile") or {})
    artist = str(entry.get("name") or "")
    groups = {label_key(str(k), artist): set(v or [])
              for k, v in (prof.get("group_titles") or {}).items()}
    hidden: set = set()
    for lbl in choice.get("hide") or []:
        hidden |= groups.get(label_key(str(lbl), artist), set())
    for t in choice.get("hide_titles") or []:
        hidden.add(tkey(str(t)))
    hidden -= {tkey(str(t)) for t in (choice.get("show_titles") or [])}
    visible = ({t for t in (prof.get("titles") or [])}
               | {t for t in (prof.get("hidden_titles") or [])}
               | {tkey(str(t)) for t in (choice.get("show_titles") or [])}) - hidden
    prof["hidden_titles"] = sorted(hidden)[:_TITLES_CAP]
    prof["titles"] = sorted(visible)[:_TITLES_CAP]
    prof["merged"] = bool(hidden)
    prof["two_people"] = bool(hidden) or bool(prof.get("two_people"))
    ident["profile"] = prof
    entry["identity"] = ident
    ident["two_people"] = two_people(entry)
    ident["needs_owner"] = needs_owner(entry)
    ident["choice_ts"] = time.time()
    entry["identity"] = ident
    return prof


def summary(entries: list) -> dict:
    """Для владельца и отчётов: сколько подписок с чем.

    `stale` и `repairs` — про самоизлечение: первое говорит, сколько подписок
    ещё проверялись СТАРЫМИ правилами (и разберутся в темпе радара), второе —
    сколько подтверждений уже переложились на другой id, то есть сколько чужих
    склеек сняли.

    `anchors` / `no_anchor` — про правило якоря (23.09): сколько подписок вообще
    чем судить, а сколько остаются на старых данных. Скрывать «нет якоря» было
    бы хуже самой цензуры: владелец обязан видеть, где автомат молчит.
    """
    out = {"confirmed": 0, "pending": 0, "conflict": 0, "hidden": 0, "unbound": 0,
           "merged": 0, "needs_owner": 0, "stale": 0, "repairs": 0,
           "ambiguous": 0, "auto_hidden": [], "no_anchor": [], "items": []}
    try:
        from . import owner_anchor
    except Exception:                                          # noqa: BLE001
        owner_anchor = None
    for e in entries or []:
        if not e.get("artist_id") or e.get("kind") == "label":
            continue
        ident = identity_of(e)
        anchor = owner_anchor.describe(e) if owner_anchor else {"anchor": None}
        if not anchor.get("anchor"):
            out["no_anchor"].append(str(e.get("name") or ""))
        if rules_stale(e):
            out["stale"] += 1
        out["repairs"] += len(ident.get("repairs") or [])
        if ident.get("ambiguous_names"):
            out["ambiguous"] += 1
        if not ident.get("services"):
            out["unbound"] += 1
            continue
        st = [(s, (r or {}).get("status")) for s, r in ident["services"].items()]
        prof = ident.get("profile") or {}
        pending = [s for s, r in st if r == PENDING]
        if any(s == CONFIRMED for _, s in st):
            out["confirmed"] += 1
        if pending:
            out["pending"] += 1
        if ident.get("conflict"):
            out["conflict"] += 1
        if prof.get("merged"):
            out["merged"] += 1
        owner = needs_owner(e)
        if owner:
            out["needs_owner"] += 1
        hid = len(ident.get("hidden") or [])
        out["hidden"] += hid
        for h in ident.get("hidden") or []:
            if str(h.get("reason") or "").startswith("авто:"):
                out["auto_hidden"].append({
                    "artist": e.get("name"), "title": h.get("title"),
                    "label": h.get("label") or "", "genres": h.get("genres") or [],
                    "service": h.get("service") or "", "id": h.get("id") or "",
                    "reason": h.get("reason")})
        # «Не разрешилось» должно быть видно: подписка, чью личность ни одна
        # витрина не подтвердила, владельцу заметна сама по себе — её релизы из
        # чужих витрин в ленту не выйдут, и без объяснения это выглядит как
        # молчаливая потеря находок.
        if (hid or ident.get("conflict") or prof.get("merged") or owner
                or pending or ident.get("repairs") or rules_stale(e)
                or anchor.get("anchor")):
            out["items"].append({
                "name": e.get("name"), "id": str(e.get("artist_id") or ""),
                "services": dict(st), "hidden": hid, "pending_services": pending,
                "merged": bool(prof.get("merged")),
                "two_people": two_people(e),
                "needs_owner": bool(owner),
                "ambiguous": ident.get("ambiguous_names") or {},
                "repairs": ident.get("repairs") or [],
                "stale": rules_stale(e),
                "persons": prof.get("persons") or [],
                "groups": prof.get("groups") or {},
                "group_titles": prof.get("group_titles") or {},
                "hide": (ident.get("choice") or {}).get("hide") or [],
                "show_titles": (ident.get("choice") or {}).get("show_titles") or [],
                "anchor": anchor,
            })
    out["anchors"] = sum(1 for i in out["items"] if (i["anchor"] or {}).get("anchor"))
    return out


def auto_pull_ok(entry: dict, rel: dict) -> tuple[bool, str]:
    """Можно ли СКАЧИВАТЬ релиз молча, без владельца.

    Посмотреть и скачать — разные риски: карточку однофамильца владелец
    отметит глазами, а чужой альбом уедет к нему в библиотеку и останется там.
    Поэтому автозакачка закрыта ровно до тех пор, пока личность подписки не
    решена: страница склеена без выбора владельца (`needs_owner`) или в витрине
    под этим именем подтверждены ДВОЕ. Показывать при этом релиз ничего не
    мешает: цензура ленты (`home_show`) остаётся отдельной дверью.
    """
    ok, why = home_show(entry, rel)
    if not ok:
        return False, why
    if needs_owner(entry):
        return False, ("личность подписки ещё не решена: в ленте два "
                       "однофамильца, владелец не выбрал своего")
    return True, ""
