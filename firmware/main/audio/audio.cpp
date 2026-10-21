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
constexpr int MIC_FRAME = 320;                // 20 Ð¼Ñ
// Ð’ ÑÐµÑ‚ÑŒ â€” Ð¿Ð°Ñ‡ÐºÐ°Ð¼Ð¸ Ð¿Ð¾ 100 Ð¼Ñ: ÐºÐ°Ð¶Ð´Ñ‹Ð¹ Ð¿Ð°ÐºÐµÑ‚ Wi-Fi ÑÐ»Ñ‹ÑˆÐµÐ½ Ð² Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½Ðµ Ñ‰ÐµÐ»Ñ‡ÐºÐ¾Ð¼ (ÑÐ¼. Highpass),
// Ð¸ Ð¿Ð°Ñ‡ÐºÐ¸ Ð´Ð°ÑŽÑ‚ Ð¸Ñ… Ð² 5 Ñ€Ð°Ð· Ð¼ÐµÐ½ÑŒÑˆÐµ, Ñ‡ÐµÐ¼ ÐºÐ°Ð´Ñ€ Ð½Ð° ÐºÐ°Ð¶Ð´Ñ‹Ðµ 20 Ð¼Ñ.
constexpr int MIC_FRAMES_PER_SEND = 5;
constexpr float MIC_HIGHPASS_HZ = 100.0f;
constexpr size_t MIC_BUF = 32 * 1024;         // ~1 Ñ â€” ÐµÑÐ»Ð¸ ÑÐµÑ‚ÑŒ Ð¿Ñ€Ð¸Ñ‚Ð¾Ñ€Ð¼Ð¾Ð·Ð¸Ð»Ð°
constexpr size_t SPK_BUF = 128 * 1024;        // 4 Ñ Ð¿Ñ€Ð¸ 16 ÐºÐ“Ñ†
constexpr int SPK_DMA_DESC = 6;
constexpr int SPK_DMA_FRAMES = 240;           // 15 Ð¼Ñ Ð½Ð° Ð´ÐµÑÐºÑ€Ð¸Ð¿Ñ‚Ð¾Ñ€ Ð¿Ñ€Ð¸ 16 ÐºÐ“Ñ†
constexpr int SPK_CHUNK = 512;                // ÑÑÐ¼Ð¿Ð»Ð¾Ð² Ð·Ð° Ð¿Ñ€Ð¾Ñ…Ð¾Ð´ Ð¿Ñ€Ð¾Ð¸Ð³Ñ€Ñ‹Ð²Ð°Ñ‚ÐµÐ»Ñ
constexpr int PREBUF_MS = 400;
// ÐœÐ°Ð»ÐµÐ½ÑŒÐºÐ¸Ð¹ Ð´Ð¸Ð½Ð°Ð¼Ð¸Ðº Ð½Ð¸Ð¶Ðµ ~160 Ð“Ñ† Ð½Ðµ Ð·Ð²ÑƒÑ‡Ð¸Ñ‚, Ð° Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð±ÑŒÑ‘Ñ‚ÑÑ Ð¸ Ñ…Ñ€Ð¸Ð¿Ð¸Ñ‚, Ð¸ ÑƒÑÐ¸Ð»Ð¸Ñ‚ÐµÐ»ÑŒ Ñ‚ÑÐ½ÐµÑ‚ Ð¸Ð·
// USB Ð¿Ð¸ÐºÐ¸ Ñ‚Ð¾ÐºÐ°. Ð“Ð¾Ð»Ð¾Ñ Gemini â€” Ð±Ð¾Ð»ÑŒÑˆÐµ Ð¿Ð¾Ð»Ð¾Ð²Ð¸Ð½Ñ‹ ÑÐ½ÐµÑ€Ð³Ð¸Ð¸ Ð½Ð¸Ð¶Ðµ 400 Ð“Ñ†, Ð±Ð°ÑÑ‹ ÑÑ€ÐµÐ·Ð°ÐµÐ¼.
constexpr float SPK_HIGHPASS_HZ = 300.0f;
constexpr float LIMIT_KNEE = 0.5f;            // Ð´Ð¾Ð»Ñ Ð¿Ð¾Ð»Ð½Ð¾Ð¹ ÑˆÐºÐ°Ð»Ñ‹, Ñ ÐºÐ¾Ñ‚Ð¾Ñ€Ð¾Ð¹ Ñ€Ð°Ð±Ð¾Ñ‚Ð°ÐµÑ‚ Ð¾Ð³Ñ€Ð°Ð½Ð¸Ñ‡Ð¸Ñ‚ÐµÐ»ÑŒ
constexpr float MAX_GAIN = 0.36f;             // Ð³Ñ€Ð¾Ð¼ÐºÐ¾ÑÑ‚ÑŒ 100 %

constexpr int BARGE_FRAMES = 10;              // 200 Ð¼Ñ Ð¿Ð¾Ð´Ñ€ÑÐ´ Ð³Ñ€Ð¾Ð¼Ñ‡Ðµ ÑÑ…Ð° â€” Ð¿ÐµÑ€ÐµÐ±Ð¸Ð»Ð¸
constexpr int BARGE_PREROLL_FRAMES = 12;      // 240 Ð¼Ñ Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½Ð° Ð¾Ñ‚Ð´Ð°Ñ‘Ð¼ ÑÐµÑ€Ð²ÐµÑ€Ñƒ Ð²ÑÐ»ÐµÐ´
constexpr int BARGE_GRACE_FRAMES = 12;        // Ð¿ÐµÑ€Ð²Ñ‹Ðµ 240 Ð¼Ñ Ð¾Ñ‚Ð²ÐµÑ‚Ð° Ð½Ðµ ÑÐ»ÑƒÑˆÐ°ÐµÐ¼: ÐµÑ‰Ñ‘ Ð½ÐµÑ‚ ÑÑ…Ð°
constexpr float BARGE_MIN_RMS = 2500.0f;      // Ñ‚Ð¸ÑˆÐµ Ð½Ðµ ÑÑ‡Ð¸Ñ‚Ð°ÐµÑ‚ÑÑ Ð³Ð¾Ð»Ð¾ÑÐ¾Ð¼ (~âˆ’22 Ð´Ð‘FS)
constexpr float BARGE_QUIET_RMS = 4500.0f;    // Ð¿Ð°ÑƒÐ·Ð° Ð² Ð¾Ñ‚Ð²ÐµÑ‚Ðµ: Ð³Ð¾Ð»Ð¾Ñ Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð±Ñ‹Ñ‚ÑŒ ÑÑ€ÐºÐ¾ ÑÐ»Ñ‹ÑˆÐµÐ½
constexpr int BARGE_TAIL_FRAMES = 15;         // Ð¿Ð¾ÑÐ»Ðµ Ð·Ð²ÑƒÐºÐ° 300 Ð¼Ñ Ð½Ðµ ÑÐ»ÑƒÑˆÐ°ÐµÐ¼: Ñ…Ð²Ð¾ÑÑ‚ ÑÑ…Ð° Ð¸ ÐºÐ¾Ð¼Ð½Ð°Ñ‚Ð°
constexpr float BARGE_K_FLOOR = 2.0f;         // ÑÑ…Ð¾ Ð² Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½Ðµ Ð½Ðµ ÑÐ»Ð°Ð±ÐµÐµ 2Ã— Ð¸Ð³Ñ€Ð°ÑŽÑ‰ÐµÐ³Ð¾: Ð½Ð¸Ð¶Ð½ÑÑ Ð³Ñ€Ð°Ð½Ð¸Ñ†Ð° k
constexpr int BARGE_LEARN_FRAMES = 50;        // Ð¿ÐµÑ€Ð²Ð°Ñ ÑÐµÐºÑƒÐ½Ð´Ð° Ð¾Ñ‚Ð²ÐµÑ‚Ð°: k Ð¿Ð¾Ð´ÑÑ‚Ñ€Ð°Ð¸Ð²Ð°ÐµÑ‚ÑÑ Ð±Ñ‹ÑÑ‚Ñ€Ð¾
constexpr float BARGE_REF_MIN = 150.0f;       // ÑÑ‚Ð°Ð»Ð¾Ð½ Ñ‚Ð¸ÑˆÐµ â€” Ð¿Ð°ÑƒÐ·Ð° Ð² Ñ€ÐµÑ‡Ð¸, ÑÑ€Ð°Ð²Ð½Ð¸Ð²Ð°Ñ‚ÑŒ Ð½Ðµ Ñ Ñ‡ÐµÐ¼

