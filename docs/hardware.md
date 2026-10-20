# Hardware

Physically, everything is identical to the old project; nothing is resoldered.

| Component | Model | Connection |
|---|---|---|
| Board | Lolin S3 Mini (ESP32-S3FH4R2) | 4 MB flash, 2 MB quad-PSRAM, USB via built-in USB-Serial-JTAG |
| Screen | GC9A01, round IPS 240×240 | SPI: CLK=12, MOSI=13, CS=16, DC=15, RST=14 (no MISO) |
| Microphone | INMP441 | I2S input: SCK=9, WS=10, SD=11, L/R→GND (left channel) |
| Amplifier | MAX98357A + speaker | I2S output: BCLK=4, LRC=44, DIN=5 (GAIN/SD unconnected) |

Pins are duplicated in the code: [firmware/main/board.h](../firmware/main/board.h).

## What is Important to Remember

- **GPIO44 is UART0 RX.** It is used for the speaker LRC, so console/logs go
  exclusively via USB-Serial-JTAG (`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`); UART0 is left untouched.
- **Flash is only 4 MB.** Partitioning: two 1.75 MB application slots (for over-the-air
  updates) + NVS. It matches the old ESPHome partition scheme byte for byte — therefore, the new
  firmware can be flashed over ESPHome via OTA, and vice versa.
- **PSRAM 2 MB, quad, 80 MHz.** Slower than internal memory: the screen frame buffer is kept in
  internal DMA memory, while PSRAM holds the background and large audio buffers.
- **The screen handles 80 MHz SPI** over short wires. A full frame of 240×240×16 bits takes ≈ 14 ms —
  this is the bus limit; therefore, the face engine sends only the changed band.
- **Wi-Fi is audible in the microphone.** Each packet causes a current spike, INMP441 supply voltage sags,
  producing a click in the audio. In software: microphone packets of 100 ms and a high-pass filter from 100 Hz. In hardware
  (if it needs to be quieter): 10 µF + 100 nF directly across the VDD–GND pins of the microphone.
- **Two independent I2S interfaces** (microphone and speaker on different peripheral blocks) — recording and
  playback can run simultaneously.
