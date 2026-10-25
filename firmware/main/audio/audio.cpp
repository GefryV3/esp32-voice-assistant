#include "audio/audio.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>

#include "audio/clap.h"
#include "board.h"
#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"

namespace audio {
namespace {

const char *TAG = "audio";

constexpr int MIC_RATE = 16000;
constexpr int MIC_FRAME = 320;                // 20 мс
// В сеть — пачками по 100 мс: каждый пакет Wi-Fi слышен в микрофоне щелчком (см. Highpass),
// и пачки дают их в 5 раз меньше, чем кадр на каждые 20 мс.
constexpr int MIC_FRAMES_PER_SEND = 5;
constexpr float MIC_HIGHPASS_HZ = 100.0f;
constexpr size_t MIC_BUF = 32 * 1024;         // ~1 с — если сеть притормозила
constexpr size_t SPK_BUF = 128 * 1024;        // 4 с при 16 кГц
constexpr int SPK_DMA_DESC = 6;
constexpr int SPK_DMA_FRAMES = 240;           // 15 мс на дескриптор при 16 кГц
constexpr int SPK_CHUNK = 512;                // сэмплов за проход проигрывателя
constexpr int PREBUF_MS = 400;
// Маленький динамик ниже ~160 Гц не звучит, а только бьётся и хрипит, и усилитель тянет из
// USB пики тока. Голос Gemini — больше половины энергии ниже 400 Гц, басы срезаем.
constexpr float SPK_HIGHPASS_HZ = 300.0f;
constexpr float LIMIT_KNEE = 0.5f;            // доля полной шкалы, с которой работает ограничитель
constexpr float MAX_GAIN = 0.36f;             // громкость 100 %

constexpr int BARGE_FRAMES = 10;              // 10 loud frames (gaps forgiven) — перебили
constexpr int BARGE_PREROLL_FRAMES = 12;      // 240 мс микрофона отдаём серверу вслед
constexpr int BARGE_GRACE_FRAMES = 12;        // первые 240 мс ответа не слушаем: ещё нет эха
constexpr float BARGE_MIN_RMS = 1800.0f;      // тише не считается голосом (~−25 дБFS, echo here reaches ~1700)
constexpr float BARGE_QUIET_RMS = 3000.0f;    // пауза в ответе: голос должен быть ярко слышен
constexpr int BARGE_TAIL_FRAMES = 15;         // после звука 300 мс не слушаем: хвост эха и комната
constexpr float BARGE_K_FLOOR = 2.0f;         // эхо в микрофоне не слабее 2× играющего: нижняя граница k
constexpr int BARGE_LEARN_FRAMES = 50;        // первая секунда ответа: k подстраивается быстро
constexpr float BARGE_REF_MIN = 40.0f;        // эталон тише — пауза в речи, сравнивать не с чем

SendFn s_send = nullptr;
DoneFn s_done = nullptr;
BargeFn s_barge = nullptr;
ClapFn s_clap = nullptr;

i2s_chan_handle_t s_rx = nullptr;
i2s_chan_handle_t s_tx = nullptr;
StreamBufferHandle_t s_mic_sb = nullptr;      // микрофон → задача отправки
StreamBufferHandle_t s_spk_sb = nullptr;      // WebSocket → проигрыватель
StaticStreamBuffer_t s_mic_sb_ctl;
StaticStreamBuffer_t s_spk_sb_ctl;
TaskHandle_t s_player = nullptr;
TaskHandle_t s_mic = nullptr;

std::atomic<bool> s_listening{false};
std::atomic<int> s_gain_q8{1019};             // усиление ×256: 12 дБ по умолчанию
std::atomic<int> s_vol_q15{2949};             // громкость 50 %, пока сервер не прислал свою
std::atomic<bool> s_accept{false};            // принимать кадры звука в буфер
std::atomic<bool> s_speaking{false};          // динамик сейчас играет
std::atomic<bool> s_barge_on{false};
std::atomic<bool> s_barge_armed{false};       // звук реально звучит (после накопления), а не ждёт начала
std::atomic<int> s_barge_ratio_x100{300};
std::atomic<float> s_ref_rms{0};              // громкость того, что сейчас играет (с задержкой затухания)
std::atomic<bool> s_clap_on{true};
std::atomic<int> s_clap_sens{5};
std::atomic<bool> s_alarm_stop{false};
std::atomic<bool> s_alarm_on{false};          // встроенный сигнал звучит
TaskHandle_t s_alarm_task = nullptr;
std::atomic<bool> s_ended{false};
std::atomic<bool> s_stop{false};
std::atomic<int> s_rate_req{16000};
std::atomic<uint32_t> s_overflow{0};
int s_rate = 16000;

// ── слух для экрана: уровень голоса, резкие звуки, ритм ──
std::atomic<float> s_level{0};
std::atomic<uint32_t> s_bangs{0}, s_beats{0};

struct Ear {
  float floor = 60;      // фон комнаты (rms, единицы int16), медленно подстраивается
  float fast = 0;        // быстрая огибающая
  float slow = 0;        // медленная (~300 мс)
  float lvl = 0;
  int bang_cool = 0, beat_cool = 0;