SendFn s_send = nullptr;
DoneFn s_done = nullptr;
BargeFn s_barge = nullptr;
ClapFn s_clap = nullptr;

i2s_chan_handle_t s_rx = nullptr;
i2s_chan_handle_t s_tx = nullptr;
StreamBufferHandle_t s_mic_sb = nullptr;      // Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½ â†’ Ð·Ð°Ð´Ð°Ñ‡Ð° Ð¾Ñ‚Ð¿Ñ€Ð°Ð²ÐºÐ¸
StreamBufferHandle_t s_spk_sb = nullptr;      // WebSocket â†’ Ð¿Ñ€Ð¾Ð¸Ð³Ñ€Ñ‹Ð²Ð°Ñ‚ÐµÐ»ÑŒ
StaticStreamBuffer_t s_mic_sb_ctl;
StaticStreamBuffer_t s_spk_sb_ctl;
TaskHandle_t s_player = nullptr;
TaskHandle_t s_mic = nullptr;

std::atomic<bool> s_listening{false};
std::atomic<int> s_gain_q8{1019};             // ÑƒÑÐ¸Ð»ÐµÐ½Ð¸Ðµ Ã—256: 12 Ð´Ð‘ Ð¿Ð¾ ÑƒÐ¼Ð¾Ð»Ñ‡Ð°Ð½Ð¸ÑŽ
std::atomic<int> s_vol_q15{2949};             // Ð³Ñ€Ð¾Ð¼ÐºÐ¾ÑÑ‚ÑŒ 50 %, Ð¿Ð¾ÐºÐ° ÑÐµÑ€Ð²ÐµÑ€ Ð½Ðµ Ð¿Ñ€Ð¸ÑÐ»Ð°Ð» ÑÐ²Ð¾ÑŽ
std::atomic<bool> s_accept{false};            // Ð¿Ñ€Ð¸Ð½Ð¸Ð¼Ð°Ñ‚ÑŒ ÐºÐ°Ð´Ñ€Ñ‹ Ð·Ð²ÑƒÐºÐ° Ð² Ð±ÑƒÑ„ÐµÑ€
std::atomic<bool> s_speaking{false};          // Ð´Ð¸Ð½Ð°Ð¼Ð¸Ðº ÑÐµÐ¹Ñ‡Ð°Ñ Ð¸Ð³Ñ€Ð°ÐµÑ‚
std::atomic<bool> s_barge_on{false};
std::atomic<bool> s_barge_armed{false};       // Ð·Ð²ÑƒÐº Ñ€ÐµÐ°Ð»ÑŒÐ½Ð¾ Ð·Ð²ÑƒÑ‡Ð¸Ñ‚ (Ð¿Ð¾ÑÐ»Ðµ Ð½Ð°ÐºÐ¾Ð¿Ð»ÐµÐ½Ð¸Ñ), Ð° Ð½Ðµ Ð¶Ð´Ñ‘Ñ‚ Ð½Ð°Ñ‡Ð°Ð»Ð°
std::atomic<int> s_barge_ratio_x100{300};
std::atomic<float> s_ref_rms{0};              // Ð³Ñ€Ð¾Ð¼ÐºÐ¾ÑÑ‚ÑŒ Ñ‚Ð¾Ð³Ð¾, Ñ‡Ñ‚Ð¾ ÑÐµÐ¹Ñ‡Ð°Ñ Ð¸Ð³Ñ€Ð°ÐµÑ‚ (Ñ Ð·Ð°Ð´ÐµÑ€Ð¶ÐºÐ¾Ð¹ Ð·Ð°Ñ‚ÑƒÑ…Ð°Ð½Ð¸Ñ)
std::atomic<bool> s_clap_on{true};
std::atomic<int> s_clap_sens{5};
std::atomic<bool> s_alarm_stop{false};
std::atomic<bool> s_alarm_on{false};          // Ð²ÑÑ‚Ñ€Ð¾ÐµÐ½Ð½Ñ‹Ð¹ ÑÐ¸Ð³Ð½Ð°Ð» Ð·Ð²ÑƒÑ‡Ð¸Ñ‚
TaskHandle_t s_alarm_task = nullptr;
std::atomic<bool> s_ended{false};
std::atomic<bool> s_stop{false};
std::atomic<int> s_rate_req{16000};
std::atomic<uint32_t> s_overflow{0};
int s_rate = 16000;

// â”€â”€ ÑÐ»ÑƒÑ… Ð´Ð»Ñ ÑÐºÑ€Ð°Ð½Ð°: ÑƒÑ€Ð¾Ð²ÐµÐ½ÑŒ Ð³Ð¾Ð»Ð¾ÑÐ°, Ñ€ÐµÐ·ÐºÐ¸Ðµ Ð·Ð²ÑƒÐºÐ¸, Ñ€Ð¸Ñ‚Ð¼ â”€â”€
std::atomic<float> s_level{0};
std::atomic<uint32_t> s_bangs{0}, s_beats{0};

