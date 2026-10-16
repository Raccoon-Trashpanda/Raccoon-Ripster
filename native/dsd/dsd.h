// DSD (.dsf / .dff) → PCM. Свой разбор контейнера и своя децимация.
//
// ПОЧЕМУ СВОЙ, А НЕ БИБЛИОТЕКА. Контейнеры DSD простые — заголовок и сырой
// поток однобитных отсчётов; вся работа не в разборе, а в фильтре. Тянуть
// ради этого чужой декодер с несвободной лицензией незачем.
//
// ПОЧЕМУ СРАЗУ В PCM. Отдать DSD «как есть» на Android некуда: DoP требует
// ЦАП, который его понимает, и USB-тракта в обход системы — у нас ни того,
// ни другого. Поэтому единственный честный путь — децимация в PCM. Так же
// поступает любой программный плеер без своего USB-драйвера; про сам DoP
// см. пункт a3 трекера.
//
// ЧАСТОТА ВЫХОДА 88200 Гц ВЫБРАНА, А НЕ СЛУЧАЙНА. DSD64 это 2822400 Гц,
// ровно 64×44100. Делим на 32 → 88200: кратность целая (никакого пересчёта
// частоты поверх), а полоса с запасом покрывает всё слышимое. 44100 брать
// хуже — пришлось бы душить фильтром вдвое агрессивнее у самой границы
// слышимости, а выигрыша нет.
//
// ФИЛЬТР. Однобитный поток несёт огромный ультразвуковой шум формирователя;
// без подавления он свернётся в слышимую полосу при децимации. Берём
// windowed-sinc на 128 отсчётов и считаем его через таблицы по байтам: для
// каждого из 16 байт окна и каждого из 256 значений байта частичная сумма
// посчитана заранее. Тогда один выходной отсчёт — это 16 сложений вместо
// 128 умножений.
#pragma once

#include <cmath>
#include <cstdint>
#include <cstring>
#include <vector>

namespace dsd {

// Длина фильтра в битах и в байтах. 128 бит — компромисс: короче даёт
// заметный шум обратно в полосе, длиннее не слышно, а считать дороже.
constexpr int kTaps      = 128;
constexpr int kTapBytes  = kTaps / 8;      // 16
constexpr int kOutRate   = 88200;

/** Готовые суммы: [позиция байта в окне][значение байта]. */
struct FirTable {
    float t[kTapBytes][256];
};

/** Посчитать таблицы один раз на процесс. */
inline const FirTable& firTable() {
    static FirTable tbl = [] {
        FirTable f{};
        double coef[kTaps];
        // Windowed-sinc, срез ~0.45 от половины выходной частоты. Окно
        // Блэкмана: у него боковые лепестки ниже, чем у Хэмминга, а для
        // подавления ультразвука шума важно именно это.
        const double fc = 0.45 / 32.0;     // относительно частоты DSD
        double sum = 0.0;
        for (int i = 0; i < kTaps; ++i) {
            const double m = i - (kTaps - 1) / 2.0;
            const double s = (m == 0.0) ? 2.0 * fc
                                        : std::sin(2.0 * M_PI * fc * m) / (M_PI * m);
            const double w = 0.42
                           - 0.5  * std::cos(2.0 * M_PI * i / (kTaps - 1))
                           + 0.08 * std::cos(4.0 * M_PI * i / (kTaps - 1));
            coef[i] = s * w;
            sum += coef[i];
        }
        // Нормируем по сумме, иначе громкость зависела бы от длины фильтра.
        for (double& c : coef) c /= sum;

        for (int b = 0; b < kTapBytes; ++b) {
            for (int v = 0; v < 256; ++v) {
                double acc = 0.0;
                for (int k = 0; k < 8; ++k) {
                    // Бит 1 → +1, бит 0 → −1: однобитный поток это знак, а не
                    // величина. Старший бит первый — порядок приводится к
                    // этому виду ещё при чтении (см. maybeFlip).
                    const bool bit = (v >> (7 - k)) & 1;
                    acc += coef[b * 8 + k] * (bit ? 1.0 : -1.0);
                }
                f.t[b][v] = static_cast<float>(acc);
            }
        }
        return f;
    }();
    return tbl;
}

/** Развернуть биты в байте: у DSF порядок бывает младшим вперёд. */
inline uint8_t flipBits(uint8_t v) {
    v = static_cast<uint8_t>((v & 0xF0u) >> 4 | (v & 0x0Fu) << 4);
    v = static_cast<uint8_t>((v & 0xCCu) >> 2 | (v & 0x33u) << 2);
    v = static_cast<uint8_t>((v & 0xAAu) >> 1 | (v & 0x55u) << 1);
    return v;
}

enum class Container { None, Dsf, Dff };

/**
 * Состояние декодера. Байты каждого канала лежат отдельно: и DSF (блоками по
 * каналу), и DFF (побайтно вперемешку) приводятся к одному виду сразу при
 * чтении — дальше фильтру всё равно, из какого контейнера пришло.
 */
struct Ctx {
    Container kind      = Container::None;
    int       channels  = 0;
    int64_t   dsdRate   = 0;      // 2822400 для DSD64
    int       decim     = 32;     // dsdRate / kOutRate
    bool      lsbFirst  = false;
    int64_t   dataStart = 0;      // смещение звука в файле
    int64_t   dataBytes = 0;      // сколько его всего
    int       blockSize = 4096;   // DSF: байт на канал в блоке; DFF: 1

