"""Сведение жанра из ВСЕХ источников, с самокоррекцией.

Владелец 05.09.2026: «наша цель это идеальные теги, без компромиссов… берёшь
всё… модуль сам корректировал себя, дополнял либо наоборот убирал лишнее».

Здесь не выбирается «лучший источник» — здесь выслушиваются все и сводится
одно. Три вещи, которые делают это не «голосованием большинства»:

1. У КАЖДОГО ИСТОЧНИКА СВОЯ ОБЛАСТЬ. Beatport точен внутри танцевальной
   электроники и неверен вне её (Massive Attack → «Pop»). Поэтому вес голоса
   зависит не только от источника, но и от того, о чём он говорит.

2. СОГЛАСИЕ УСИЛИВАЕТ. Один источник, сказавший «trip hop», — гипотеза; три —
   ответ. Считаем не голоса, а СУММУ ВЕСОВ по канону: разные слова («Trip
   Hop», «trip-hop», «Трип-хоп») сначала сводятся к одному ключу.

3. МОДУЛЬ ПРАВИТ СЕБЯ САМ. После каждого разбора он смотрит, кто оказался
   согласен с итогом, а кто нет, и меняет вес источника — отдельно по каждой
   области. Источник, который в электронике прав, а в джазе врёт, со временем
   получает по этим областям разные веса, и никакого списка исключений в коде
   для этого не нужно.

Что значит «убирать лишнее»: теги, оказавшиеся заведомо не-жанрами (город,
десятилетие, настроение), отсекаются в [ripster.genre_sources]; а ярлыки,
которые раз за разом расходятся с итогом, теряют вес сами и перестают влиять.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

# Стартовые веса. Это ПОСЕВ, а не приговор: дальше они меняются замером.
# Числа взяты из проверки 05.09.2026 (Beatport 10/10 по трекам внутри своей
# области, Discogs 8/8 включая контрпримеры, Bandcamp точен но шумен,
# MusicBrainz шумит, потоки дают только вёдра).
SEED_TRUST: dict[str, float] = {
    "beatport": 1.00,
    "discogs": 0.95,
    # Bandcamp точен там, где артист САМ там живёт (Timecop1983 → synthwave),
    # и подводит на именитых, чьим именем подписываются авторы эдитов. Ставим
    # его вровень с MusicBrainz: дальше самокоррекция разведёт их по областям
    # сама, а высокий посев не даёт ей на это шанса — он выигрывает сразу.
    "bandcamp": 0.50,
    "musicbrainz": 0.50,
    "apple": 0.35,
    "deezer": 0.35,
    "yandex": 0.35,
    "qobuz": 0.35,
    "tidal": 0.35,
}

# Ярлыки-вёдра: они не ошибка, но и не ответ. Голос за ведро считается с
# сильным понижением — иначе широкое «Electronic» от четырёх потоков
# перевесит точный «Melodic House & Techno» от Beatport.
BUCKETS = {
    "music", "electronic", "electronica", "dance", "pop", "rock", "other",
    "alternative", "indie", "world", "worldwide", "soundtrack", "various",
}

# Уточнения, а не жанры. У Discogs в `styles` рядом с настоящими стилями лежат
# слова, описывающие ПОДАЧУ, а не направление: «Conscious» (о текстах),
# «Acoustic» (о составе), «Abstract», «Contemporary», «Experimental». Замер по
# 40 трекам 05.09.2026: Kendrick Lamar и A Tribe Called Quest получили
# «Conscious», Ali Farka Touré — «Acoustic», Arvo Pärt — «Contemporary».
# Формально это их стили, но на вопрос «что это за музыка» они не отвечают.
#
# Не выбрасываем: если сказать больше нечего, уточнение честнее пустоты.
# Понижаем — так же, как вёдра.
MODIFIERS = {
    "conscious", "acoustic", "abstract", "contemporary", "experimental",
    "instrumental", "vocal", "modern", "classic", "traditional", "minimal",
    "ambient/experimental", "leftfield", "downbeat",
}

_WORD = re.compile(r"[a-zа-яё0-9]+", re.I)


def canon(label: str) -> str:
    """Ключ ярлыка: только буквы и цифры, в нижнем регистре.

    «Trip Hop», «trip-hop» и «Trip_Hop» — один и тот же жанр, и считать их
    порознь значит развалить согласие на ровном месте.
    """
    return "".join(_WORD.findall((label or "").lower()))


def is_bucket(label: str) -> bool:
    return canon(label) in {canon(b) for b in BUCKETS}


def is_modifier(label: str) -> bool:
    """Слово описывает подачу, а не направление. См. MODIFIERS."""
    return canon(label) in {canon(m) for m in MODIFIERS}


class GenreResolver:
    """Сводит ярлыки и учится на своих же разборах."""

    def __init__(self, store: Path | None = None):
        self.store = store
        self.trust: dict[str, dict[str, float]] = {}   # источник → область → вес
        self.seen: dict[str, int] = {}                 # источник → сколько раз участвовал
        if store and store.exists():
            try:
                data = json.loads(store.read_text(encoding="utf-8"))
                self.trust = data.get("trust") or {}
                self.seen = data.get("seen") or {}
            except Exception:
                pass                                   # битый файл — начинаем с посева

    # ── веса ────────────────────────────────────────────────────────────────

    def weight(self, source: str, area: str) -> float:
        """Вес голоса источника в этой области.

        Область — это грубая семья жанра («electronic», «rock», «jazz»…).
        Пока по области ничего не измерено, действует посевной вес источника:
        отсутствие опыта не повод молчать, но и не повод доверять сверх меры.
        """
        seed = SEED_TRUST.get(source, 0.3)
        learned = (self.trust.get(source) or {}).get(area)
        if learned is None:
            return seed
        # Смешиваем посев и выученное: одно наблюдение не должно переворачивать
        # оценку источника, но сотня — должна.
        n = self.seen.get(f"{source}|{area}", 0)
        k = min(1.0, n / 20.0)
        return seed * (1 - k) + learned * k

    def _learn(self, source: str, area: str, agreed: bool) -> None:
        row = self.trust.setdefault(source, {})
        cur = row.get(area, SEED_TRUST.get(source, 0.3))
        # Небольшой шаг: модуль должен меняться от опыта, а не дёргаться.
        row[area] = max(0.05, min(1.0, cur + (0.04 if agreed else -0.06)))
        key = f"{source}|{area}"
        self.seen[key] = self.seen.get(key, 0) + 1

    def save(self) -> None:
        if not self.store:
            return
        try:
            self.store.write_text(
                json.dumps({"trust": self.trust, "seen": self.seen},
                           ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    # ── сведение ────────────────────────────────────────────────────────────

    def resolve(self, votes: dict[str, list[str]], area_hint: str = "") -> dict:
        """Свести голоса в один ответ.

        `votes` — {источник: [ярлыки от него, от сильного к слабому]}.
        Возвращает `{"genre", "confidence", "evidence", "dropped", "area"}`.

        Порядок ярлыков внутри источника значим: первый — его главный ответ,
        последующие идут с затуханием. Это не «все теги равны»: у Bandcamp
        десять тегов, и если считать их наравне, один болтливый источник
        перевесит остальных числом.
        """
        score: dict[str, float] = {}
        label_of: dict[str, str] = {}
        by_key: dict[str, list[str]] = {}

        for src, labels in (votes or {}).items():
            for i, lab in enumerate(labels or []):
                k = canon(lab)
                if not k:
                    continue
                w = self.weight(src, area_hint or "")
                w *= 1.0 / (1.0 + i)                # затухание по порядку
                if is_bucket(lab):
                    w *= 0.25                       # ведро — не ответ
                elif is_modifier(lab):
                    w *= 0.35                       # уточнение — почти не ответ
                score[k] = score.get(k, 0.0) + w
                label_of.setdefault(k, lab)
                by_key.setdefault(k, []).append(src)

        if not score:
            return {"genre": None, "confidence": 0.0, "evidence": {},
                    "dropped": [], "area": area_hint or ""}

        best_k = max(score, key=lambda k: score[k])
        total = sum(score.values()) or 1.0
        conf = score[best_k] / total

        # Что отбросили и почему — часть ответа, а не отладочная роскошь.
        dropped = [label_of[k] for k in score if k != best_k]

        return {
            "genre": label_of[best_k],
            "confidence": round(conf, 3),
            "evidence": {label_of[k]: sorted(set(by_key[k])) for k in score},
            "dropped": dropped,
            "area": area_hint or "",
        }

    def resolve_and_learn(self, votes: dict[str, list[str]], area_hint: str = "") -> dict:
        """Свести и тут же поправить веса источников по итогу.

        Согласившийся с итогом получает прибавку, разошедшийся — убавку, и
        всё это ОТДЕЛЬНО ПО ОБЛАСТИ. Так источник, который прав в электронике
        и врёт в джазе, сам разъезжается по областям, без единой строки
        исключений в коде.
        """
        out = self.resolve(votes, area_hint)
        if not out["genre"]:
            return out
        area = area_hint or canon(out["genre"])[:12]
        win = canon(out["genre"])
        for src, labels in (votes or {}).items():
            if not labels:
                continue
            self._learn(src, area, any(canon(l) == win for l in labels))
        return out


def confidence_word(c: float) -> str:
    """Словом — для журнала. Не для интерфейса: там свои переводы."""
    if c >= 0.6:
        return "уверенно"
    if c >= 0.35:
        return "похоже"
    return "слабо"


def entropy(votes: dict[str, list[str]]) -> float:
    """Насколько источники разошлись. 0 — полное согласие.

    Нужна не для красоты: высокий разброс означает, что ответ ненадёжен, даже
    если у победителя набралось больше всех. Такой случай честнее отдать как
    «не знаю», чем как ответ.
    """
    cnt: dict[str, int] = {}
    for labels in (votes or {}).values():
        for lab in (labels or [])[:1]:
            k = canon(lab)
            if k:
                cnt[k] = cnt.get(k, 0) + 1
    n = sum(cnt.values())
    if n <= 1:
        return 0.0
    return -sum((v / n) * math.log(v / n, 2) for v in cnt.values())