struct Ear {
  float floor = 60;      // Ñ„Ð¾Ð½ ÐºÐ¾Ð¼Ð½Ð°Ñ‚Ñ‹ (rms, ÐµÐ´Ð¸Ð½Ð¸Ñ†Ñ‹ int16), Ð¼ÐµÐ´Ð»ÐµÐ½Ð½Ð¾ Ð¿Ð¾Ð´ÑÑ‚Ñ€Ð°Ð¸Ð²Ð°ÐµÑ‚ÑÑ
  float fast = 0;        // Ð±Ñ‹ÑÑ‚Ñ€Ð°Ñ Ð¾Ð³Ð¸Ð±Ð°ÑŽÑ‰Ð°Ñ
  float slow = 0;        // Ð¼ÐµÐ´Ð»ÐµÐ½Ð½Ð°Ñ (~300 Ð¼Ñ)
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
    // ÑƒÑ€Ð¾Ð²ÐµÐ½ÑŒ Ð´Ð»Ñ Ñ„Ð¾Ñ€Ð¼Ñ‹ Ð³Ð»Ð°Ð·: Ð½Ð°Ð´ Ñ„Ð¾Ð½Ð¾Ð¼, Ñ Ð·Ð°Ð¿Ð°ÑÐ¾Ð¼ Ð³Ñ€Ð¾Ð¼ÐºÐ¾ÑÑ‚Ð¸ Ð³Ð¾Ð»Ð¾ÑÐ° ~ 4000
    const float v = std::clamp((m - floor * 1.4f) / 3500.0f, 0.0f, 1.0f);
    lvl += (v - lvl) * (v > lvl ? 0.6f : 0.12f);
    s_level.store(lvl, std::memory_order_relaxed);
    if (bang_cool > 0) --bang_cool;
    if (beat_cool > 0) --beat_cool;
    if (speaking) return;  // ÑÑ…Ð¾ ÑÐ²Ð¾ÐµÐ³Ð¾ Ð¾Ñ‚Ð²ÐµÑ‚Ð° Ð·Ð° Ñ…Ð»Ð¾Ð¿Ð¾Ðº Ð½Ðµ ÑÑ‡Ð¸Ñ‚Ð°ÐµÐ¼
    // Ñ…Ð»Ð¾Ð¿Ð¾Ðº/ÑÑ‚ÑƒÐº: Ñ€ÐµÐ·ÐºÐ¾ Ð²Ñ‹ÑˆÐµ Ñ„Ð¾Ð½Ð° Ð¸ Ð½Ð°Ð¼Ð½Ð¾Ð³Ð¾ Ð²Ñ‹ÑˆÐµ Ð½ÐµÐ´Ð°Ð²Ð½ÐµÐ³Ð¾ ÑÑ€ÐµÐ´Ð½ÐµÐ³Ð¾
    if (bang_cool == 0 && m > std::max(floor * 6.0f, 2200.0f) && m > before * 4.0f) {
      s_bangs.fetch_add(1);
      bang_cool = 15;  // 300 Ð¼Ñ
    }
    // ÑƒÐ´Ð°Ñ€ Ñ€Ð¸Ñ‚Ð¼Ð°: Ð·Ð°Ð¼ÐµÑ‚Ð½Ñ‹Ð¹ Ð²ÑÐ¿Ð»ÐµÑÐº Ð½Ð°Ð´ Ð¼ÐµÐ´Ð»ÐµÐ½Ð½Ð¾Ð¹ Ð¾Ð³Ð¸Ð±Ð°ÑŽÑ‰ÐµÐ¹, Ð½Ðµ Ñ€ÐµÐ¶Ðµ Ñ‡ÐµÐ¼ 250 Ð¼Ñ
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

// â”€â”€ Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½ â”€â”€

void init_mic() {
  i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  cc.dma_desc_num = 6;
  cc.dma_frame_num = MIC_FRAME;
  ESP_ERROR_CHECK(i2s_new_channel(&cc, nullptr, &s_rx));
  // INMP441 Ð¾Ñ‚Ð´Ð°Ñ‘Ñ‚ 24 Ð±Ð¸Ñ‚Ð° Ð² 32-Ð±Ð¸Ñ‚Ð½Ð¾Ð¼ ÑÐ»Ð¾Ñ‚Ðµ, L/R Ð½Ð° Ð·ÐµÐ¼Ð»Ðµ â€” Ð»ÐµÐ²Ñ‹Ð¹ ÐºÐ°Ð½Ð°Ð»
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
  // Ñ€Ð°Ð±Ð¾Ñ‚Ð°ÐµÑ‚ Ð²ÑÐµÐ³Ð´Ð°: INMP441 Ð¿Ð¾ÑÐ»Ðµ Ð²ÐºÐ»ÑŽÑ‡ÐµÐ½Ð¸Ñ Ñ‚Ð°ÐºÑ‚Ð¾Ð² ~90 Ð¼Ñ Ð²Ñ‹Ð´Ð°Ñ‘Ñ‚ Ð¼ÑƒÑÐ¾Ñ€, Ð¸ ÑÐ»Ð¾Ð²Ð¾
  // Ð¿Ñ€Ð¾Ð±ÑƒÐ¶Ð´ÐµÐ½Ð¸Ñ (ÑÑ‚Ð°Ð¿ 5) Ð²ÑÑ‘ Ñ€Ð°Ð²Ð½Ð¾ ÑÐ»ÑƒÑˆÐ°ÐµÑ‚ Ð¿Ð¾ÑÑ‚Ð¾ÑÐ½Ð½Ð¾
  ESP_ERROR_CHECK(i2s_channel_enable(s_rx));
}

// Ð¤Ð¸Ð»ÑŒÑ‚Ñ€ Ð²Ñ‹ÑÐ¾ÐºÐ¸Ñ… Ñ‡Ð°ÑÑ‚Ð¾Ñ‚, Ð‘Ð°Ñ‚Ñ‚ÐµÑ€Ð²Ð¾Ñ€Ñ‚ 2-Ð³Ð¾ Ð¿Ð¾Ñ€ÑÐ´ÐºÐ°. Ð¡Ñ€ÐµÐ·Ð°ÐµÑ‚ Ð¿Ð¾ÑÑ‚Ð¾ÑÐ½Ð½ÑƒÑŽ ÑÐ¾ÑÑ‚Ð°Ð²Ð»ÑÑŽÑ‰ÑƒÑŽ Ð¸, Ð³Ð»Ð°Ð²Ð½Ð¾Ðµ,
// Ð¿Ð¾Ð¼ÐµÑ…Ñƒ Ð¾Ñ‚ Ð¿ÐµÑ€ÐµÐ´Ð°Ñ‚Ñ‡Ð¸ÐºÐ° Wi-Fi: ÐºÐ°Ð¶Ð´Ñ‹Ð¹ Ð¿Ð°ÐºÐµÑ‚ â€” Ð±Ñ€Ð¾ÑÐ¾Ðº Ñ‚Ð¾ÐºÐ°, Ð¿Ð¸Ñ‚Ð°Ð½Ð¸Ðµ Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½Ð° Ð¿Ñ€Ð¾ÑÐµÐ´Ð°ÐµÑ‚, Ð¸ Ð²
// Ð·Ð²ÑƒÐº Ð¿Ð¾Ð¿Ð°Ð´Ð°ÐµÑ‚ Ð¸Ð¼Ð¿ÑƒÐ»ÑŒÑ, Ð¿Ð¾Ñ‡Ñ‚Ð¸ Ð²ÐµÑÑŒ Ð½Ð¸Ð¶Ðµ 150 Ð“Ñ†. ÐÐ° 100 Ð“Ñ† ÑˆÑƒÐ¼ Ð¿Ð°Ð´Ð°ÐµÑ‚ Ð½Ð° ~13 Ð´Ð‘, Ð³Ð¾Ð»Ð¾Ñ â€” Ð½Ð° 2.
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

// Ð§Ð¸Ñ‚Ð°ÐµÑ‚ Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½ Ð²ÑÐµÐ³Ð´Ð° (Ñ„Ð¸Ð»ÑŒÑ‚Ñ€ Ð½Ðµ Ð¾ÑÑ‚Ñ‹Ð²Ð°ÐµÑ‚), Ð¾Ñ‚Ð´Ð°Ñ‘Ñ‚ â€” Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð¿Ð¾ÐºÐ° ÑÐ»ÑƒÑˆÐ°ÐµÐ¼. ÐžÑ‚Ð¿Ñ€Ð°Ð²ÐºÐ° Ð²
// ÑÐµÑ‚ÑŒ â€” Ð² Ð¾Ñ‚Ð´ÐµÐ»ÑŒÐ½Ð¾Ð¹ Ð·Ð°Ð´Ð°Ñ‡Ðµ, Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð·Ð°Ð´ÐµÑ€Ð¶ÐºÐ° ÑÐµÑ‚Ð¸ Ð½Ðµ Ñ€Ð²Ð°Ð»Ð° DMA.
// ÐŸÐµÑ€ÐµÐ±Ð¸Ð²Ð°Ð½Ð¸Ðµ. Ð­Ñ…Ð¾ ÑÐ¾Ð±ÑÑ‚Ð²ÐµÐ½Ð½Ð¾Ð³Ð¾ Ð¾Ñ‚Ð²ÐµÑ‚Ð° Ð² Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½Ðµ Ð¿Ñ€Ð¾Ð¿Ð¾Ñ€Ñ†Ð¸Ð¾Ð½Ð°Ð»ÑŒÐ½Ð¾ Ñ‚Ð¾Ð¼Ñƒ, Ñ‡Ñ‚Ð¾ Ð¸Ð³Ñ€Ð°ÐµÑ‚, Ð¿Ð¾ÑÑ‚Ð¾Ð¼Ñƒ
// ÑÑ€Ð°Ð²Ð½Ð¸Ð²Ð°ÐµÐ¼ Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½ Ð½Ðµ Ñ Ð¿Ð¾Ñ€Ð¾Ð³Ð¾Ð¼, Ð° Ñ Ð³Ñ€Ð¾Ð¼ÐºÐ¾ÑÑ‚ÑŒÑŽ Ð²Ð¾ÑÐ¿Ñ€Ð¾Ð¸Ð·Ð²ÐµÐ´ÐµÐ½Ð¸Ñ: Ð¾Ñ‚Ð½Ð¾ÑˆÐµÐ½Ð¸Ðµ Â«Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½ /
// Ð¸Ð³Ñ€Ð°ÑŽÑ‰ÐµÐµÂ» Ð² Ð½Ð¾Ñ€Ð¼Ðµ Ð¿Ð¾Ñ‡Ñ‚Ð¸ Ð¿Ð¾ÑÑ‚Ð¾ÑÐ½Ð½Ð¾ (ÐµÐ³Ð¾ Ð¸ Ð·Ð°Ð¿Ð¾Ð¼Ð¸Ð½Ð°ÐµÐ¼, k), Ð° Ð³Ð¾Ð»Ð¾Ñ Ð¿Ð¾Ð²ÐµÑ€Ñ… ÑÑ…Ð° ÐµÐ³Ð¾ Ñ€ÐµÐ·ÐºÐ¾
// Ð¿Ð¾Ð´Ð½Ð¸Ð¼Ð°ÐµÑ‚. Ð­Ñ‚Ð°Ð»Ð¾Ð½ â€” Ð¿Ð¸Ðº Ð¸Ð³Ñ€Ð°ÑŽÑ‰ÐµÐ³Ð¾ Ð·Ð²ÑƒÐºÐ° Ñ Ð·Ð°Ñ‚ÑƒÑ…Ð°Ð½Ð¸ÐµÐ¼ ~200 Ð¼Ñ (Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½ ÑÐ»Ñ‹ÑˆÐ¸Ñ‚ Ñ Ð·Ð°Ð´ÐµÑ€Ð¶ÐºÐ¾Ð¹).
struct Barge {
  static constexpr size_t RING = BARGE_PREROLL_FRAMES;
  int16_t ring[RING][MIC_FRAME];
  int head = 0;
  int frames = 0;       // ÑÐºÐ¾Ð»ÑŒÐºÐ¾ ÐºÐ°Ð´Ñ€Ð¾Ð² Ð¸Ð´Ñ‘Ñ‚ ÑÑ‚Ð¾Ñ‚ Ð¾Ñ‚Ð²ÐµÑ‚
  int hits = 0;         // Ð¿Ð¾Ð´Ñ€ÑÐ´ Ð³Ñ€Ð¾Ð¼Ñ‡Ðµ ÑÑ…Ð°
  int quiet = 0;        // Ð¿Ð¾Ð´Ñ€ÑÐ´ ÐºÐ°Ð´Ñ€Ð¾Ð², ÐºÐ¾Ð³Ð´Ð° Ð¸Ð³Ñ€Ð°ÑŽÑ‰ÐµÐµ Ð¼Ð¾Ð»Ñ‡Ð¸Ñ‚
  float k = 0;          // Ð¾Ð±Ñ‹Ñ‡Ð½Ð¾Ðµ Ð¾Ñ‚Ð½Ð¾ÑˆÐµÐ½Ð¸Ðµ Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½/ÑÑ‚Ð°Ð»Ð¾Ð½
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
      // Ð¸Ð³Ñ€Ð°ÑŽÑ‰ÐµÐµ Ð¼Ð¾Ð»Ñ‡Ð¸Ñ‚ (Ð¿Ð°ÑƒÐ·Ð° Ð¼ÐµÐ¶Ð´Ñƒ Ñ„Ñ€Ð°Ð·Ð°Ð¼Ð¸): ÑÐ»ÑƒÑˆÐ°ÐµÐ¼ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð¿Ð¾ÑÐ»Ðµ 300 Ð¼Ñ Ñ‚Ð¸ÑˆÐ¸Ð½Ñ‹ Ð² ÑÑ‚Ð°Ð»Ð¾Ð½Ðµ
      loud = ++quiet > BARGE_TAIL_FRAMES && m > BARGE_QUIET_RMS;
    } else {
      quiet = 0;
      const float q = m / r;
      loud = m > BARGE_MIN_RMS && k > 0 && q > k * ratio;
      if (!loud) {      // ÑÑ…Ð¾: ÑƒÑ‚Ð¾Ñ‡Ð½ÑÐµÐ¼ Â«Ð¾Ð±Ñ‹Ñ‡Ð½Ð¾ÐµÂ» Ð¾Ñ‚Ð½Ð¾ÑˆÐµÐ½Ð¸Ðµ. Ð’ Ð½Ð°Ñ‡Ð°Ð»Ðµ Ð¾Ñ‚Ð²ÐµÑ‚Ð° ÑÑ…Ð¾ ÐµÑ‰Ñ‘ Ð½Ð°Ñ€Ð°ÑÑ‚Ð°ÐµÑ‚: ÑƒÑ‡Ð¸Ð¼ÑÑ Ð±Ñ‹ÑÑ‚Ñ€Ð¾ Ð¸
                        // Ð½Ðµ Ð´Ð°Ñ‘Ð¼ k ÑƒÐ¿Ð°ÑÑ‚ÑŒ Ð½Ð¸Ð¶Ðµ Ð¿Ð¾Ð»Ð°, Ð¸Ð½Ð°Ñ‡Ðµ Ð¾Ð±Ñ‹Ñ‡Ð½Ð¾Ðµ ÑÑ…Ð¾ Ð¿Ñ€Ð¸Ð½Ð¸Ð¼Ð°ÐµÑ‚ÑÑ Ð·Ð° Ð³Ð¾Ð»Ð¾Ñ Ð¸ Ñ„Ñ€Ð°Ð·Ð° Ñ€Ð²Ñ‘Ñ‚ÑÑ
        const float a = frames < BARGE_LEARN_FRAMES ? 0.15f : 0.02f;
        k = k > 0 ? k + (q - k) * a : q;
        k = std::max(k, BARGE_K_FLOOR);
      }
    }
    if ((++log_tick % 25) == 0) {
      ESP_LOGI(TAG, "Ð¿ÐµÑ€ÐµÐ±Ð¸Ð²Ð°Ð½Ð¸Ðµ: Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½ %.0f ÑÑ‚Ð°Ð»Ð¾Ð½ %.0f k %.2f %s", m, r, k, loud ? "Ð“Ð ÐžÐœÐšÐž" : "");
    }
    hits = loud ? hits + 1 : 0;
    return hits >= BARGE_FRAMES;
  }
};

