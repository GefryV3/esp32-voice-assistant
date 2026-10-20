#pragma once
// Детектор серии из двух хлопков по сырому звуку микрофона. Чистая логика без ESP-IDF: те же
// числа повторяет firmware/tools/clap_sim.py, на нём проверены синтетические сигналы.
//
// Как отличаем хлопок от остального (звук — 16 кГц, уже после фильтра выше 100 Гц):
//  1. Резкий фронт: пик 5-мс блока в `peak_ratio` раз выше фона (медленное среднее модуля) и
//     выше абсолютного минимума. Речь и музыка нарастают плавно, а фон за секунду «привыкает».
//  2. Высокие частоты: хлопок — широкополосный щелчок, у него много перепадов от сэмпла к
//     сэмплу (отношение суммы |x[n]-x[n-1]| к сумме |x[n]| у голоса ≈0.1…0.3, у хлопка >0.45).
//     Этим же отбрасываем импульсы питания Wi-Fi: они почти целиком ниже 150 Гц.
//  3. Быстрый спад: через 40-60 мс после фронта пик упал ниже трети. Речь и музыка держатся.
//  4. Два таких события с интервалом 0.2-1.0 с — серия. Быстрее 0.2 с — эхо комнаты, не считаем.
// Пока колонка играет (и 0.6 с после), детектор молчит и сбрасывается.
#include <cmath>
#include <cstdint>
#include <cstdlib>

namespace clap {

constexpr int RATE = 16000;
constexpr int BLOCK = 80;                  // 5 мс
constexpr int BLOCK_MS = 5;

constexpr int SETTLE_BLOCKS = 10;          // через 50 мс после фронта проверяем спад
constexpr float DECAY_MAX = 0.33f;         // пик к этому моменту / пик фронта
constexpr float HF_MIN = 0.45f;            // доля высоких частот у хлопка
constexpr int REFRACTORY_BLOCKS = 16;      // 80 мс после фронта новые фронты не ищем
constexpr int GAP_MIN_MS = 200;            // быстрее — эхо комнаты, не второй хлопок
constexpr int GAP_MAX_MS = 1000;           // медленнее — это два отдельных звука
constexpr int LOCKOUT_MS = 1500;           // после срабатывания отдыхаем
constexpr int MUTE_TAIL_MS = 600;          // после звука из динамика хвост эха
constexpr float BG_ALPHA = 1.0f / 200.0f;  // фон усредняется ~1 с (200 блоков по 5 мс)
constexpr float BG_MIN = 8.0f;             // тишина «ниже пола»: не делим на ноль

// Чувствительность 1 (скупая) … 10 (острая), 5 по умолчанию.
inline float peak_ratio(int sens) { return 20.0f - 1.5f * sens; }
inline float peak_min(int sens) { return 6000.0f - 450.0f * sens; }

// Что решил детектор (для журнала: по нему видно, почему хлопок не засчитался).
struct Diag {
  enum Kind : uint8_t { None, Onset, NearMiss, Settled, TooSlow, First, Echo, Timeout, Fired } kind = None;
  int peak = 0;            // пик блока (Onset, NearMiss) или пик спада (Settled, TooSlow)
  int onset_peak = 0;      // пик фронта (Settled, TooSlow)
  float ratio = 0;         // пик / фон
  float hf = 0;            // доля высоких частот
  int gap_ms = 0;          // интервал между хлопками (Echo, Fired)
};

class Detector {
 public:
  // Последнее событие для журнала; true один раз на событие.
  bool take_diag(Diag &d) {
    if (diag_.kind == Diag::None) return false;
    d = diag_;
    diag_.kind = Diag::None;
    return true;
  }

  void set_sensitivity(int s) { sens_ = s < 1 ? 1 : (s > 10 ? 10 : s); }
  int sensitivity() const { return sens_; }

  // Вернёт true, когда набралась серия из двух хлопков. `muted` — колонка сейчас играет
  // или идёт разговор, который нельзя путать с хлопком. Кадр любой длины: режем по 5 мс.
  bool process(const int16_t *pcm, int n, bool muted) {
    bool fired = false;
    for (int i = 0; i < n; ++i) {
      buf_[fill_++] = pcm[i];
      if (fill_ == BLOCK) {
        fill_ = 0;
        if (block(buf_, muted)) fired = true;
      }
    }
    return fired;
  }

  void reset() {
    state_ = Idle;
    since_first_ms_ = -1;
    mute_left_ = 0;
  }

 private:
  enum State { Idle, Settling };

