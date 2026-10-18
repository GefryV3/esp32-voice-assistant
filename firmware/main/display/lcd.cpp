#include "display/lcd.h"

#include "board.h"
#include "driver/spi_master.h"
#include "esp_lcd_gc9a01.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

namespace lcd {
namespace {

const char *TAG = "lcd";
constexpr spi_host_device_t HOST = SPI2_HOST;

esp_lcd_panel_handle_t s_panel = nullptr;
SemaphoreHandle_t s_done = nullptr;  // +1 на каждую законченную передачу цвета
int s_pending = 0;                   // отправлено, но ещё не подтверждено (трогает одна задача)

bool IRAM_ATTR on_color_done(esp_lcd_panel_io_handle_t, esp_lcd_panel_io_event_data_t *, void *) {
  BaseType_t woken = pdFALSE;
  xSemaphoreGiveFromISR(s_done, &woken);
  return woken == pdTRUE;
}

}  // namespace

bool init() {
  s_done = xSemaphoreCreateCounting(16, 0);

  spi_bus_config_t bus = {};
  bus.mosi_io_num = board::LCD_MOSI;
  bus.miso_io_num = -1;
  bus.sclk_io_num = board::LCD_SCLK;
  bus.quadwp_io_num = -1;
  bus.quadhd_io_num = -1;
  bus.max_transfer_sz = board::LCD_W * board::LCD_H * 2;
  ESP_ERROR_CHECK(spi_bus_initialize(HOST, &bus, SPI_DMA_CH_AUTO));

  esp_lcd_panel_io_spi_config_t io_cfg = {};
  io_cfg.cs_gpio_num = board::LCD_CS;
  io_cfg.dc_gpio_num = board::LCD_DC;
  io_cfg.spi_mode = 0;
  io_cfg.pclk_hz = board::LCD_SPI_HZ;
  io_cfg.trans_queue_depth = 4;
  io_cfg.on_color_trans_done = on_color_done;
  io_cfg.lcd_cmd_bits = 8;
  io_cfg.lcd_param_bits = 8;
  esp_lcd_panel_io_handle_t io = nullptr;
  ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)HOST, &io_cfg, &io));

  esp_lcd_panel_dev_config_t panel_cfg = {};
  panel_cfg.reset_gpio_num = board::LCD_RST;
  panel_cfg.rgb_ele_order = board::LCD_BGR ? LCD_RGB_ELEMENT_ORDER_BGR : LCD_RGB_ELEMENT_ORDER_RGB;
  panel_cfg.bits_per_pixel = 16;
  ESP_ERROR_CHECK(esp_lcd_new_panel_gc9a01(io, &panel_cfg, &s_panel));

  ESP_ERROR_CHECK(esp_lcd_panel_reset(s_panel));
  ESP_ERROR_CHECK(esp_lcd_panel_init(s_panel));
  ESP_ERROR_CHECK(esp_lcd_panel_invert_color(s_panel, board::LCD_INVERT));
  ESP_ERROR_CHECK(esp_lcd_panel_mirror(s_panel, board::LCD_MIRROR_X, board::LCD_MIRROR_Y));
  ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(s_panel, true));
  ESP_LOGI(TAG, "GC9A01 готов, SPI %d МГц", board::LCD_SPI_HZ / 1000000);
  return true;
}

void blit_async(int y0, int h, const uint8_t *rows) {
  if (!s_panel || h <= 0) return;
  wait(8);  // не переполнить счётчик
  esp_lcd_panel_draw_bitmap(s_panel, 0, y0, board::LCD_W, y0 + h, rows);
  s_pending++;
}

void wait(int max_pending) {
  while (s_pending > max_pending) {
    xSemaphoreTake(s_done, portMAX_DELAY);
    s_pending--;
  }
}

int pending() {
  while (s_pending > 0 && xSemaphoreTake(s_done, 0) == pdTRUE) s_pending--;
  return s_pending;
}

void blit(int y0, int h, const uint8_t *rows) {
  blit_async(y0, h, rows);
  wait(0);
}

}  // namespace lcd
