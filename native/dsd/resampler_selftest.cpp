// Проверка полифазного ресемплера без телефона. Собирается обычным g++.
//
//   g++ -O2 -std=c++17 -D_USE_MATH_DEFINES -o resampler_selftest resampler_selftest.cpp
//
// Проверяем не «не падает», а четыре вещи, каждая из которых уже была живой
// ошибкой либо в нашем коде, либо в чужих движках (каталог a7):
//
//   1. тон переживает пересчёт частоты и не теряет громкость;
//   2. то, что выше новой частоты Найквиста, ГЛУШИТСЯ, а не заворачивается
//      обратно в слышимую полосу (это и есть алиасинг);
//   3. РЕЗУЛЬТАТ НЕ ЗАВИСИТ ОТ РАЗМЕРА БЛОКА — иначе на каждой границе блока
//      щелчок; ровно этим болел линейный ресемпл, который здесь стоял;
//   4. нет постоянной составляющей и перегрузки.

#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "resampler.h"

namespace {

int failures = 0;

void check(bool ok, const std::string& what, const std::string& detail = "") {
    std::printf("  %s %s%s%s\n", ok ? "ok  " : "ПЛОХО", what.c_str(),
                detail.empty() ? "" : " — ", detail.c_str());
    if (!ok) ++failures;
}

std::vector<float> tone(double hz, double rate, int64_t frames, int ch, double amp = 0.5) {
    std::vector<float> v((size_t) frames * ch);
    for (int64_t i = 0; i < frames; ++i) {
        const double s = amp * std::sin(2.0 * M_PI * hz * (double) i / rate);
        for (int c = 0; c < ch; ++c) v[(size_t) i * ch + c] = (float) s;
    }
    return v;
}

double goertzel(const std::vector<float>& x, int ch, int c, double f, double sr) {
    const size_t n = x.size() / (size_t) ch;
    if (n < 16) return 0.0;
    const double w = 2.0 * M_PI * f / sr;
    const double k = 2.0 * std::cos(w);
    double s1 = 0, s2 = 0;
    for (size_t i = 0; i < n; ++i) {
        const double s0 = (double) x[i * ch + c] + k * s1 - s2;
        s2 = s1; s1 = s0;
    }
    return std::sqrt(s1 * s1 + s2 * s2 - k * s1 * s2) * 2.0 / (double) n;
}

/** Прогнать весь вход блоками по [block] кадров. */
std::vector<float> runBlocks(const std::vector<float>& in, int ch, double inRate,
                             double outRate, int64_t block) {
    rsmp::Resampler r;
    r.reset(ch, inRate, outRate);
    const int64_t frames = (int64_t) in.size() / ch;
    std::vector<float> out;
    std::vector<float> chunk((size_t) r.maxOut(block) * ch);
    for (int64_t off = 0; off < frames; off += block) {
        const int64_t n = (off + block <= frames) ? block : frames - off;
        const int64_t got = r.process(in.data() + (size_t) off * ch, n,
                                      chunk.data(), (int64_t) chunk.size() / ch);
        out.insert(out.end(), chunk.begin(), chunk.begin() + (long) (got * ch));
    }
    return out;
}

}  // namespace