  bool block(const int16_t *x, bool muted) {
    int peak = 0;
    float sum_abs = 0, sum_diff = 0;
    for (int i = 0; i < BLOCK; ++i) {
      const int a = std::abs(static_cast<int>(x[i]));
      if (a > peak) peak = a;
      sum_abs += static_cast<float>(a);
      sum_diff += static_cast<float>(std::abs(static_cast<int>(x[i]) - static_cast<int>(prev_)));
      prev_ = x[i];
    }
    const float mean = sum_abs / BLOCK;
    const float hf = sum_abs > 1.0f ? sum_diff / sum_abs : 0.0f;

    if (muted) {
      mute_left_ = MUTE_TAIL_MS / BLOCK_MS;
      reset_events();
      bg_ = bg_ + (mean - bg_) * BG_ALPHA;   // фон всё равно ведём
      return false;
    }
    if (mute_left_ > 0) {
      --mute_left_;
      bg_ = bg_ + (mean - bg_) * BG_ALPHA;
      return false;
    }
    if (lockout_ > 0) --lockout_;
    if (since_first_ms_ >= 0) {
      since_first_ms_ += BLOCK_MS;
      if (since_first_ms_ > GAP_MAX_MS) {
        since_first_ms_ = -1;   // второй не дождались
        note(Diag::Timeout, 0, 0, 0, 0, GAP_MAX_MS);
      }
    }

    bool fired = false;
    const float bg = bg_ > BG_MIN ? bg_ : BG_MIN;
    if (state_ == Idle) {
      if (refractory_ > 0) --refractory_;
      if (refractory_ == 0 && lockout_ == 0 && peak >= peak_min(sens_) &&
          static_cast<float>(peak) >= peak_ratio(sens_) * bg && hf >= HF_MIN) {
        state_ = Settling;
        onset_peak_ = peak;
        settle_ = SETTLE_BLOCKS;
        settle_peak_ = 0;
        refractory_ = REFRACTORY_BLOCKS;
        note(Diag::Onset, peak, 0, static_cast<float>(peak) / bg, hf, 0);
      } else if (refractory_ == 0 && lockout_ == 0 && peak >= peak_min(sens_) / 2 && diag_cool_ == 0) {
        diag_cool_ = 40;   // громкий резкий блок, но не хлопок: раз в 200 мс причина в журнал
        note(Diag::NearMiss, peak, 0, static_cast<float>(peak) / bg, hf, 0);
      }
      if (diag_cool_ > 0) --diag_cool_;
    } else {   // Settling
      if (refractory_ > 0) --refractory_;
      if (--settle_ < 2 && peak > settle_peak_) settle_peak_ = peak;   // два последних блока перед проверкой
      if (settle_ == 0) {
        state_ = Idle;
        if (static_cast<float>(settle_peak_) <= DECAY_MAX * static_cast<float>(onset_peak_)) {
          note(Diag::Settled, settle_peak_, onset_peak_, 0, 0, 0);
          fired = on_clap();
        } else {
          note(Diag::TooSlow, settle_peak_, onset_peak_, 0, 0, 0);
        }
      }
    }
    // фон ведём по тихим блокам: хлопок и речь его не поднимают
    if (state_ == Idle && static_cast<float>(peak) < 3.0f * bg) bg_ = bg_ + (mean - bg_) * BG_ALPHA;
    return fired;
  }

  bool on_clap() {
    if (since_first_ms_ >= 0) {
      if (since_first_ms_ < GAP_MIN_MS) {      // эхо того же хлопка
        note(Diag::Echo, 0, 0, 0, 0, since_first_ms_);
        return false;
      }
      note(Diag::Fired, 0, 0, 0, 0, since_first_ms_);
      since_first_ms_ = -1;
      lockout_ = LOCKOUT_MS / BLOCK_MS;
      return true;
    }
    since_first_ms_ = 0;
    note(Diag::First, 0, 0, 0, 0, 0);
    return false;
  }

  void note(Diag::Kind k, int peak, int onset, float ratio, float hf, int gap) {
    // важное (Settled…Fired) не затираем «почти»: Onset/NearMiss заменяют только пустое или такое же
    if (diag_.kind != Diag::None && (k == Diag::NearMiss || k == Diag::Onset)) return;
    diag_ = {k, peak, onset, ratio, hf, gap};
  }

  void reset_events() {
    state_ = Idle;
    since_first_ms_ = -1;
    refractory_ = 0;
  }

  int sens_ = 5;
  State state_ = Idle;
  int16_t buf_[BLOCK] = {};
  int fill_ = 0;
  int16_t prev_ = 0;
  float bg_ = 100.0f;
  int onset_peak_ = 0;
  int settle_ = 0;
  int settle_peak_ = 0;
  int refractory_ = 0;
  int lockout_ = 0;
  int mute_left_ = 1000 / BLOCK_MS;   // первая секунда после включения: INMP441 выдаёт мусор
  int since_first_ms_ = -1;
  Diag diag_;
  int diag_cool_ = 0;
};

}  // namespace clap
