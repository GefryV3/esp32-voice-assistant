if(NOT EXISTS ${CMAKE_CURRENT_LIST_DIR}/secrets.h)
    message(FATAL_ERROR "Нет firmware/main/secrets.h: скопируй secrets.h.example в secrets.h и заполни.")
endif()

idf_component_register(
    SRCS
        "main.cpp"
        "display/lcd.cpp"
        "face/anim.cpp"
        "face/fonts_data.cpp"
        "face/render.cpp"
        "face/text.cpp"
        "ui/ui.cpp"
        "net/wifi.cpp"
        "net/uplink.cpp"
        "net/ota.cpp"
        "audio/audio.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        app_update
        esp_driver_gpio
        esp_driver_i2s
        esp_http_client
        esp_https_ota
        esp_driver_spi
        esp_event
        esp_lcd
        esp_netif
        esp_timer
        esp_wifi
        json
        nvs_flash
)

# Отрисовка лица считает много плавающей арифметики на каждый пиксель: без errno и с
# быстрыми делением/корнем она в разы быстрее. NaN и бесконечности там не используются.
set_source_files_properties(face/render.cpp face/anim.cpp
    PROPERTIES COMPILE_OPTIONS "-O3;-ffast-math")