void audio_barge_in(Barge &b);

// Ð§Ñ‚Ð¾ Ñ€ÐµÑˆÐ¸Ð» Ð´ÐµÑ‚ÐµÐºÑ‚Ð¾Ñ€ Ñ…Ð»Ð¾Ð¿ÐºÐ¾Ð²: Ð¿Ð¾ ÑÑ‚Ð¸Ð¼ ÑÑ‚Ñ€Ð¾ÐºÐ°Ð¼ Ð²Ð¸Ð´Ð½Ð¾, Ð³Ð´Ðµ Ñ…Ð»Ð¾Ð¿Ð¾Ðº Ð½Ðµ Ð¿Ñ€Ð¾ÑˆÑ‘Ð» (Ð¿Ð¸Ðº, Ñ„Ð¾Ð½, ÑÐ¿Ð°Ð´, Ð¸Ð½Ñ‚ÐµÑ€Ð²Ð°Ð»).
// Ð—Ð²ÑƒÐº Ð² Ð¶ÑƒÑ€Ð½Ð°Ð» Ð½Ðµ Ð¿Ð¾Ð¿Ð°Ð´Ð°ÐµÑ‚, Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ñ‡Ð¸ÑÐ»Ð°. ÐÐµ Ñ‡Ð°Ñ‰Ðµ 8 ÑÑ‚Ñ€Ð¾Ðº Ð² ÑÐµÐºÑƒÐ½Ð´Ñƒ, Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð½Ðµ Ð¼ÐµÑˆÐ°Ñ‚ÑŒ Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½Ñƒ.
void log_clap(const clap::Diag &d) {
  static int64_t window = 0;
  static int lines = 0;
  const int64_t now = esp_timer_get_time() / 1000000;
  if (now != window) window = now, lines = 0;
  if (lines++ >= 8) return;
  switch (d.kind) {
    case clap::Diag::Onset: ESP_LOGI(TAG, "Ñ…Ð»Ð¾Ð¿Ð¾Ðº: Ñ„Ñ€Ð¾Ð½Ñ‚, Ð¿Ð¸Ðº %d (%.0fÃ— Ñ„Ð¾Ð½Ð°), hf %.2f", d.peak, d.ratio, d.hf); break;
    case clap::Diag::NearMiss: ESP_LOGI(TAG, "Ñ…Ð»Ð¾Ð¿Ð¾Ðº: Ð½Ðµ Ñ„Ñ€Ð¾Ð½Ñ‚, Ð¿Ð¸Ðº %d (%.0fÃ— Ñ„Ð¾Ð½Ð°), hf %.2f", d.peak, d.ratio, d.hf); break;
    case clap::Diag::Settled: ESP_LOGI(TAG, "Ñ…Ð»Ð¾Ð¿Ð¾Ðº: ÑÐ¿Ð°Ð´ Ð¿Ñ€Ð¸Ð½ÑÑ‚ (%d Ð¸Ð· %d)", d.peak, d.onset_peak); break;
    case clap::Diag::TooSlow: ESP_LOGI(TAG, "Ñ…Ð»Ð¾Ð¿Ð¾Ðº: Ð·Ð²ÑƒÐº Ð½Ðµ ÑÐ¿Ð°Ð» (%d Ð¸Ð· %d), Ð½Ðµ Ñ…Ð»Ð¾Ð¿Ð¾Ðº", d.peak, d.onset_peak); break;
    case clap::Diag::First: ESP_LOGI(TAG, "Ñ…Ð»Ð¾Ð¿Ð¾Ðº: Ð¿ÐµÑ€Ð²Ñ‹Ð¹, Ð¶Ð´Ñƒ Ð²Ñ‚Ð¾Ñ€Ð¾Ð¹"); break;
    case clap::Diag::Echo: ESP_LOGI(TAG, "Ñ…Ð»Ð¾Ð¿Ð¾Ðº: ÑÐ»Ð¸ÑˆÐºÐ¾Ð¼ ÑÐºÐ¾Ñ€Ð¾ (%d Ð¼Ñ), ÑÑ‚Ð¾ ÑÑ…Ð¾", d.gap_ms); break;
    case clap::Diag::Timeout: ESP_LOGI(TAG, "Ñ…Ð»Ð¾Ð¿Ð¾Ðº: Ð²Ñ‚Ð¾Ñ€Ð¾Ð³Ð¾ Ð½Ðµ Ð±Ñ‹Ð»Ð¾"); break;
    case clap::Diag::Fired: ESP_LOGI(TAG, "Ñ…Ð»Ð¾Ð¿ÐºÐ¸: ÑÐµÑ€Ð¸Ñ Ñ‡ÐµÑ€ÐµÐ· %d Ð¼Ñ", d.gap_ms); break;
    case clap::Diag::None: break;
  }
}

