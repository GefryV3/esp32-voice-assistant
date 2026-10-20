# Security

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use
[private vulnerability reporting](https://github.com/GefryV3/esp32-voice-assistant/security/advisories/new)
instead; I will get back to you as soon as I can.

## How secrets are handled

- The server reads all keys and tokens from `server/.env`; the firmware takes Wi-Fi credentials, the server URL and
  the device token from `firmware/main/secrets.h`. Both files are git-ignored; commit only the `.example` versions.
- Every client (speaker, phone, PC agent) authenticates to the server with a shared device token in the WebSocket
  handshake.
- The Android build embeds the server address and token from `server/.env`, so **never publish a built APK**.
  The same applies to a firmware image built with real `secrets.h`.
- The server is designed for a home LAN (or a private overlay network such as Tailscale). Don't expose its port to
  the internet.