  void step(const int16_t *pcm, int n, bool speaking) {
    double acc = 0;
    for (int i = 0; i < n; ++i) acc += static_cast<double>(pcm[i]) * pcm[i];
    const float m = n ? static_cast<float>(std::sqrt(acc / n)) : 0.0f;
    if (m < floor * 1.6f) floor += (m - floor) * 0.01f;
    else floor += (m - floor) * 0.0006f;
    floor = std::clamp(floor, 20.0f, 3000.0f);
    fast += (m - fast) * 0.6f;
    const float before = slow;
    slow += (m - slow) * 0.07f;
    // уровень для формы глаз: над фоном, с запасом громкости голоса ~ 4000
    const float v = std::clamp((m - floor * 1.4f) / 3500.0f, 0.0f, 1.0f);
    lvl += (v - lvl) * (v > lvl ? 0.6f : 0.12f);
    s_level.store(lvl, std::memory_order_relaxed);
    if (bang_cool > 0) --bang_cool;
    if (beat_cool > 0) --beat_cool;
    if (speaking) return;  // эхо своего ответа за хлопок не считаем
    // хлопок/стук: резко выше фона и намного выше недавнего среднего
    if (bang_cool == 0 && m > std::max(floor * 6.0f, 2200.0f) && m > before * 4.0f) {
      s_bangs.fetch_add(1);
      bang_cool = 15;  // 300 мс
    }
    // удар ритма: заметный всплеск над медленной огибающей, не реже чем 250 мс
    if (beat_cool == 0 && m > floor * 2.5f && m > 500 && m > before * 1.7f) {
      s_beats.fetch_add(1);
      beat_cool = 12;
    }
  }
};

StreamBufferHandle_t make_buffer(size_t size, size_t trigger, StaticStreamBuffer_t *ctl) {
  auto *mem = static_cast<uint8_t *>(heap_caps_malloc(size + 1, MALLOC_CAP_SPIRAM));
  if (!mem) mem = static_cast<uint8_t *>(heap_caps_malloc(size + 1, MALLOC_CAP_8BIT));
  return mem ? xStreamBufferCreateStatic(size, trigger, mem, ctl) : nullptr;
}

// ── микрофон ──

void init_mic() {
  i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  cc.dma_desc_num = 6;
  cc.dma_frame_num = MIC_FRAME;
  ESP_ERROR_CHECK(i2s_new_channel(&cc, nullptr, &s_rx));
  // INMP441 отдаёт 24 бита в 32-битном слоте, L/R на земле — левый канал
  i2s_std_config_t sc = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(MIC_RATE),
      .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
      .gpio_cfg = {.mclk = I2S_GPIO_UNUSED,
                   .bclk = board::MIC_SCK,
                   .ws = board::MIC_WS,
                   .dout = I2S_GPIO_UNUSED,
                   .din = board::MIC_SD,
                   .invert_flags = {}},
  };
  sc.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &sc));
  // работает всегда: INMP441 после включения тактов ~90 мс выдаёт мусор, и слово
  // пробуждения (этап 5) всё равно слушает постоянно
  ESP_ERROR_CHECK(i2s_channel_enable(s_rx));
}

// Фильтр высоких частот, Баттерворт 2-го порядка. Срезает постоянную составляющую и, главное,
// помеху от передатчика Wi-Fi: каждый пакет — бросок тока, питание микрофона проседает, и в
// звук попадает импульс, почти весь ниже 150 Гц. На 100 Гц шум падает на ~13 дБ, голос — на 2.
struct Highpass {
  float b0, b1, b2, a1, a2;
  float x1 = 0, x2 = 0, y1 = 0, y2 = 0;