void mic_task(void *) {
  static int32_t raw[MIC_FRAME];
  static int16_t pcm[MIC_FRAME];
  static int16_t flat[MIC_FRAME];               // Ñ‚Ð¾Ñ‚ Ð¶Ðµ Ð·Ð²ÑƒÐº Ð±ÐµÐ· ÑƒÑÐ¸Ð»ÐµÐ½Ð¸Ñ â€” Ð´Ð»Ñ Ð´ÐµÑ‚ÐµÐºÑ‚Ð¾Ñ€Ð° Ñ…Ð»Ð¾Ð¿ÐºÐ¾Ð²
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
    const float gain = s_gain_q8.load() / 256.0f / 256.0f;  // 24 Ð±Ð¸Ñ‚Ð° â†’ 16 Ð¸ ÑƒÑÐ¸Ð»ÐµÐ½Ð¸Ðµ
    for (int i = 0; i < n; ++i) {
      const float y = hpf(static_cast<float>(raw[i] >> 8));
      pcm[i] = static_cast<int16_t>(std::clamp(y * gain, -32768.0f, 32767.0f));
      flat[i] = static_cast<int16_t>(std::clamp(y / 256.0f, -32768.0f, 32767.0f));
    }
    // Ð¥Ð»Ð¾Ð¿ÐºÐ¸: Ñ€Ð°Ð±Ð¾Ñ‚Ð°ÑŽÑ‚ Ð²ÑÐµÐ³Ð´Ð°, Ð´Ð°Ð¶Ðµ Ð±ÐµÐ· ÑÐµÑ‚Ð¸ Ð¸ Ð±ÐµÐ· ÑÐ»Ð¾Ð²Ð° Ð¿Ñ€Ð¾Ð±ÑƒÐ¶Ð´ÐµÐ½Ð¸Ñ; Ð¿Ð¾ÐºÐ° Ð¸Ð³Ñ€Ð°ÐµÑ‚ Ð´Ð¸Ð½Ð°Ð¼Ð¸Ðº â€”
    // Ð¼Ð¾Ð»Ñ‡Ð°Ñ‚ (ÑÐ°Ð¼Ð¸ ÑÐµÐ±Ñ Ð¶Ðµ Ð¸ ÑƒÑÐ»Ñ‹ÑˆÐ¸Ð¼)
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
    // ÐŸÐ¾ÐºÐ° Ð´Ð¸Ð½Ð°Ð¼Ð¸Ðº Ð¸Ð³Ñ€Ð°ÐµÑ‚, Ð² ÑÐµÑ‚ÑŒ Ð½Ðµ ÑˆÐ»Ñ‘Ð¼: ÐºÐ°Ð¶Ð´Ñ‹Ð¹ Ð¿Ð°ÐºÐµÑ‚ Wi-Fi â€” Ð±Ñ€Ð¾ÑÐ¾Ðº Ñ‚Ð¾ÐºÐ°, Ð¸ ÑƒÑÐ¸Ð»Ð¸Ñ‚ÐµÐ»ÑŒ
    // Ñ‚Ñ€ÐµÑ‰Ð¸Ñ‚ Ð² Ñ‚Ð°ÐºÑ‚ Ð¾Ñ‚Ð¿Ñ€Ð°Ð²ÐºÐ°Ð¼. Ð¡Ð»ÑƒÑˆÐ°ÐµÐ¼ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð»Ð¾ÐºÐ°Ð»ÑŒÐ½Ð¾: Ð½Ðµ Ð¿ÐµÑ€ÐµÐ±Ð¸Ð²Ð°ÐµÑ‚ Ð»Ð¸ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»ÑŒ.
    if (s_speaking) {
      if (!s_barge_armed) barge.idle();    // Ð½Ð°ÐºÐ¾Ð¿Ð»ÐµÐ½Ð¸Ðµ Ð² Ð±ÑƒÑ„ÐµÑ€Ðµ: ÑÐ»ÑƒÑˆÐ°Ñ‚ÑŒ Ð¿Ð¾ÐºÐ° Ð½ÐµÑ‡ÐµÐ³Ð¾
      else if (s_barge_on && s_listening && barge.step(pcm, n)) {
        audio_barge_in(barge);
      }
      continue;
    }
    barge.idle();
    if (!s_listening) continue;
    if (xStreamBufferSend(s_mic_sb, pcm, n * sizeof(int16_t), 0) != n * sizeof(int16_t)) {
      if (dropped++ % 50 == 0) ESP_LOGW(TAG, "Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½: ÑÐµÑ‚ÑŒ Ð½Ðµ ÑƒÑÐ¿ÐµÐ²Ð°ÐµÑ‚, Ñ‚ÐµÑ€ÑÑŽ ÐºÐ°Ð´Ñ€Ñ‹ (%lu)", (unsigned long)dropped);
    }
  }
}

