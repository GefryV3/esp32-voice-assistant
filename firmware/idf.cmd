@echo off
rem Запуск idf.ps1 в обход запрета скриптов PowerShell (только для этого вызова,
rem системная настройка не меняется). Пример: idf build   idf -p COM3 flash monitor
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0idf.ps1" %*