    // Байты канала, уже в «старший бит первый». Держим окно фильтра плюс
    // запас: на каждый выходной отсчёт уходит decim/8 байт.
    std::vector<std::vector<uint8_t>> chan;
    int64_t   filePos   = 0;      // сколько байт данных уже прочитано
    int       have      = 0;      // сколько валидных байт в chan[*]
    int       cursor    = 0;      // позиция окна в chan[*]

    int64_t   totalFrames = 0;
};

inline uint32_t rdU32le(const uint8_t* p) {
    return (uint32_t) p[0] | ((uint32_t) p[1] << 8) | ((uint32_t) p[2] << 16) | ((uint32_t) p[3] << 24);
}
inline uint64_t rdU64le(const uint8_t* p) {
    uint64_t v = 0;
    for (int i = 7; i >= 0; --i) v = (v << 8) | p[i];
    return v;
}
inline uint64_t rdU64be(const uint8_t* p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; ++i) v = (v << 8) | p[i];
    return v;
}
inline uint32_t rdU32be(const uint8_t* p) {
    return ((uint32_t) p[0] << 24) | ((uint32_t) p[1] << 16) | ((uint32_t) p[2] << 8) | (uint32_t) p[3];
}

/** Читалка файла: тот же приём, что у остальных декодеров — pread по fd. */
using ReadAt = bool (*)(void* user, int64_t off, void* out, size_t bytes);

/**
 * Разобрать заголовок. false — это не DSD либо мы его не умеем (например DST:
 * данные там сжаты, и молча отдать тишину было бы хуже честного отказа).
 */