// ÐŸÐµÑ€ÐµÐ±Ð¸Ð»Ð¸: Ð·Ð°Ð¼Ð¾Ð»Ñ‡Ð°Ñ‚ÑŒ ÑÑ€Ð°Ð·Ñƒ, Ð½Ðµ Ð´Ð¾Ð¶Ð¸Ð´Ð°ÑÑÑŒ ÑÐµÑ€Ð²ÐµÑ€Ð°, Ð¾Ñ‚Ð´Ð°Ñ‚ÑŒ ÐµÐ¼Ñƒ Ð½Ð°Ñ‡Ð°Ð»Ð¾ Ñ„Ñ€Ð°Ð·Ñ‹ Ð¸ ÑÐ¾Ð¾Ð±Ñ‰Ð¸Ñ‚ÑŒ.
void audio_barge_in(Barge &b) {
  ESP_LOGI(TAG, "Ð¿ÐµÑ€ÐµÐ±Ð¸Ð»Ð¸ â€” Ð·Ð°Ð¼Ð¾Ð»ÐºÐ°ÑŽ");
  s_accept = false;
  s_stop = true;
  if (s_barge) s_barge();      // ÑÐ¾Ð¾Ð±Ñ‰ÐµÐ½Ð¸Ðµ Ñ€Ð°Ð½ÑŒÑˆÐµ ÐºÐ°Ð´Ñ€Ð¾Ð²: ÑÐµÑ€Ð²ÐµÑ€ ÑƒÐ¶Ðµ Ð¶Ð´Ñ‘Ñ‚ Ð·Ð²ÑƒÐº, ÐºÐ¾Ð³Ð´Ð° Ð¾Ð½Ð¸ Ð¿Ñ€Ð¸Ð´ÑƒÑ‚
  for (int i = 0; i < Barge::RING; ++i) {
    const int16_t *frame = b.ring[(b.head + i) % Barge::RING];   // ÑÑ‚Ð°Ñ€Ñ‹Ðµ ÐºÐ°Ð´Ñ€Ñ‹ Ð¿ÐµÑ€Ð²Ñ‹Ð¼Ð¸
    xStreamBufferSend(s_mic_sb, frame, MIC_FRAME * sizeof(int16_t), 0);
  }
  b.idle();
}

// Ð–Ð´Ñ‘Ñ‚ Ð¿Ð°Ñ‡ÐºÑƒ Ð² 100 Ð¼Ñ, Ð½Ð¾ Ð·Ð°Ð±Ð¸Ñ€Ð°ÐµÑ‚ Ð²ÑÑ‘ Ð½Ð°ÐºÐ¾Ð¿Ð»ÐµÐ½Ð½Ð¾Ðµ (Ð´Ð¾ 200 Ð¼Ñ) Ð¾Ð´Ð½Ð¸Ð¼ ÐºÑƒÑÐºÐ¾Ð¼: Ð¿Ð¾ÐºÐ°
// WebSocket Ð·Ð°Ð½ÑÑ‚ Ð¿Ñ€Ð¸Ñ‘Ð¼Ð¾Ð¼ Ð·Ð²ÑƒÐºÐ°, ÐºÐ°Ð´Ñ€Ñ‹ ÐºÐ¾Ð¿ÑÑ‚ÑÑ Ð¸ Ð¿Ð¾Ñ‚Ð¾Ð¼ ÑƒÑ…Ð¾Ð´ÑÑ‚ Ñ€Ð°Ð·Ð¾Ð¼, Ð° Ð½Ðµ Ñ‚ÐµÑ€ÑÑŽÑ‚ÑÑ.
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

// â”€â”€ Ð´Ð¸Ð½Ð°Ð¼Ð¸Ðº â”€â”€

void init_speaker() {
  i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
  cc.dma_desc_num = SPK_DMA_DESC;
  cc.dma_frame_num = SPK_DMA_FRAMES;
  cc.auto_clear = true;  // Ð´Ð°Ð½Ð½Ñ‹Ðµ ÐºÐ¾Ð½Ñ‡Ð¸Ð»Ð¸ÑÑŒ â€” Ð² Ð´Ð¸Ð½Ð°Ð¼Ð¸Ðº Ð¸Ð´Ñ‘Ñ‚ Ñ‚Ð¸ÑˆÐ¸Ð½Ð°, Ð° Ð½Ðµ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€ Ð±ÑƒÑ„ÐµÑ€Ð°
  ESP_ERROR_CHECK(i2s_new_channel(&cc, &s_tx, nullptr));
  // Ð¼Ð¾Ð½Ð¾: Ð½Ð° S3 Ð¾Ð´Ð¸Ð½ Ð¸ Ñ‚Ð¾Ñ‚ Ð¶Ðµ ÑÑÐ¼Ð¿Ð» ÑƒÑ…Ð¾Ð´Ð¸Ñ‚ Ð² Ð¾Ð±Ð° ÑÐ»Ð¾Ñ‚Ð°, MAX98357A Ð±ÐµÑ€Ñ‘Ñ‚ (L+R)/2
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
  // ÐºÐ°Ð½Ð°Ð» Ð²ÐºÐ»ÑŽÑ‡Ð°ÐµÐ¼ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð½Ð° Ð²Ñ€ÐµÐ¼Ñ Ð·Ð²ÑƒÐºÐ°: Ð±ÐµÐ· Ñ‚Ð°ÐºÑ‚Ð¾Ð² ÑƒÑÐ¸Ð»Ð¸Ñ‚ÐµÐ»ÑŒ Ð¼Ð¾Ð»Ñ‡Ð¸Ñ‚ Ð¸ Ð½Ðµ ÑˆÐ¸Ð¿Ð¸Ñ‚
}