  Highpass(float fc, float fs) {
    const float w0 = 2.0f * static_cast<float>(M_PI) * fc / fs;
    const float c = std::cos(w0), alpha = std::sin(w0) / std::sqrt(2.0f), a0 = 1.0f + alpha;
    b0 = b2 = (1.0f + c) / 2.0f / a0;
    b1 = -(1.0f + c) / a0;
    a1 = -2.0f * c / a0;
    a2 = (1.0f - alpha) / a0;
  }

  float operator()(float x) {
    const float y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
    x2 = x1;
    x1 = x;
    y2 = y1;
    y1 = y;
    return y;
  }
};

// Читает микрофон всегда (фильтр не остывает), отдаёт — только пока слушаем. Отправка в
// сеть — в отдельной задаче, чтобы задержка сети не рвала DMA.
// Перебивание. Эхо собственного ответа в микрофоне пропорционально тому, что играет, поэтому
// сравниваем микрофон не с порогом, а с громкостью воспроизведения: отношение «микрофон /
// играющее» в норме почти постоянно (его и запоминаем, k), а голос поверх эха его резко
// поднимает. Эталон — пик играющего звука с затуханием ~200 мс (микрофон слышит с задержкой).
struct Barge {
  static constexpr size_t RING = BARGE_PREROLL_FRAMES;
  int16_t ring[RING][MIC_FRAME];
  int head = 0;
  int frames = 0;       // сколько кадров идёт этот ответ
  int hits = 0;         // подряд громче эха
  int quiet = 0;        // подряд кадров, когда играющее молчит
  float k = 0;          // обычное отношение микрофон/эталон
  uint32_t log_tick = 0;

  void idle() { frames = 0; hits = 0; quiet = BARGE_TAIL_FRAMES; }

  bool step(const int16_t *pcm, int n) {
    memcpy(ring[head], pcm, n * sizeof(int16_t));
    if (n < MIC_FRAME) memset(ring[head] + n, 0, (MIC_FRAME - n) * sizeof(int16_t));
    head = (head + 1) % RING;
    ++frames;
    double acc = 0;
    for (int i = 0; i < n; ++i) acc += static_cast<double>(pcm[i]) * pcm[i];
    const float m = n ? static_cast<float>(std::sqrt(acc / n)) : 0.0f;
    const float r = s_ref_rms.load();
    if (frames <= BARGE_GRACE_FRAMES) return false;
    const float ratio = s_barge_ratio_x100 / 100.0f;
    bool loud;
    if (r < BARGE_REF_MIN) {
      // играющее молчит (пауза между фразами): слушаем только после 300 мс тишины в эталоне
      loud = ++quiet > BARGE_TAIL_FRAMES && m > BARGE_QUIET_RMS;
    } else {
      quiet = 0;
      const float q = m / r;
      loud = m > BARGE_MIN_RMS && k > 0 && q > k * ratio;
      if (!loud) {      // эхо: уточняем «обычное» отношение. В начале ответа эхо ещё нарастает: учимся быстро и
                        // не даём k упасть ниже пола, иначе обычное эхо принимается за голос и фраза рвётся
        const float a = frames < BARGE_LEARN_FRAMES ? 0.15f : 0.02f;
        k = k > 0 ? k + (q - k) * a : q;
        k = std::max(k, BARGE_K_FLOOR);
      }
    }
    if ((++log_tick % 25) == 0) {
      ESP_LOGI(TAG, "перебивание: микрофон %.0f эталон %.0f k %.2f %s", m, r, k, loud ? "ГРОМКО" : "");
    }
    hits = loud ? hits + 1 : std::max(0, hits - 1);   // speech has gaps between syllables: forgive them slowly
    return hits >= BARGE_FRAMES;
  }
};

void audio_barge_in(Barge &b);