int main() {
    const int ch = 2;

    // ── 1. 96 → 48 кГц, тон 1 кГц ───────────────────────────────────────────
    {
        std::printf("== 96000 -> 48000, тон 1 кГц ==\n");
        const auto in = tone(1000.0, 96000.0, 96000, ch);
        const auto out = runBlocks(in, ch, 96000.0, 48000.0, 4096);
        const int64_t frames = (int64_t) out.size() / ch;
        std::printf("     кадров: %lld (ожидали ~48000)\n", (long long) frames);
        check(frames > 47000 && frames < 48100, "число кадров соответствует отношению",
              std::to_string((long long) frames));
        // Меряем середину: у краёв фильтр набирает и отдаёт хвост.
        std::vector<float> mid(out.begin() + (long) (out.size() / 4),
                               out.end() - (long) (out.size() / 4));
        const double a = goertzel(mid, ch, 0, 1000.0, 48000.0);
        std::printf("     амплитуда на 1 кГц: %.4f\n", a);
        check(a > 0.47 && a < 0.53, "громкость сохранена");
        double dc = 0, peak = 0;
        for (float v : mid) { dc += v; peak = std::max(peak, (double) std::fabs(v)); }
        dc /= (double) mid.size();
        check(std::fabs(dc) < 0.002, "нет постоянной составляющей",
              std::to_string(dc));
        check(peak < 0.6, "нет перегрузки", std::to_string(peak));
    }

    // ── 2. Алиасинг: тон ВЫШЕ новой границы должен исчезнуть ────────────────
    {
        std::printf("\n== 96000 -> 48000, тон 30 кГц (выше новой границы) ==\n");
        const auto in = tone(30000.0, 96000.0, 96000, ch);
        const auto out = runBlocks(in, ch, 96000.0, 48000.0, 4096);
        std::vector<float> mid(out.begin() + (long) (out.size() / 4),
                               out.end() - (long) (out.size() / 4));
        // 30 кГц при 48 кГц завернулись бы на 18 кГц — вот там и смотрим.
        const double fold = goertzel(mid, ch, 0, 18000.0, 48000.0);
        std::printf("     зеркало на 18 кГц: %.6f (у линейного было бы слышно)\n", fold);
        check(fold < 0.01, "зеркало подавлено", std::to_string(fold));
    }

    // ── 3. ГЛАВНОЕ: результат не зависит от размера блока ───────────────────
    {
        std::printf("\n== одинаковость при разной нарезке (щелчки на границах) ==\n");
        const auto in = tone(997.0, 44100.0, 44100, ch);
        const auto big = runBlocks(in, ch, 44100.0, 48000.0, 44100);
        const auto small = runBlocks(in, ch, 44100.0, 48000.0, 137);   // нарочно некруглый
        const size_t n = std::min(big.size(), small.size());
        double worst = 0.0;
        for (size_t i = 0; i < n; ++i)
            worst = std::max(worst, (double) std::fabs(big[i] - small[i]));
        std::printf("     кадров: одним куском %zu, мелкими блоками %zu\n",
                    big.size() / ch, small.size() / ch);
        std::printf("     худшее расхождение: %.8f\n", worst);
        check(n > 0, "оба прогона что-то выдали");
        check(worst < 1e-5, "нарезка на блоки НЕ меняет звук", std::to_string(worst));

        // Заодно ловим разрывы внутри самого сигнала: соседние отсчёты синуса
        // не могут отличаться больше, чем на шаг синуса с запасом.
        double jump = 0.0;
        for (size_t i = ch; i + ch < small.size(); i += ch)
            jump = std::max(jump, (double) std::fabs(small[i] - small[i - ch]));
        const double expected = 0.5 * 2.0 * M_PI * 997.0 / 48000.0;
        std::printf("     наибольший скачок между соседними: %.5f (плавный ход ~%.5f)\n",
                    jump, expected);
        check(jump < expected * 2.0, "разрывов на границах блоков нет");
    }

    // ── 4. Повышение частоты ────────────────────────────────────────────────
    {
        std::printf("\n== 44100 -> 96000, тон 5 кГц ==\n");
        const auto in = tone(5000.0, 44100.0, 44100, ch);
        const auto out = runBlocks(in, ch, 44100.0, 96000.0, 1024);
        std::vector<float> mid(out.begin() + (long) (out.size() / 4),
                               out.end() - (long) (out.size() / 4));
        const double a = goertzel(mid, ch, 0, 5000.0, 96000.0);
        std::printf("     амплитуда на 5 кГц: %.4f | кадров %zu\n", a, out.size() / ch);
        check(a > 0.47 && a < 0.53, "громкость сохранена при повышении");
        check(out.size() / ch > 90000, "число кадров выросло по отношению");
    }

    // ── 5. Совпадающие частоты — сквозной проход ────────────────────────────
    {
        std::printf("\n== 48000 -> 48000 ==\n");
        rsmp::Resampler r;
        r.reset(ch, 48000.0, 48000.0);
        check(r.passthrough(), "распознан сквозной проход (ресемпл не нужен)");
    }

    std::printf("\n%s (неудачных проверок: %d)\n",
                failures ? "ЕСТЬ ПРОБЛЕМЫ" : "ВСЁ СОШЛОСЬ", failures);
    return failures ? 1 : 0;
}
