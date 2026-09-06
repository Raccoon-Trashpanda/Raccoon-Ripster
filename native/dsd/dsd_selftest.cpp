// Проверка DSD-декодера БЕЗ телефона: собирается обычным g++ и гоняется на ПК.
//
// Зачем именно так. `dsd.h` намеренно не знает ни про Android, ни про Oboe —
// он берёт колбэк чтения и отдаёт PCM. Значит его можно проверить там, где
// проверять дёшево: синтезируем файл с ИЗВЕСТНЫМ тоном, декодируем и смотрим,
// вышел ли на выходе тот самый тон. Слушать телефон ушами такой проверки не
// заменяет, но ловит ровно то, что ушами и не расслышать: перепутанный порядок
// бит, разъехавшиеся каналы, кривую децимацию.
//
// Сборка:
//   g++ -O2 -std=c++17 -o dsd_selftest dsd_selftest.cpp
//
// Тон синтезируется дельта-сигма модулятором второго порядка — так же, как
// DSD и записывают: один бит на отсчёт, а «громкость» несёт плотность единиц.

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "dsd.h"

namespace {

constexpr int64_t kDsdRate = 2822400;      // DSD64
constexpr double kTone = 1000.0;           // Гц
constexpr double kAmp = 0.5;

/** Один бит на отсчёт: дельта-сигма второго порядка. */
std::vector<uint8_t> makeDsdBits(int channels, double seconds, double toneHz) {
    const int64_t n = (int64_t) (kDsdRate * seconds);
    std::vector<uint8_t> bits((size_t) (n * channels));       // 1 байт = 1 бит, упакуем позже
    for (int ch = 0; ch < channels; ++ch) {
        double i1 = 0.0, i2 = 0.0, prev = 0.0;
        // Второй канал сдвинут по фазе: если каналы перепутаются, это будет видно.
        const double phase = (ch == 0) ? 0.0 : M_PI / 2.0;
        for (int64_t i = 0; i < n; ++i) {
            const double x = kAmp * std::sin(2.0 * M_PI * toneHz * (double) i / (double) kDsdRate + phase);
            i1 += x - prev;
            i2 += i1 - prev;
            const double y = (i2 >= 0.0) ? 1.0 : -1.0;
            prev = y;
            bits[(size_t) (i * channels + ch)] = (y > 0.0) ? 1 : 0;
        }
    }
    return bits;
}

/** Упаковать биты канала в байты, старшим битом вперёд. */
std::vector<uint8_t> packChannel(const std::vector<uint8_t>& bits, int channels, int ch,
                                 bool lsbFirst) {
    const size_t n = bits.size() / (size_t) channels;
    std::vector<uint8_t> out(n / 8);
    for (size_t byteIdx = 0; byteIdx < out.size(); ++byteIdx) {
        uint8_t v = 0;
        for (int k = 0; k < 8; ++k) {
            const uint8_t bit = bits[(byteIdx * 8 + k) * channels + ch];
            if (lsbFirst) v |= (uint8_t) (bit << k);
            else          v |= (uint8_t) (bit << (7 - k));
        }
        out[byteIdx] = v;
    }
    return out;
}

void put32le(std::vector<uint8_t>& v, uint32_t x) {
    for (int i = 0; i < 4; ++i) v.push_back((uint8_t) ((x >> (8 * i)) & 0xFF));
}
void put64le(std::vector<uint8_t>& v, uint64_t x) {
    for (int i = 0; i < 8; ++i) v.push_back((uint8_t) ((x >> (8 * i)) & 0xFF));
}
void put64be(std::vector<uint8_t>& v, uint64_t x) {
    for (int i = 7; i >= 0; --i) v.push_back((uint8_t) ((x >> (8 * i)) & 0xFF));
}
void put32be(std::vector<uint8_t>& v, uint32_t x) {
    for (int i = 3; i >= 0; --i) v.push_back((uint8_t) ((x >> (8 * i)) & 0xFF));
}
void putTag(std::vector<uint8_t>& v, const char* s) {
    for (int i = 0; i < 4; ++i) v.push_back((uint8_t) s[i]);
}

/** DSF: блоки по каналу, порядок бит задаётся полем. */
std::vector<uint8_t> buildDsf(const std::vector<uint8_t>& bits, int channels, bool lsbFirst) {
    const int block = 4096;
    std::vector<std::vector<uint8_t>> per;
    for (int ch = 0; ch < channels; ++ch) per.push_back(packChannel(bits, channels, ch, lsbFirst));
    const size_t bytesPerCh = per[0].size();
    const size_t blocks = bytesPerCh / (size_t) block;
    const size_t dataBytes = blocks * (size_t) block * (size_t) channels;

    std::vector<uint8_t> f;
    putTag(f, "DSD "); put64le(f, 28); put64le(f, 0); put64le(f, 0);   // размер файла допишем ниже
    putTag(f, "fmt "); put64le(f, 52);
    put32le(f, 1);                       // версия
    put32le(f, 0);                       // формат: сырой DSD
    put32le(f, channels == 2 ? 2 : 1);   // тип каналов
    put32le(f, (uint32_t) channels);
    put32le(f, (uint32_t) kDsdRate);
    put32le(f, lsbFirst ? 1 : 8);
    put64le(f, (uint64_t) (blocks * (size_t) block * 8));   // отсчётов на канал
    put32le(f, (uint32_t) block);
    put32le(f, 0);                       // резерв
    putTag(f, "data"); put64le(f, dataBytes + 12);
    for (size_t b = 0; b < blocks; ++b)
        for (int ch = 0; ch < channels; ++ch)
            f.insert(f.end(), per[ch].begin() + (long) (b * block),
                     per[ch].begin() + (long) ((b + 1) * block));
    // Размер файла в заголовке.
    const uint64_t total = f.size();
    for (int i = 0; i < 8; ++i) f[12 + i] = (uint8_t) ((total >> (8 * i)) & 0xFF);
    return f;
}

/** DFF: байты каналов чередуются, всегда старшим битом вперёд. */
std::vector<uint8_t> buildDff(const std::vector<uint8_t>& bits, int channels) {
    std::vector<std::vector<uint8_t>> per;
    for (int ch = 0; ch < channels; ++ch) per.push_back(packChannel(bits, channels, ch, false));
    const size_t bytesPerCh = per[0].size();

    std::vector<uint8_t> snd;
    putTag(snd, "FS  "); put64be(snd, 4); put32be(snd, (uint32_t) kDsdRate);
    putTag(snd, "CHNL"); put64be(snd, 2);
    snd.push_back(0); snd.push_back((uint8_t) channels);

    std::vector<uint8_t> f;
    putTag(f, "FRM8"); put64be(f, 0); putTag(f, "DSD ");
    putTag(f, "PROP"); put64be(f, 4 + snd.size()); putTag(f, "SND ");
    f.insert(f.end(), snd.begin(), snd.end());
    putTag(f, "DSD "); put64be(f, (uint64_t) (bytesPerCh * (size_t) channels));
    for (size_t i = 0; i < bytesPerCh; ++i)
        for (int ch = 0; ch < channels; ++ch) f.push_back(per[ch][i]);
    const uint64_t formSize = f.size() - 12;
    for (int i = 0; i < 8; ++i) f[4 + i] = (uint8_t) ((formSize >> (8 * (7 - i))) & 0xFF);
    return f;
}

struct MemSrc { const std::vector<uint8_t>* buf; };

bool memReadAt(void* user, int64_t off, void* out, size_t bytes) {
    auto* s = static_cast<MemSrc*>(user);
    if (off < 0 || (size_t) off + bytes > s->buf->size()) return false;
    std::memcpy(out, s->buf->data() + off, bytes);
    return true;
}

/** Мощность на частоте f (Гёрцель) — чтобы не тащить БПФ ради одного тона. */
double goertzel(const std::vector<float>& x, int channels, int ch, double f, double sr) {
    const size_t n = x.size() / (size_t) channels;
    const double w = 2.0 * M_PI * f / sr;
    const double c = 2.0 * std::cos(w);
    double s1 = 0, s2 = 0;
    for (size_t i = 0; i < n; ++i) {
        const double s0 = (double) x[i * channels + ch] + c * s1 - s2;
        s2 = s1; s1 = s0;
    }
    return std::sqrt(s1 * s1 + s2 * s2 - c * s1 * s2) * 2.0 / (double) n;
}

int failures = 0;

void check(bool ok, const std::string& what, const std::string& detail = "") {
    std::printf("  %s %s%s%s\n", ok ? "ok  " : "ПЛОХО", what.c_str(),
                detail.empty() ? "" : " — ", detail.c_str());
    if (!ok) ++failures;
}

void runOne(const char* label, const std::vector<uint8_t>& file, int channels) {
    std::printf("\n== %s (%zu байт) ==\n", label, file.size());
    MemSrc src{&file};
    dsd::Ctx ctx;
    char why[96] = {0};
    if (!dsd::parse(ctx, memReadAt, &src, (int64_t) file.size(), why, sizeof why)) {
        std::printf("  ПЛОХО разбор не удался: %s\n", why);
        ++failures;
        return;
    }
    check(ctx.channels == channels, "каналов", std::to_string(ctx.channels));
    check(ctx.dsdRate == kDsdRate, "частота DSD", std::to_string((long long) ctx.dsdRate));
    check(ctx.decim == 32, "децимация", std::to_string(ctx.decim));

    const int64_t want = 40000;
    std::vector<float> out((size_t) (want * channels));
    const int64_t got = dsd::decode(ctx, memReadAt, &src, out.data(), want);
    check(got > want / 2, "кадров декодировано", std::to_string((long long) got));
    out.resize((size_t) (got * channels));

    // Начало переходное (фильтр набирает окно) — меряем со второй половины.
    std::vector<float> tail(out.begin() + (long) (out.size() / 2), out.end());

    const double at1k = goertzel(tail, channels, 0, kTone, dsd::kOutRate);
    const double at5k = goertzel(tail, channels, 0, 5000.0, dsd::kOutRate);
    std::printf("     тон 1 кГц: %.4f | посторонние 5 кГц: %.5f\n", at1k, at5k);
    check(at1k > 0.25 && at1k < 0.75, "амплитуда тона близка к 0.5");
    check(at5k < at1k / 8.0, "шум на чужой частоте подавлен");

    double dc = 0.0, peak = 0.0;
    for (float v : tail) { dc += v; peak = std::max(peak, (double) std::fabs(v)); }
    dc /= (double) tail.size();
    std::printf("     постоянная составляющая: %.5f | пик: %.4f\n", dc, peak);
    check(std::fabs(dc) < 0.02, "нет постоянной составляющей");
    check(peak < 1.01, "нет перегрузки");

    if (channels == 2) {
        const double r = goertzel(tail, channels, 1, kTone, dsd::kOutRate);
        std::printf("     правый канал на 1 кГц: %.4f\n", r);
        check(r > 0.25 && r < 0.75, "второй канал жив и не перепутан");
    }
}

}  // namespace