// Что решил детектор хлопков: по этим строкам видно, где хлопок не прошёл (пик, фон, спад, интервал).
// Звук в журнал не попадает, только числа. Не чаще 8 строк в секунду, чтобы не мешать микрофону.
void log_clap(const clap::Diag &d) {
  static int64_t window = 0;
  static int lines = 0;
  const int64_t now = esp_timer_get_time() / 1000000;
  if (now != window) window = now, lines = 0;
  if (lines++ >= 8) return;
  switch (d.kind) {
    case clap::Diag::Onset: ESP_LOGI(TAG, "хлопок: фронт, пик %d (%.0f× фона), hf %.2f", d.peak, d.ratio, d.hf); break;
    case clap::Diag::NearMiss: ESP_LOGI(TAG, "хлопок: не фронт, пик %d (%.0f× фона), hf %.2f", d.peak, d.ratio, d.hf); break;
    case clap::Diag::Settled: ESP_LOGI(TAG, "хлопок: спад принят (%d из %d)", d.peak, d.onset_peak); break;
    case clap::Diag::TooSlow: ESP_LOGI(TAG, "хлопок: звук не спал (%d из %d), не хлопок", d.peak, d.onset_peak); break;
    case clap::Diag::First: ESP_LOGI(TAG, "хлопок: первый, жду второй"); break;
    case clap::Diag::Echo: ESP_LOGI(TAG, "хлопок: слишком скоро (%d мс), это эхо", d.gap_ms); break;
    case clap::Diag::Timeout: ESP_LOGI(TAG, "хлопок: второго не было"); break;
    case clap::Diag::Fired: ESP_LOGI(TAG, "хлопки: серия через %d мс", d.gap_ms); break;
    case clap::Diag::Crowded: ESP_LOGI(TAG, "хлопок: шумно вокруг (речь, шёпот), пик в %.1f× тише порога изоляции, hf %.2f", d.ratio, d.hf); break;
    case clap::Diag::Uneven: ESP_LOGI(TAG, "хлопок: второй не такой по громкости (пик %d против %d, ×%.1f), считаю новым первым", d.peak, d.onset_peak, d.ratio); break;
    case clap::Diag::None: break;
  }
}

void mic_task(void *) {
  static int32_t raw[MIC_FRAME];
  static int16_t pcm[MIC_FRAME];
  static int16_t flat[MIC_FRAME];               // тот же звук без усиления — для детектора хлопков
  static Barge barge;
  static Ear ear;
  static clap::Detector claps;
  Highpass hpf(MIC_HIGHPASS_HZ, MIC_RATE);
  uint32_t dropped = 0;
  int clap_sens = 0;
  for (;;) {
    size_t got = 0;
    if (i2s_channel_read(s_rx, raw, sizeof raw, &got, portMAX_DELAY) != ESP_OK) continue;
    const int n = got / sizeof(int32_t);
    const float gain = s_gain_q8.load() / 256.0f / 256.0f;  // 24 бита → 16 и усиление
    for (int i = 0; i < n; ++i) {
      const float y = hpf(static_cast<float>(raw[i] >> 8));
      pcm[i] = static_cast<int16_t>(std::clamp(y * gain, -32768.0f, 32767.0f));
      flat[i] = static_cast<int16_t>(std::clamp(y / 256.0f, -32768.0f, 32767.0f));
    }
    // Хлопки: работают всегда, даже без сети и без слова пробуждения; пока играет динамик —
    // молчат (сами себя же и услышим)
    if (s_clap_on) {
      if (const int sens = s_clap_sens; sens != clap_sens) {
        clap_sens = sens;
        claps.set_sensitivity(sens);
      }
      if (claps.process(flat, n, s_speaking) && s_clap) s_clap(2);
      clap::Diag d;
      while (claps.take_diag(d)) log_clap(d);
    }
    ear.step(pcm, n, s_speaking);
    // Пока динамик играет, в сеть не шлём: каждый пакет Wi-Fi — бросок тока, и усилитель
    // трещит в такт отправкам. Слушаем только локально: не перебивает ли пользователь.
    if (s_speaking) {
      if (!s_barge_armed) barge.idle();    // накопление в буфере: слушать пока нечего
      else if (s_barge_on && s_listening && barge.step(pcm, n)) {
        audio_barge_in(barge);
      }
      continue;
    }
    barge.idle();
    if (!s_listening) continue;
    if (xStreamBufferSend(s_mic_sb, pcm, n * sizeof(int16_t), 0) != n * sizeof(int16_t)) {
      if (dropped++ % 50 == 0) ESP_LOGW(TAG, "микрофон: сеть не успевает, теряю кадры (%lu)", (unsigned long)dropped);
    }
  }
}