// ÐœÑÐ³ÐºÐ¸Ð¹ Ð¾Ð³Ñ€Ð°Ð½Ð¸Ñ‡Ð¸Ñ‚ÐµÐ»ÑŒ: Ð´Ð¾ LIMIT_KNEE Ð·Ð²ÑƒÐº ÐºÐ°Ðº ÐµÑÑ‚ÑŒ, Ð²Ñ‹ÑˆÐµ â€” Ð¿Ð»Ð°Ð²Ð½Ð¾ Ð¿Ñ€Ð¸Ð¶Ð¸Ð¼Ð°ÐµÑ‚ÑÑ Ðº Ð¿Ð¾Ñ‚Ð¾Ð»ÐºÑƒ,
// Ð° Ð½Ðµ ÑÑ€ÐµÐ·Ð°ÐµÑ‚ÑÑ ÑÑ‚ÑƒÐ¿ÐµÐ½ÑŒÐºÐ¾Ð¹ (ÑÑ‚ÑƒÐ¿ÐµÐ½ÑŒÐºÐ° Ð¸ ÐµÑÑ‚ÑŒ Ñ‚Ñ€ÐµÑÐº).
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
    ESP_LOGW(TAG, "Ñ‡Ð°ÑÑ‚Ð¾Ñ‚Ð° %d Ð“Ñ† Ð½Ðµ Ð²ÑÑ‚Ð°Ð»Ð°, Ð¸Ð³Ñ€Ð°ÑŽ Ð½Ð° %d", rate, s_rate);
  }
}

void play_once() {
  s_speaking = true;
  set_rate(s_rate_req);
  // ÐºÐ¾Ð¿Ð¸Ð¼ 400 Ð¼Ñ: Ñ‡ÐµÑ€ÐµÐ· Ñ€ÐµÑ‚Ñ€Ð°Ð½ÑÐ»ÑÑ‚Ð¾Ñ€ Ð·Ð°Ð´ÐµÑ€Ð¶ÐºÐ° ÑÐµÑ‚Ð¸ ÑÐºÐ°Ñ‡ÐµÑ‚ Ð´Ð¾ 300 Ð¼Ñ
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
      const float vol = s_vol_q15 / 32768.0f / 32768.0f;  // Ð¸ Ð² ÑˆÐºÐ°Ð»Ñƒ âˆ’1â€¦1
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
      ++underruns;  // ÑÐµÑ‚ÑŒ Ð½Ðµ ÑƒÑÐ¿ÐµÐ»Ð° â€” Ð² Ð´Ð¸Ð½Ð°Ð¼Ð¸Ðº Ð¸Ð´Ñ‘Ñ‚ Ñ‚Ð¸ÑˆÐ¸Ð½Ð°
    }
  }
  if (!s_stop) {
    // Ð´Ð¾Ð¸Ð³Ñ€Ð°Ñ‚ÑŒ Ñ‚Ð¾, Ñ‡Ñ‚Ð¾ ÑƒÐ¶Ðµ Ð»ÐµÐ¶Ð¸Ñ‚ Ð² DMA: Ð´Ð¾Ð¿Ð¸ÑÐ°Ñ‚ÑŒ Ñ‚Ð¸ÑˆÐ¸Ð½Ñƒ Ð´Ð»Ð¸Ð½Ð¾Ð¹ Ð²Ð¾ Ð²ÐµÑÑŒ DMA-Ð±ÑƒÑ„ÐµÑ€
    static int16_t zeros[SPK_DMA_FRAMES] = {};
    size_t written = 0;
    for (int i = 0; i <= SPK_DMA_DESC; ++i) i2s_channel_write(s_tx, zeros, sizeof zeros, &written, portMAX_DELAY);
  }
  i2s_channel_disable(s_tx);
  s_barge_armed = false;
  s_ref_rms = 0;
  s_speaking = false;
  if (s_stop) xStreamBufferReset(s_spk_sb);  // Ð¿Ð¸ÑÐ°Ñ‚ÐµÐ»ÑŒ ÑƒÐ¶Ðµ Ð½Ðµ Ð¿Ð¸ÑˆÐµÑ‚ (s_accept ÑÐ½ÑÑ‚)
  const uint32_t overflow = s_overflow.exchange(0);
  ESP_LOGI(TAG, "Ð´Ð¾Ð¸Ð³Ñ€Ð°Ð» %.1f Ñ%s, Ð¿Ñ€Ð¾Ð¿ÑƒÑÐºÐ¾Ð² %lu, Ð½Ðµ Ð²Ð»ÐµÐ·Ð»Ð¾ %lu Ð±Ð°Ð¹Ñ‚", samples_total / (float)s_rate,
           s_stop ? " (Ð¾ÑÑ‚Ð°Ð½Ð¾Ð²Ð»ÐµÐ½)" : "", (unsigned long)underruns, (unsigned long)overflow);
}