int main() {
    std::printf("Синтезирую DSD64, тон %.0f Гц, амплитуда %.1f…\n", kTone, kAmp);
    const auto bits = makeDsdBits(2, 0.35, kTone);

    runOne("DSF, старший бит первым", buildDsf(bits, 2, false), 2);
    runOne("DSF, младший бит первым", buildDsf(bits, 2, true), 2);
    runOne("DFF (DSDIFF)", buildDff(bits, 2), 2);

    // Отдельно: сжатый DST должен ОТКАЗАТЬ вслух, а не отдать тишину.
    std::vector<uint8_t> dst;
    putTag(dst, "FRM8"); put64be(dst, 32); putTag(dst, "DSD ");
    putTag(dst, "DST "); put64be(dst, 8);
    for (int i = 0; i < 8; ++i) dst.push_back(0);
    {
        std::printf("\n== DST (сжатый) ==\n");
        MemSrc src{&dst};
        dsd::Ctx ctx;
        char why[96] = {0};
        const bool ok = dsd::parse(ctx, memReadAt, &src, (int64_t) dst.size(), why, sizeof why);
        check(!ok, "отказ есть");
        check(std::string(why).find("DST") != std::string::npos, "причина названа", why);
    }

    std::printf("\n%s (неудачных проверок: %d)\n", failures ? "ЕСТЬ ПРОБЛЕМЫ" : "ВСЁ СОШЛОСЬ",
                failures);
    return failures ? 1 : 0;
}