// Перебили: замолчать сразу, не дожидаясь сервера, отдать ему начало фразы и сообщить.
void audio_barge_in(Barge &b) {
  ESP_LOGI(TAG, "перебили — замолкаю");
  s_accept = false;
  s_stop = true;
  if (s_barge) s_barge();      // сообщение раньше кадров: сервер уже ждёт звук, когда они придут
  for (int i = 0; i < Barge::RING; ++i) {
    const int16_t *frame = b.ring[(b.head + i) % Barge::RING];   // старые кадры первыми
    xStreamBufferSend(s_mic_sb, frame, MIC_FRAME * sizeof(int16_t), 0);
  }
  b.idle();
}

// Ждёт пачку в 100 мс, но забирает всё накопленное (до 200 мс) одним куском: пока
// WebSocket занят приёмом звука, кадры копятся и потом уходят разом, а не теряются.
void mic_send_task(void *) {
  static uint8_t chunk[MIC_FRAME * sizeof(int16_t) * 10];
  for (;;) {
    const size_t n = xStreamBufferReceive(s_mic_sb, chunk, sizeof chunk, portMAX_DELAY);
    for (int attempt = 0; n && s_listening && s_send && attempt < 20; ++attempt) {
      if (s_send(chunk, n)) break;
      vTaskDelay(pdMS_TO_TICKS(10));
    }
  }
}

// ── динамик ──

void init_speaker() {
  i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
  cc.dma_desc_num = SPK_DMA_DESC;
  cc.dma_frame_num = SPK_DMA_FRAMES;
  cc.auto_clear = true;  // данные кончились — в динамик идёт тишина, а не повтор буфера
  ESP_ERROR_CHECK(i2s_new_channel(&cc, &s_tx, nullptr));
  // моно: на S3 один и тот же сэмпл уходит в оба слота, MAX98357A берёт (L+R)/2
  i2s_std_config_t sc = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000),
      .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
      .gpio_cfg = {.mclk = I2S_GPIO_UNUSED,
                   .bclk = board::SPK_BCLK,
                   .ws = board::SPK_LRC,
                   .dout = board::SPK_DIN,
                   .din = I2S_GPIO_UNUSED,
                   .invert_flags = {}},
  };
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &sc));
  // канал включаем только на время звука: без тактов усилитель молчит и не шипит
}

// Мягкий ограничитель: до LIMIT_KNEE звук как есть, выше — плавно прижимается к потолку,
// а не срезается ступенькой (ступенька и есть треск).
float limit(float y) {
  const float a = std::fabs(y);
  if (a <= LIMIT_KNEE) return y;
  const float r = 1.0f - LIMIT_KNEE;
  return std::copysign(LIMIT_KNEE + r * std::tanh((a - LIMIT_KNEE) / r), y);
}

void set_rate(int rate) {
  if (rate == s_rate) return;
  i2s_std_clk_config_t clk = I2S_STD_CLK_DEFAULT_CONFIG(static_cast<uint32_t>(rate));
  if (i2s_channel_reconfig_std_clock(s_tx, &clk) == ESP_OK) {
    s_rate = rate;
  } else {
    ESP_LOGW(TAG, "частота %d Гц не встала, играю на %d", rate, s_rate);
  }
}