inline bool parse(Ctx& c, ReadAt readAt, void* user, int64_t fileSize, char* why, size_t whyLen) {
    auto fail = [&](const char* msg) {
        if (why && whyLen) std::snprintf(why, whyLen, "%s", msg);
        return false;
    };
    uint8_t hdr[96];
    if (!readAt(user, 0, hdr, 4)) return fail("short file");

    // ── DSF ────────────────────────────────────────────────────────────────
    if (std::memcmp(hdr, "DSD ", 4) == 0) {
        uint8_t fmt[52];
        if (!readAt(user, 28, fmt, sizeof fmt)) return fail("dsf: no fmt chunk");
        if (std::memcmp(fmt, "fmt ", 4) != 0) return fail("dsf: fmt chunk missing");
        // РАСКЛАДКА ЧАНКА `fmt` У DSF. Смещения считаются от начала чанка:
        //
        //    0  "fmt "                12  версия формата
        //    4  размер чанка (8, =52) 16  ИДЕНТИФИКАТОР ФОРМАТА (0 = сырой DSD)
        //                             20  тип каналов
        //                             24  число каналов
        //                             28  частота
        //                             32  бит на отсчёт (1 = младший первым, 8 = старший)
        //                             36  отсчётов на канал (8 байт)
        //                             44  размер блока на канал
        //                             48  резерв
        //
        // Первая версия читала всё это на 4 байта дальше, чем надо: идентификатор
        // брался с 20-го байта (то есть тип каналов), не совпадал с нулём — и
        // КАЖДЫЙ .dsf отвергался с «not raw DSD». Поймано самопроверкой
        // (dsd_selftest.cpp) на синтезированном файле: DFF при этом играл
        // безупречно, и без отдельной проверки DSF ошибка дожила бы до первого
        // живого файла у человека.
        const uint32_t formatId = rdU32le(fmt + 16);
        if (formatId != 0) return fail("dsf: not raw DSD");
        c.channels  = (int) rdU32le(fmt + 24);
        c.dsdRate   = (int64_t) rdU32le(fmt + 28);
        const uint32_t bps = rdU32le(fmt + 32);
        c.lsbFirst  = (bps == 1);            // 1 = младший бит первый, 8 = старший
        c.totalFrames = 0;                   // посчитаем ниже из sampleCount
        const uint64_t sampleCount = rdU64le(fmt + 36);
        c.blockSize = (int) rdU32le(fmt + 44);
        uint8_t dchunk[12];
        if (!readAt(user, 80, dchunk, sizeof dchunk)) return fail("dsf: no data chunk");
        if (std::memcmp(dchunk, "data", 4) != 0) return fail("dsf: data chunk missing");
        const uint64_t dataChunkSize = rdU64le(dchunk + 4);
        c.dataStart = 92;
        c.dataBytes = (int64_t) (dataChunkSize >= 12 ? dataChunkSize - 12 : 0);
        if (c.dataBytes <= 0 || c.dataStart + c.dataBytes > fileSize)
            c.dataBytes = fileSize - c.dataStart;
        c.kind = Container::Dsf;
        if (c.blockSize <= 0) c.blockSize = 4096;
        // sampleCount — однобитные отсчёты на канал.
        if (sampleCount > 0) c.totalFrames = (int64_t) (sampleCount / 32);
        }

    // ── DFF (DSDIFF) ───────────────────────────────────────────────────────
    else if (std::memcmp(hdr, "FRM8", 4) == 0) {
        uint8_t form[4];
        if (!readAt(user, 12, form, 4)) return fail("dff: short header");
        if (std::memcmp(form, "DSD ", 4) != 0) return fail("dff: not a DSD form");
        // Идём по чанкам верхнего уровня: 4 байта имени + 8 байт длины (BE).
        int64_t off = 16;
        int64_t propEnd = 0;
        while (off + 12 <= fileSize) {
            uint8_t ch[12];
            if (!readAt(user, off, ch, sizeof ch)) break;
            const uint64_t sz = rdU64be(ch + 4);
            const int64_t body = off + 12;
            if (std::memcmp(ch, "PROP", 4) == 0) {
                propEnd = body + (int64_t) sz;
                off = body + 4;              // внутрь PROP, пропустив тип 'SND '
                continue;
            }
            if (std::memcmp(ch, "FS  ", 4) == 0) {
                uint8_t v[4];
                if (readAt(user, body, v, 4)) c.dsdRate = (int64_t) rdU32be(v);
            } else if (std::memcmp(ch, "CHNL", 4) == 0) {
                uint8_t v[2];
                if (readAt(user, body, v, 2)) c.channels = (int) ((v[0] << 8) | v[1]);
            } else if (std::memcmp(ch, "DSD ", 4) == 0) {
                c.dataStart = body;
                c.dataBytes = (int64_t) sz;
                break;
            } else if (std::memcmp(ch, "DST ", 4) == 0) {
                // DST — это СЖАТЫЙ DSD, отдельный декодер. Не умеем и не
                // притворяемся: молчаливая тишина хуже честного отказа.
                return fail("dff: DST compression is not supported");
            }
            off = body + (int64_t) sz + ((sz & 1u) ? 1 : 0);
            if (propEnd && off >= propEnd) propEnd = 0;
        }
        if (!c.dataStart) return fail("dff: no DSD chunk");
        c.lsbFirst  = false;                 // DFF всегда старшим битом вперёд
        c.blockSize = 1;                     // байты каналов чередуются
        c.kind = Container::Dff;
    } else {
        return fail("not a DSD file");
    }

    if (c.channels <= 0 || c.channels > 8) return fail("bad channel count");
    if (c.dsdRate <= 0) return fail("bad sample rate");
    if (c.dsdRate % kOutRate != 0) return fail("sample rate is not a multiple of 88200");
    c.decim = (int) (c.dsdRate / kOutRate);
    // Шаг должен быть целым числом байт: иначе окно пришлось бы двигать по
    // битам, а это на порядок дороже без единого выигрыша в звуке.
    if (c.decim % 8 != 0) return fail("decimation is not byte aligned");
    if (c.dataBytes <= 0) return fail("empty data chunk");

    if (!c.totalFrames) {
        const int64_t perChan = c.dataBytes / c.channels;
        c.totalFrames = perChan * 8 / c.decim;
    }
    c.chan.assign((size_t) c.channels, {});
    return true;
}

/**
 * Подтянуть байты каналов из файла, сохранив хвост окна.
 *
 * Окно фильтра шире шага, поэтому между вызовами нужно оставлять последние
 * kTapBytes-1 байт: без этого на каждой границе блока получался бы разрыв —
 * ровно те щелчки, что описаны в пункте a7 трекера.
 */
