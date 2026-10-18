#include "audio/audio.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>

#include "board.h"
#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
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

SendFn s_send = nullptr;
DoneFn s_done = nullptr;

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
std::atomic<int> s_vol_q15{16056};            // громкость: 70 % → (0.7)² по квадратичной шкале
std::atomic<bool> s_accept{false};            // принимать кадры звука в буфер
std::atomic<bool> s_ended{false};
std::atomic<bool> s_stop{false};
std::atomic<int> s_rate_req{16000};
std::atomic<uint32_t> s_overflow{0};
int s_rate = 16000;

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
void mic_task(void *) {
  static int32_t raw[MIC_FRAME];
  static int16_t pcm[MIC_FRAME];
  Highpass hpf(MIC_HIGHPASS_HZ, MIC_RATE);
  uint32_t dropped = 0;
  for (;;) {
    size_t got = 0;
    if (i2s_channel_read(s_rx, raw, sizeof raw, &got, portMAX_DELAY) != ESP_OK) continue;
    const int n = got / sizeof(int32_t);
    const float gain = s_gain_q8.load() / 256.0f / 256.0f;  // 24 бита → 16 и усиление
    for (int i = 0; i < n; ++i) {
      const float y = hpf(static_cast<float>(raw[i] >> 8));
      pcm[i] = static_cast<int16_t>(std::clamp(y * gain, -32768.0f, 32767.0f));
    }
    if (!s_listening) continue;
    if (xStreamBufferSend(s_mic_sb, pcm, n * sizeof(int16_t), 0) != n * sizeof(int16_t)) {
      if (dropped++ % 50 == 0) ESP_LOGW(TAG, "микрофон: сеть не успевает, теряю кадры (%lu)", (unsigned long)dropped);
    }
  }
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
  ESP_ERROR_CHECK(i2s_channel_enable(s_tx));
  size_t carry = 0;
  uint32_t underruns = 0, samples_total = 0;
  for (;;) {
    if (s_stop) break;
    size_t n = carry + xStreamBufferReceive(s_spk_sb, in + carry, SPK_CHUNK * 2, pdMS_TO_TICKS(40));
    const size_t samples = n / 2;
    if (samples) {
      const int vol = s_vol_q15;
      for (size_t i = 0; i < samples; ++i) {
        int16_t x;
        memcpy(&x, in + 2 * i, 2);
        out[i] = static_cast<int16_t>((x * vol) >> 15);
      }
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
  if (s_stop) xStreamBufferReset(s_spk_sb);  // писатель уже не пишет (s_accept снят)
  const uint32_t overflow = s_overflow.exchange(0);
  ESP_LOGI(TAG, "доиграл %.1f с%s, пропусков %lu, не влезло %lu байт", samples_total / (float)s_rate,
           s_stop ? " (остановлен)" : "", (unsigned long)underruns, (unsigned long)overflow);
}

void player_task(void *) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    play_once();
    if (s_done) s_done();
  }
}

}  // namespace

void start(SendFn send_mic, DoneFn on_played) {
  s_send = send_mic;
  s_done = on_played;
  s_mic_sb = make_buffer(MIC_BUF, MIC_FRAME * sizeof(int16_t) * MIC_FRAMES_PER_SEND, &s_mic_sb_ctl);
  s_spk_sb = make_buffer(SPK_BUF, 1, &s_spk_sb_ctl);
  if (!s_mic_sb || !s_spk_sb) {
    ESP_LOGE(TAG, "нет памяти под звуковые буферы — звука не будет");
    return;
  }
  init_mic();
  init_speaker();
  // ядро 0, выше отрисовки: звук всегда вытеснит помощника лица
  xTaskCreatePinnedToCore(mic_task, "mic", 3072, nullptr, 8, &s_mic, 0);
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

void set_volume(int level) {
  const float v = std::clamp(level, 0, 100) / 100.0f;
  s_vol_q15 = static_cast<int>(v * v * 32768.0f);
}

}  // namespace audio