void play_once() {
  s_speaking = true;
  set_rate(s_rate_req);
  // копим 400 мс: через ретранслятор задержка сети скачет до 300 мс
  const size_t prebuf = static_cast<size_t>(s_rate) * 2 * PREBUF_MS / 1000;
  const TickType_t t0 = xTaskGetTickCount();
  while (!s_stop && !s_ended && xStreamBufferBytesAvailable(s_spk_sb) < prebuf &&
         xTaskGetTickCount() - t0 < pdMS_TO_TICKS(1000)) {
    vTaskDelay(pdMS_TO_TICKS(10));
  }

  static uint8_t in[SPK_CHUNK * 2 + 1];
  static int16_t out[SPK_CHUNK];
  Highpass hpf(SPK_HIGHPASS_HZ, static_cast<float>(s_rate));
  ESP_ERROR_CHECK(i2s_channel_enable(s_tx));
  s_barge_armed = true;
  size_t carry = 0;
  uint32_t underruns = 0, samples_total = 0;
  for (;;) {
    if (s_stop) break;
    size_t n = carry + xStreamBufferReceive(s_spk_sb, in + carry, SPK_CHUNK * 2, pdMS_TO_TICKS(40));
    const size_t samples = n / 2;
    if (samples) {
      const float vol = s_vol_q15 / 32768.0f / 32768.0f;  // и в шкалу −1…1
      double acc = 0;
      for (size_t i = 0; i < samples; ++i) {
        int16_t x;
        memcpy(&x, in + 2 * i, 2);
        out[i] = static_cast<int16_t>(limit(hpf(static_cast<float>(x)) * vol) * 32767.0f);
        acc += static_cast<double>(out[i]) * out[i];
      }
      const float rms = static_cast<float>(std::sqrt(acc / samples));
      s_ref_rms = std::max(rms, s_ref_rms.load() * 0.85f);
      size_t written = 0;
      i2s_channel_write(s_tx, out, samples * 2, &written, portMAX_DELAY);
      samples_total += samples;
    }
    carry = n & 1;
    if (carry) in[0] = in[n - 1];
    if (n == 0) {
      if (s_ended) break;
      ++underruns;  // сеть не успела — в динамик идёт тишина
    }
  }
  if (!s_stop) {
    // доиграть то, что уже лежит в DMA: дописать тишину длиной во весь DMA-буфер
    static int16_t zeros[SPK_DMA_FRAMES] = {};
    size_t written = 0;
    for (int i = 0; i <= SPK_DMA_DESC; ++i) i2s_channel_write(s_tx, zeros, sizeof zeros, &written, portMAX_DELAY);
  }
  i2s_channel_disable(s_tx);
  s_barge_armed = false;
  s_ref_rms = 0;
  s_speaking = false;
  if (s_stop) xStreamBufferReset(s_spk_sb);  // писатель уже не пишет (s_accept снят)
  const uint32_t overflow = s_overflow.exchange(0);
  ESP_LOGI(TAG, "доиграл %.1f с%s, пропусков %lu, не влезло %lu байт", samples_total / (float)s_rate,
           s_stop ? " (остановлен)" : "", (unsigned long)underruns, (unsigned long)overflow);
}

// Встроенный сигнал: три гудка по 150 мс и пауза, цикл 2 с, тон чередуется 880/1175 Гц. Рисуем
// сэмплы прямо в буфер проигрывателя и ждём, когда в нём появится место: темп задаёт сам динамик.
void alarm_task(void *) {
  static int16_t chunk[320];
  for (;;) {
    int seconds = static_cast<int>(ulTaskNotifyTake(pdTRUE, portMAX_DELAY));
    for (int i = 0; s_speaking && i < 200; ++i) vTaskDelay(pdMS_TO_TICKS(10));
    if (s_speaking) continue;
    s_alarm_stop = false;
    s_alarm_on = true;
    play_start(MIC_RATE);
    const int64_t total = static_cast<int64_t>(seconds) * MIC_RATE;
    float phase = 0;
    for (int64_t n = 0; n < total && !s_alarm_stop && s_accept;) {
      for (int i = 0; i < 320; ++i, ++n) {
        const int t_ms = static_cast<int>((n * 1000 / MIC_RATE) % 2000);
        const int slot = t_ms / 250, in_slot = t_ms % 250;
        float env = 0;
        if (slot < 3 && in_slot < 150) env = std::min({in_slot, 150 - in_slot, 8}) / 8.0f;   // плавные края
        const float hz = (static_cast<int>(n / (2 * MIC_RATE)) & 1) ? 1175.0f : 880.0f;
        phase += 2.0f * static_cast<float>(M_PI) * hz / MIC_RATE;
        if (phase > 2.0f * static_cast<float>(M_PI)) phase -= 2.0f * static_cast<float>(M_PI);
        chunk[i] = static_cast<int16_t>(std::sin(phase) * env * 22000.0f);
      }
      const uint8_t *p = reinterpret_cast<const uint8_t *>(chunk);
      size_t left = sizeof chunk;
      while (left && !s_alarm_stop && s_accept) {
        const size_t sent = xStreamBufferSend(s_spk_sb, p, left, pdMS_TO_TICKS(100));
        p += sent;
        left -= sent;
      }
    }
    if (!s_alarm_stop) play_end();
    for (int i = 0; s_speaking && !s_alarm_stop && i < 800; ++i) vTaskDelay(pdMS_TO_TICKS(10));   // доиграть буфер
    s_alarm_on = false;
  }
}

void player_task(void *) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    play_once();
    if (s_done) s_done();
  }
}

}  // namespace