inline bool fill(Ctx& c, ReadAt readAt, void* user, int wantBytes) {
    const int keep = kTapBytes - 1;
    for (auto& v : c.chan) {
        if ((int) v.size() > keep && c.cursor >= keep)
            v.erase(v.begin(), v.begin() + (v.size() - keep));
    }
    c.cursor = (int) (c.chan[0].size() >= (size_t) keep ? keep : c.chan[0].size());
    c.have   = (int) c.chan[0].size();

    while (c.have - c.cursor < wantBytes + kTapBytes) {
        const int64_t left = c.dataBytes - c.filePos;
        if (left <= 0) break;

        if (c.kind == Container::Dsf) {
            // DSF: блок целиком по каналу, каналы подряд.
            const int64_t need = (int64_t) c.blockSize * c.channels;
            const int64_t take = need < left ? need : left;
            std::vector<uint8_t> buf((size_t) take);
            if (!readAt(user, c.dataStart + c.filePos, buf.data(), (size_t) take)) break;
            c.filePos += take;
            const int per = (int) (take / c.channels);
            for (int ch = 0; ch < c.channels; ++ch) {
                const uint8_t* src = buf.data() + (size_t) ch * c.blockSize;
                for (int i = 0; i < per; ++i)
                    c.chan[(size_t) ch].push_back(c.lsbFirst ? flipBits(src[i]) : src[i]);
            }
            c.have += per;
        } else {
            // DFF: байты каналов чередуются по одному.
            const int64_t chunk = 8192LL * c.channels;
            const int64_t take = chunk < left ? chunk : left;
            std::vector<uint8_t> buf((size_t) take);
            if (!readAt(user, c.dataStart + c.filePos, buf.data(), (size_t) take)) break;
            c.filePos += take;
            const int per = (int) (take / c.channels);
            for (int i = 0; i < per; ++i)
                for (int ch = 0; ch < c.channels; ++ch)
                    c.chan[(size_t) ch].push_back(buf[(size_t) i * c.channels + ch]);
            c.have += per;
        }
    }
    return c.have - c.cursor >= kTapBytes;
}

/**
 * Выдать [frames] кадров PCM (интерливнуто). Возвращает, сколько получилось;
 * 0 — поток кончился.
 */
inline int64_t decode(Ctx& c, ReadAt readAt, void* user, float* out, int64_t frames) {
    if (c.channels <= 0 || frames <= 0) return 0;
    const FirTable& tbl = firTable();
    const int step = c.decim / 8;                 // байт на один выходной кадр
    int64_t done = 0;

    while (done < frames) {
        if (c.have - c.cursor < kTapBytes) {
            const int want = (int) ((frames - done) * step);
            if (!fill(c, readAt, user, want > 0 ? want : step)) break;
            if (c.have - c.cursor < kTapBytes) break;
        }
        const int64_t canHere = (c.have - c.cursor - kTapBytes) / step + 1;
        const int64_t n = (frames - done) < canHere ? (frames - done) : canHere;
        for (int64_t k = 0; k < n; ++k) {
            const int at = c.cursor + (int) (k * step);
            for (int ch = 0; ch < c.channels; ++ch) {
                const uint8_t* p = c.chan[(size_t) ch].data() + at;
                float acc = 0.0f;
                for (int b = 0; b < kTapBytes; ++b) acc += tbl.t[b][p[b]];
                out[(done + k) * c.channels + ch] = acc;
            }
        }
        c.cursor += (int) (n * step);
        done += n;
        if (n == 0) break;
    }
    return done;
}

/** Перемотка. Точность — до байта, то есть до 8 однобитных отсчётов. */
inline bool seek(Ctx& c, int64_t frame) {
    if (frame < 0) frame = 0;
    const int step = c.decim / 8;
    int64_t byteOff = frame * step;               // байт на канал от начала
    const int64_t perChan = c.dataBytes / (c.channels ? c.channels : 1);
    if (byteOff > perChan) byteOff = perChan;

    if (c.kind == Container::Dsf) {
        // Прыгать можно только на границу блока: внутри блока канал лежит
        // сплошняком, и начать с середины значит разъехаться по каналам.
        const int64_t blocks = byteOff / c.blockSize;
        c.filePos = blocks * c.blockSize * c.channels;
    } else {
        c.filePos = byteOff * c.channels;
    }
    for (auto& v : c.chan) v.clear();
    c.have = 0;
    c.cursor = 0;
    return true;
}

}  // namespace dsd
