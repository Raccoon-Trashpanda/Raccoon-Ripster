"""Свой аудиотракт для ПК-версии: вывод в обход микшера Windows.

Владелец 05.09.2026: «аудио движок кстати должен быть и в пк версии».

Зачем он вообще нужен, замерено на машине владельца в тот же день:

    WASAPI exclusive   44100 Гц → открылся
                       48000 Гц → открылся
                       96000 Гц → открылся
    WASAPI shared      44100 Гц → Invalid sample rate
                       48000 Гц → открылся
                       96000 Гц → Invalid sample rate

Общий режим принимает ровно одну частоту — ту, на которой стоит микшер
Windows. Всё остальное он пересчитывает. Браузеру доступен ТОЛЬКО общий режим,
поэтому web-плеер Ripster физически не может отдать 44.1 кГц как есть: даже
идеальный gapless на Web Audio играет через ресемпл в 48. Эксклюзивный режим
забирает устройство себе и принимает частоту файла — это и есть bit-perfect.

Чего этот модуль НЕ делает и почему:

* не заменяет web-плеер. Тот играет потоки, HLS, чужие сервисы и работает
  на телефоне через браузер. Здесь — локальные файлы фонотеки, где и есть
  смысл в bit-perfect;
* не решает за человека. Устройство и режим выбираются явно: эксклюзивный
  захват отбирает звук у всей системы, и делать это молча нельзя;
* не сочиняет слов для экрана. Отдаёт факт ([Playback.bit_perfect],
  частоты), текст подбирает интерфейс — тот же урок, что с мобильным
  движком, где `formatLine()` возвращал готовую русскую строку.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

#: Расширения, которые читает libsndfile. MP3/AAC сюда не входят намеренно:
#: у них своя история с декодером, а bit-perfect для lossy лишён смысла.
#: `.ogg` здесь БЫЛ и убран 19.09.2026: libsndfile его читает, но Ogg — почти
#: всегда Vorbis, то есть сжатие С ПОТЕРЯМИ, и движок выдавал бы ему
#: `bit_perfect=True`. От этого спасала только проверка на фронте
#: (player.js) — одна линия защиты вместо двух. См. скилл ripster-audio-integrity.
LOSSLESS_EXT = {".flac", ".wav", ".aiff", ".aif", ".w64"}


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    default_rate: int


@dataclass
class Playback:
    """Что происходит сейчас. Числа, не фразы."""

    path: str = ""
    playing: bool = False
    #: Частота файла и та, что реально открылась. Разошлись — не bit-perfect.
    file_rate: int = 0
    granted_rate: int = 0
    channels: int = 0
    bits: int = 0
    exclusive: bool = False
    position_sec: float = 0.0
    duration_sec: float = 0.0
    #: Пауза: поток открыт и устройство наше, но кадры не льются. Отличается от
    #: `playing=False` тем, что возвращаться некуда — достаточно снять паузу.
    paused: bool = False
    #: Файл доиграл САМ. Без этого признака интерфейс не отличает «кончился»
    #: от «остановили» и либо не включает следующий трек, либо включает его
    #: после нажатия «стоп» — обе ошибки одинаково заметны.
    finished: bool = False
    #: Почему не играет. Пусто — всё в порядке.
    error: str = ""

    @property
    def bit_perfect(self) -> bool:
        """Звук уходит в устройство ровно таким, каким лежит в файле."""
        return (
            self.exclusive
            and self.file_rate > 0
            and self.file_rate == self.granted_rate
        )


def available() -> bool:
    """Есть ли на этой машине чем играть нативно."""
    try:
        import sounddevice  # noqa: F401
        import soundfile  # noqa: F401
    except Exception:
        return False
    return True


def devices() -> list[Device]:
    """Устройства вывода WASAPI. Пустой список — WASAPI недоступен.

    Только WASAPI: эксклюзивный режим есть именно у него. MME и DirectSound
    в списке были бы обманом — bit-perfect они не дадут никогда.
    """
    if not available():
        return []
    import sounddevice as sd

    try:
        apis = sd.query_hostapis()
        wasapi = next((i for i, a in enumerate(apis) if "WASAPI" in a["name"]), None)
        if wasapi is None:
            return []
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and d["hostapi"] == wasapi:
                out.append(Device(i, d["name"], int(d["default_samplerate"])))
        return out
    except Exception:
        return []


def _co_init() -> None:
    """Инициализировать COM для текущего потока (Windows).

    Вне Windows и при повторном вызове — молча ничего не делает: COM отвечает
    S_FALSE на повторную инициализацию того же потока, и это не ошибка.
    """
    try:
        import ctypes
        # COINIT_APARTMENTTHREADED = 0x2 — то, чего ждёт WASAPI.
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    except Exception:
        pass


def pick_device(rate: int, channels: int) -> int | None:
    """Выбрать устройство, которое РЕАЛЬНО откроется на этой частоте.

    Просто «первое WASAPI» не годится: первым в списке оказался «Steam
    Streaming Microphone» — устройство есть, а играть через него нельзя
    («Invalid device», поймано 05.09.2026). Поэтому кандидаты пробуются
    открытием, и берётся тот, что открылся: проверка честнее догадки по имени.

    Порядок кандидатов — системное устройство по умолчанию первым, если оно
    среди WASAPI: человек выбрал его сам, и уводить звук в другое место без
    спроса нельзя.
    """
    if not available():
        return None
    import sounddevice as sd

    cands = devices()
    if not cands:
        return None
    try:
        default_out = sd.query_devices(kind="output")["name"]
        cands.sort(key=lambda d: 0 if d.name.startswith(default_out[:20]) else 1)
    except Exception:
        pass

    for d in cands:
        try:
            st = sd.OutputStream(device=d.index, samplerate=rate, channels=channels,
                                 dtype="int16",
                                 extra_settings=sd.WasapiSettings(exclusive=True))
            st.close()
            return d.index
        except Exception:
            continue
    return None


class Engine:
    """Проигрывание локального файла в обход микшера.

    Один поток-читатель льёт кадры в открытый поток вывода. Никакого своего
    декодера: libsndfile читает FLAC/WAV/AIFF, и переписывать это на C++ ради
    ПК смысла нет — выигрыш даёт РЕЖИМ ВЫВОДА, а не декодирование.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        #: Взведён — кадры льются. Снят — пауза. Поток вывода при этом ОСТАЁТСЯ
        #: открытым: закрыть и открыть заново эксклюзивное устройство — это
        #: щелчок и риск, что его перехватит кто-то ещё, пока оно свободно.
        self._go = threading.Event()
        self._go.set()
        #: Куда перемотать, в секундах. Забирает рабочий поток: seek делает тот,
        #: кто держит файл, иначе две руки двигают один курсор.
        self._seek_to: float | None = None
        self._thread: threading.Thread | None = None
        self._state = Playback()

    # ── состояние ────────────────────────────────────────────────────────
    @property
    def state(self) -> Playback:
        with self._lock:
            return self._state

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self._state, k, v)

    # ── управление ───────────────────────────────────────────────────────
    def play(self, path: str | Path, device: int | None = None,
             exclusive: bool = True) -> Playback:
        """Начать проигрывание. Возвращает состояние сразу после открытия.

        Отказ открыть поток — это НЕ повод молча уйти в общий режим: человек
        включил bit-perfect, и подмена режима за его спиной обесценивает всю
        затею. Ошибка кладётся в состояние как есть.
        """
        self.stop()
        p = Path(path)
        # Формат проверяем ПЕРВЫМ: это свойство самой просьбы, известное без
        # диска, и причина точнее. Иначе на mp3 с опечаткой в пути человек
        # услышит «файла нет» и пойдёт искать файл, которого движок всё равно
        # не взял бы.
        if p.suffix.lower() not in LOSSLESS_EXT:
            self._set(error="not_lossless", path=str(p), playing=False)
            return self.state
        if not p.exists():
            self._set(error="file_not_found", path=str(p), playing=False)
            return self.state
        if not available():
            self._set(error="engine_unavailable", path=str(p), playing=False)
            return self.state

        import soundfile as sf

        try:
            info = sf.info(str(p))
        except Exception as e:
            self._set(error=f"unreadable: {type(e).__name__}", path=str(p), playing=False)
            return self.state

        self._stop.clear()
        self._set(
            path=str(p), file_rate=int(info.samplerate), channels=int(info.channels),
            bits=_bits_of(info.subtype), duration_sec=float(info.frames) / info.samplerate,
            position_sec=0.0, granted_rate=0, exclusive=exclusive, error="", playing=False,
            paused=False, finished=False,
        )
        self._go.set()
        with self._lock:
            self._seek_to = None
        # Эксклюзивный режим есть ТОЛЬКО у WASAPI. Устройство по умолчанию в
        # Windows принадлежит MME, и настройки WASAPI на нём дают
        # «Incompatible host API specific stream info» — поймано первым же
        # прогоном 05.09.2026. Не выбрали устройство — берём первое WASAPI.
        if exclusive and device is None:
            device = pick_device(int(info.samplerate), int(info.channels))
            if device is None:
                self._set(error="no_wasapi_device", playing=False)
                return self.state

        started = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(str(p), device, exclusive, started), daemon=True,
        )
        self._thread.start()
        started.wait(timeout=5.0)
        return self.state

    def stop(self) -> None:
        self._stop.set()
        # Снять паузу ПЕРЕД join: поток стоит на `self._go.wait()`, и без этого
        # остановка на паузе висела бы три секунды таймаута и уходила с живым
        # потоком — устройство осталось бы занятым.
        self._go.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None
        self._set(playing=False, paused=False)

    def pause(self) -> Playback:
        """Придержать кадры, не отпуская устройство."""
        if self._thread and self._thread.is_alive():
            self._go.clear()
            self._set(paused=True)
        return self.state

    def resume(self) -> Playback:
        if self._thread and self._thread.is_alive():
            self._go.set()
            self._set(paused=False)
        return self.state

    def seek(self, sec: float) -> Playback:
        """Перемотать внутри текущего файла.

        Ничего не играет — перематывать нечего, и это не ошибка: интерфейс
        может дёрнуть перемотку на остывшем плеере.
        """
        if not (self._thread and self._thread.is_alive()):
            return self.state
        st = self.state
        with self._lock:
            self._seek_to = clamp_seek(sec, st.duration_sec)
        # Перемотка на паузе должна СРАБОТАТЬ, а не ждать снятия паузы: человек
        # тянет полосу именно на паузе чаще всего. Позицию показываем сразу,
        # звук догонит на возобновлении.
        self._set(position_sec=clamp_seek(sec, st.duration_sec))
        return self.state

    # ── рабочий поток ────────────────────────────────────────────────────
    def _run(self, path: str, device: int | None, exclusive: bool,
             started: threading.Event) -> None:
        import sounddevice as sd
        import soundfile as sf

        # WASAPI — это COM, и COM инициализируется В КАЖДОМ ПОТОКЕ отдельно.
        # Без этого те же самые параметры, которые прекрасно открываются в
        # главном потоке, дают «Invalid device» здесь — час разбора 05.09.2026
        # ушёл именно на это: ошибка указывает на устройство, а виноват поток.
        _co_init()

        rate = ch = 0
        dt = "?"
        try:
            with sf.SoundFile(path) as f:
                extra = sd.WasapiSettings(exclusive=exclusive) if exclusive else None
                rate, ch = int(f.samplerate), int(f.channels)
                dt = "int32" if f.subtype in ("PCM_24", "PCM_32") else "int16"
                stream = sd.OutputStream(
                    device=device, samplerate=rate, channels=ch,
                    dtype=dt, extra_settings=extra,
                )
                stream.start()
                self._set(granted_rate=int(stream.samplerate), playing=True, error="")
                started.set()
                block = 8192
                done = 0
                ended_on_its_own = False
                while not self._stop.is_set():
                    # Пауза: ждём с таймаутом, чтобы `stop()` во время паузы не
                    # упирался в вечное ожидание.
                    if not self._go.wait(timeout=0.2):
                        continue
                    with self._lock:
                        target, self._seek_to = self._seek_to, None
                    if target is not None:
                        frame = int(target * f.samplerate)
                        f.seek(frame)
                        done = frame
                        self._set(position_sec=float(target))
                    data = f.read(block, dtype=stream.dtype, always_2d=True)
                    if len(data) == 0:
                        ended_on_its_own = True
                        break
                    stream.write(data)
                    done += len(data)
                    self._set(position_sec=done / f.samplerate)
                if ended_on_its_own:
                    self._set(finished=True)
                stream.stop()
                stream.close()
        except Exception as e:
            # Честная причина, а не тишина: «не открылось» и «файл кончился» —
            # разные события, и человек должен видеть первое.
            # В сообщение кладём ЧЕМ именно пытались играть: «Invalid device»
            # без устройства и частоты не даёт ничего для разбора.
            self._set(error=f"{type(e).__name__}: {e} [dev={device} rate={rate} ch={ch} dtype={dt}]"[:220])
        finally:
            self._set(playing=False)
            started.set()


def clamp_seek(sec: float, duration_sec: float) -> float:
    """Куда на самом деле встанет перемотка.

    Отдельной функцией, потому что это единственная часть перемотки, которую
    можно проверить без звуковой карты, а ошибиться в ней легко: отрицательная
    секунда роняет `f.seek`, а секунда за концом файла даёт мгновенный «трек
    закончился» — и очередь проматывает альбом целиком за пару секунд.
    """
    if duration_sec <= 0:
        return 0.0
    # Полсекунды у хвоста: ровно в конец вставать бессмысленно — файл сразу
    # кончится, и перемотка прочитается как пропуск трека.
    return max(0.0, min(float(sec), duration_sec - 0.5))


def _bits_of(subtype: str | None) -> int:
    s = (subtype or "").upper()
    for n in (8, 16, 24, 32):
        if str(n) in s:
            return n
    return 0


#: Единственный экземпляр: устройство эксклюзивно по определению, двух
#: одновременных проигрываний быть не может.
ENGINE = Engine()