void start(SendFn send_mic, DoneFn on_played, BargeFn on_barge_in) {
  s_send = send_mic;
  s_done = on_played;
  s_barge = on_barge_in;
  s_mic_sb = make_buffer(MIC_BUF, MIC_FRAME * sizeof(int16_t) * MIC_FRAMES_PER_SEND, &s_mic_sb_ctl);
  s_spk_sb = make_buffer(SPK_BUF, 1, &s_spk_sb_ctl);
  if (!s_mic_sb || !s_spk_sb) {
    ESP_LOGE(TAG, "нет памяти под звуковые буферы — звука не будет");
    return;
  }
  init_mic();
  init_speaker();
  // ядро 0, выше отрисовки: звук всегда вытеснит помощника лица
  xTaskCreatePinnedToCore(mic_task, "mic", 4096, nullptr, 8, &s_mic, 0);
  xTaskCreatePinnedToCore(alarm_task, "alarm", 3072, nullptr, 4, &s_alarm_task, 0);
  xTaskCreatePinnedToCore(mic_send_task, "mic_send", 4096, nullptr, 6, nullptr, 0);
  xTaskCreatePinnedToCore(player_task, "player", 3072, nullptr, 8, &s_player, 0);
  ESP_LOGI(TAG, "микрофон и динамик готовы");
}

void listen(bool on, int gain_db) {
  if (!s_mic) return;
  gain_db = std::clamp(gain_db, 0, 30);
  s_gain_q8 = static_cast<int>(std::lround(256.0 * std::pow(10.0, gain_db / 20.0)));
  if (on && !s_listening) ESP_LOGI(TAG, "слушаю (усиление %d дБ)", gain_db);
  if (!on && s_listening) ESP_LOGI(TAG, "микрофон выключен");
  s_listening = on;
}

bool mic_pause() { return s_listening.exchange(false); }

void mic_resume(bool was_listening) {
  if (was_listening) s_listening = true;
}

void set_barge_in(bool on, float ratio) {
  s_barge_ratio_x100 = static_cast<int>(std::clamp(ratio, 1.2f, 20.0f) * 100.0f);
  if (on != s_barge_on) ESP_LOGI(TAG, "перебивание %s (порог ×%.1f)", on ? "включено" : "выключено", ratio);
  s_barge_on = on;
}

void set_clap_handler(ClapFn on_clap) { s_clap = on_clap; }

void set_clap(bool on, int sensitivity) {
  s_clap_sens = std::clamp(sensitivity, 1, 10);
  if (on != s_clap_on) ESP_LOGI(TAG, "хлопки %s (чувствительность %d)", on ? "включены" : "выключены", s_clap_sens.load());
  s_clap_on = on;
}

void alarm(int seconds) {
  if (!s_alarm_task) return;
  xTaskNotify(s_alarm_task, static_cast<uint32_t>(std::clamp(seconds, 1, 120)), eSetValueWithOverwrite);
}

void alarm_stop() {
  if (!s_alarm_on) return;      // говорит сервер, а не будильник — не трогаем
  s_alarm_stop = true;
  play_stop();
}

void play_start(int rate) {
  if (!s_player) return;
  s_rate_req = std::clamp(rate, 8000, 48000);
  s_stop = false;
  s_ended = false;
  s_accept = true;
  xTaskNotifyGive(s_player);
}

void play_data(const uint8_t *data, size_t len) {
  if (!s_accept || !s_spk_sb) return;
  const size_t sent = xStreamBufferSend(s_spk_sb, data, len, 0);
  if (sent < len) s_overflow += len - sent;
}

void play_end() {
  s_accept = false;
  s_ended = true;
}

void play_stop() {
  s_accept = false;
  s_stop = true;
}

// Квадратичная шкала с потолком: выше ~−9 дБ полной шкалы динамик и питание усилителя от USB
// начинают хрипеть, поэтому 100 % — это ×0.36, а 50 % (×0.09) — громко и ещё чисто.
bool playing() { return s_speaking.load() || s_alarm_on.load(); }
float mic_level() { return s_level.load(std::memory_order_relaxed); }
uint32_t bang_count() { return s_bangs.load(); }
uint32_t beat_count() { return s_beats.load(); }

void set_volume(int level) {
  const float v = std::clamp(level, 0, 100) / 100.0f;
  s_vol_q15 = static_cast<int>(v * v * MAX_GAIN * 32768.0f);
}

}  // namespace audio
