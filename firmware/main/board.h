#pragma once
// Железо колонки: Lolin S3 Mini + GC9A01 + INMP441 + MAX98357A. Описание — docs/hardware.md.
#include "driver/gpio.h"

namespace board {

constexpr const char *DEVICE_NAME = "jarvis-hub";

// ── Экран GC9A01, круглый 240×240, SPI ──
constexpr gpio_num_t LCD_SCLK = GPIO_NUM_12;
constexpr gpio_num_t LCD_MOSI = GPIO_NUM_13;
constexpr gpio_num_t LCD_CS = GPIO_NUM_16;
constexpr gpio_num_t LCD_DC = GPIO_NUM_15;
constexpr gpio_num_t LCD_RST = GPIO_NUM_14;
constexpr int LCD_W = 240;
constexpr int LCD_H = 240;
constexpr int LCD_SPI_HZ = 80 * 1000 * 1000;  // на коротких проводах тянет; артефакты — вернуть 40 МГц
// Ориентация и цвет. Проверка — заставка при включении: сверху вниз красная, зелёная,
// синяя полосы, цифра «1» в левом верхнем углу. Если красный и синий поменялись местами —
// переключить LCD_BGR; если цифра зеркальная — LCD_MIRROR_X.
constexpr bool LCD_INVERT = true;
constexpr bool LCD_BGR = true;
constexpr bool LCD_MIRROR_X = true;
constexpr bool LCD_MIRROR_Y = false;

// ── Микрофон INMP441, I2S вход (L/R → GND, левый канал) ──
constexpr gpio_num_t MIC_SCK = GPIO_NUM_9;
constexpr gpio_num_t MIC_WS = GPIO_NUM_10;
constexpr gpio_num_t MIC_SD = GPIO_NUM_11;

// ── Усилитель MAX98357A, I2S выход ──
constexpr gpio_num_t SPK_BCLK = GPIO_NUM_4;
constexpr gpio_num_t SPK_LRC = GPIO_NUM_44;  // это UART0 RX — поэтому консоль только по USB
constexpr gpio_num_t SPK_DIN = GPIO_NUM_5;

}  // namespace board