// Ð’ÑÑ‚Ñ€Ð¾ÐµÐ½Ð½Ñ‹Ð¹ ÑÐ¸Ð³Ð½Ð°Ð»: Ñ‚Ñ€Ð¸ Ð³ÑƒÐ´ÐºÐ° Ð¿Ð¾ 150 Ð¼Ñ Ð¸ Ð¿Ð°ÑƒÐ·Ð°, Ñ†Ð¸ÐºÐ» 2 Ñ, Ñ‚Ð¾Ð½ Ñ‡ÐµÑ€ÐµÐ´ÑƒÐµÑ‚ÑÑ 880/1175 Ð“Ñ†. Ð Ð¸ÑÑƒÐµÐ¼
// ÑÑÐ¼Ð¿Ð»Ñ‹ Ð¿Ñ€ÑÐ¼Ð¾ Ð² Ð±ÑƒÑ„ÐµÑ€ Ð¿Ñ€Ð¾Ð¸Ð³Ñ€Ñ‹Ð²Ð°Ñ‚ÐµÐ»Ñ Ð¸ Ð¶Ð´Ñ‘Ð¼, ÐºÐ¾Ð³Ð´Ð° Ð² Ð½Ñ‘Ð¼ Ð¿Ð¾ÑÐ²Ð¸Ñ‚ÑÑ Ð¼ÐµÑÑ‚Ð¾: Ñ‚ÐµÐ¼Ð¿ Ð·Ð°Ð´Ð°Ñ‘Ñ‚ ÑÐ°Ð¼ Ð´Ð¸Ð½Ð°Ð¼Ð¸Ðº.
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
        if (slot < 3 && in_slot < 150) env = std::min({in_slot, 150 - in_slot, 8}) / 8.0f;   // Ð¿Ð»Ð°Ð²Ð½Ñ‹Ðµ ÐºÑ€Ð°Ñ
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
    for (int i = 0; s_speaking && !s_alarm_stop && i < 800; ++i) vTaskDelay(pdMS_TO_TICKS(10));   // Ð´Ð¾Ð¸Ð³Ñ€Ð°Ñ‚ÑŒ Ð±ÑƒÑ„ÐµÑ€
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
    ESP_LOGE(TAG, "Ð½ÐµÑ‚ Ð¿Ð°Ð¼ÑÑ‚Ð¸ Ð¿Ð¾Ð´ Ð·Ð²ÑƒÐºÐ¾Ð²Ñ‹Ðµ Ð±ÑƒÑ„ÐµÑ€Ñ‹ â€” Ð·Ð²ÑƒÐºÐ° Ð½Ðµ Ð±ÑƒÐ´ÐµÑ‚");
    return;
  }
  init_mic();
  init_speaker();
  // ÑÐ´Ñ€Ð¾ 0, Ð²Ñ‹ÑˆÐµ Ð¾Ñ‚Ñ€Ð¸ÑÐ¾Ð²ÐºÐ¸: Ð·Ð²ÑƒÐº Ð²ÑÐµÐ³Ð´Ð° Ð²Ñ‹Ñ‚ÐµÑÐ½Ð¸Ñ‚ Ð¿Ð¾Ð¼Ð¾Ñ‰Ð½Ð¸ÐºÐ° Ð»Ð¸Ñ†Ð°
  xTaskCreatePinnedToCore(mic_task, "mic", 4096, nullptr, 8, &s_mic, 0);
  xTaskCreatePinnedToCore(alarm_task, "alarm", 3072, nullptr, 4, &s_alarm_task, 0);
  xTaskCreatePinnedToCore(mic_send_task, "mic_send", 4096, nullptr, 6, nullptr, 0);
  xTaskCreatePinnedToCore(player_task, "player", 3072, nullptr, 8, &s_player, 0);
  ESP_LOGI(TAG, "Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½ Ð¸ Ð´Ð¸Ð½Ð°Ð¼Ð¸Ðº Ð³Ð¾Ñ‚Ð¾Ð²Ñ‹");
}

void listen(bool on, int gain_db) {
  if (!s_mic) return;
  gain_db = std::clamp(gain_db, 0, 30);
  s_gain_q8 = static_cast<int>(std::lround(256.0 * std::pow(10.0, gain_db / 20.0)));
  if (on && !s_listening) ESP_LOGI(TAG, "ÑÐ»ÑƒÑˆÐ°ÑŽ (ÑƒÑÐ¸Ð»ÐµÐ½Ð¸Ðµ %d Ð´Ð‘)", gain_db);
  if (!on && s_listening) ESP_LOGI(TAG, "Ð¼Ð¸ÐºÑ€Ð¾Ñ„Ð¾Ð½ Ð²Ñ‹ÐºÐ»ÑŽÑ‡ÐµÐ½");
  s_listening = on;
}

bool mic_pause() { return s_listening.exchange(false); }

void mic_resume(bool was_listening) {
  if (was_listening) s_listening = true;
}

void set_barge_in(bool on, float ratio) {
  s_barge_ratio_x100 = static_cast<int>(std::clamp(ratio, 1.2f, 20.0f) * 100.0f);
  if (on != s_barge_on) ESP_LOGI(TAG, "Ð¿ÐµÑ€ÐµÐ±Ð¸Ð²Ð°Ð½Ð¸Ðµ %s (Ð¿Ð¾Ñ€Ð¾Ð³ Ã—%.1f)", on ? "Ð²ÐºÐ»ÑŽÑ‡ÐµÐ½Ð¾" : "Ð²Ñ‹ÐºÐ»ÑŽÑ‡ÐµÐ½Ð¾", ratio);
  s_barge_on = on;
}

void set_clap_handler(ClapFn on_clap) { s_clap = on_clap; }

void set_clap(bool on, int sensitivity) {
  s_clap_sens = std::clamp(sensitivity, 1, 10);
  if (on != s_clap_on) ESP_LOGI(TAG, "Ñ…Ð»Ð¾Ð¿ÐºÐ¸ %s (Ñ‡ÑƒÐ²ÑÑ‚Ð²Ð¸Ñ‚ÐµÐ»ÑŒÐ½Ð¾ÑÑ‚ÑŒ %d)", on ? "Ð²ÐºÐ»ÑŽÑ‡ÐµÐ½Ñ‹" : "Ð²Ñ‹ÐºÐ»ÑŽÑ‡ÐµÐ½Ñ‹", s_clap_sens.load());
  s_clap_on = on;
}

void alarm(int seconds) {
  if (!s_alarm_task) return;
  xTaskNotify(s_alarm_task, static_cast<uint32_t>(std::clamp(seconds, 1, 120)), eSetValueWithOverwrite);
}

void alarm_stop() {
  if (!s_alarm_on) return;      // Ð³Ð¾Ð²Ð¾Ñ€Ð¸Ñ‚ ÑÐµÑ€Ð²ÐµÑ€, Ð° Ð½Ðµ Ð±ÑƒÐ´Ð¸Ð»ÑŒÐ½Ð¸Ðº â€” Ð½Ðµ Ñ‚Ñ€Ð¾Ð³Ð°ÐµÐ¼
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

// ÐšÐ²Ð°Ð´Ñ€Ð°Ñ‚Ð¸Ñ‡Ð½Ð°Ñ ÑˆÐºÐ°Ð»Ð° Ñ Ð¿Ð¾Ñ‚Ð¾Ð»ÐºÐ¾Ð¼: Ð²Ñ‹ÑˆÐµ ~âˆ’9 Ð´Ð‘ Ð¿Ð¾Ð»Ð½Ð¾Ð¹ ÑˆÐºÐ°Ð»Ñ‹ Ð´Ð¸Ð½Ð°Ð¼Ð¸Ðº Ð¸ Ð¿Ð¸Ñ‚Ð°Ð½Ð¸Ðµ ÑƒÑÐ¸Ð»Ð¸Ñ‚ÐµÐ»Ñ Ð¾Ñ‚ USB
// Ð½Ð°Ñ‡Ð¸Ð½Ð°ÑŽÑ‚ Ñ…Ñ€Ð¸Ð¿ÐµÑ‚ÑŒ, Ð¿Ð¾ÑÑ‚Ð¾Ð¼Ñƒ 100 % â€” ÑÑ‚Ð¾ Ã—0.36, Ð° 50 % (Ã—0.09) â€” Ð³Ñ€Ð¾Ð¼ÐºÐ¾ Ð¸ ÐµÑ‰Ñ‘ Ñ‡Ð¸ÑÑ‚Ð¾.
bool playing() { return s_speaking.load() || s_alarm_on.load(); }
float mic_level() { return s_level.load(std::memory_order_relaxed); }
uint32_t bang_count() { return s_bangs.load(); }
uint32_t beat_count() { return s_beats.load(); }

void set_volume(int level) {
  const float v = std::clamp(level, 0, 100) / 100.0f;
  s_vol_q15 = static_cast<int>(v * v * MAX_GAIN * 32768.0f);
}

}  // namespace audio
