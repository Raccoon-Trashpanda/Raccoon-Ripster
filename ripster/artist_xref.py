"""Один артист — разные id в разных каталогах.

Радар устроен по сервисам: каждый источник читает подписки СВОЕГО аккаунта и
отдаёт релизы оттуда же. Из-за этого артист виден только тому источнику, в
котором на него подписаны, и витрина, где релиз появляется РАНЬШЕ всех, до ленты
не доходит вовсе.

Живой разбор (29.07.2026, Sultan + Shepard — «Centuries»): релиз лежал в Tidal NZ
за сутки до мировой даты, `streamReady=True`, LOSSLESS; в вишлисте артист записан
со службой apple, у Apple релиза ещё не было — радар честно показывал пустоту, а
Tidal-источник молчал, потому что в служебном Tidal-аккаунте 0 подписок. Ровно то
преимущество, ради которого новозеландский аккаунт и заводился, не использовалось.

Здесь живут ТОЛЬКО вещи, в которых имя не опознаёт человека:

  norm()                       — сравнение написания имён («Sultan + Shepard» и
                                 «Sultan & Shepard» — один человек, «Sultan & Ned
                                 Shepard» — ДРУГОЙ);
  SERVICES, has_credentials()  — какую витрину вообще есть чем спросить.

Резолвера «имя → id» здесь больше нет — он и был причиной жалобы с 03.09.2026
(«BOP», «Solomon Grey»): точного совпадения имени плюс жребия по популярности
хватало, чтобы принять ЧУЖОГО однофамильца, и его релизы наполняли ленту и уходили
в авто-скачивание. Теперь имя только находит КАНДИДАТОВ, а принимает их
`ripster.artist_identity` — по общим работам (ISRC/UPC, пересечение дискографий,
лейблы). Вернуть сюда вывод «нашёл по имени, значит это он» — значит вернуть баг.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Витрины, где у артиста бывает ОТДЕЛЬНЫЙ id — Apple не входит: его id уже лежит
# в записи подписки (`artist_id`).
SERVICES = ("tidal", "qobuz", "deezer")

_cfg: dict = {}


def configure(cfg: dict, base_dir: Path) -> None:
    """`base_dir` нужен только MusicBrainz: он делит с нами каталог кэша."""
    global _cfg
    _cfg = cfg
    # MusicBrainz-дизамбигуация делит кэш-каталог с нами.
    try:
        from . import musicbrainz as _mb
        _mb.configure(Path(base_dir))
    except Exception:  # noqa: BLE001
        pass


def norm(name: str) -> str:
    """Имя артиста в виде, пригодном для сравнения между каталогами.

    Диакритика снимается («Ørjan Nilsen» и «Orjan Nilsen» — один человек), а
    соединители приводятся к одному слову: «Sultan + Shepard», «Sultan &
    Shepard» и «Sultan and Shepard» пишутся в витринах вперемешку. При этом
    «Sultan & Ned Shepard» остаётся ОТДЕЛЬНЫМ именем — лишнее слово никуда не
    девается, и это тот случай, ради которого сверка вообще существует.
    """
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("Ø", "O").replace("ø", "o").replace("Đ", "D").replace("đ", "d")
    s = s.replace("ß", "ss").replace("Æ", "AE").replace("æ", "ae")
    s = s.lower()
    s = re.sub(r"[&+]", " and ", s)
    s = re.sub(r"\bfeat\.?\b|\bfeaturing\b|\bvs\.?\b|\bpres\.?\b", " ", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def has_credentials(service: str) -> bool:
    """Есть ли чем спрашивать каталог. Без этого «не нашли» — не факт, а враньё."""
    c = _cfg or {}
    if service == "tidal":
        return bool(str(c.get("tidal-token") or "").strip())
    if service == "qobuz":
        return bool(str(c.get("qobuz-auth-token") or "").strip())
    if service == "deezer":
        return True                      # публичный поиск, токен не нужен
    return False
