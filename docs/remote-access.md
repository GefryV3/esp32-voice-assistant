# Remote Access to Home (Tailscale)

Why: when the development laptop is not at home (school, etc.), Home Assistant, the server addon, and the speaker cannot be reached because they have home network addresses like 192.168.178.x. Tailscale creates a small private network on top of the internet: the laptop and HA can see each other no matter where they are. Nothing is exposed to the outside, and it is free for personal use.

## Once, at Home (~10 minutes)

**1. Home Assistant.** Settings → Add-ons → Add-on Store → find **Tailscale** → Install. On the "Info" tab: enable "Start on boot" and "Watchdog" → Start → **Open Web UI** → log in (Google, Microsoft, or GitHub — whichever is more convenient) → approve the device. On the `admin.tailscale.com` page, `homeassistant` will appear in the list with an address like `100.x.y.z`.

**2. Laptop.** Install Tailscale for Windows from tailscale.com/download → log in with the same account. Verification: `ping homeassistant` responds.

**3. Tell Claude.** Claude will change the line `JARVIS_SERVER=192.168.178.86` in `server/.env` to the Tailscale address (`homeassistant` or `100.x.y.z`) — `tools/send.py`, the PC app, and the addon deployment (`\\<JARVIS_SERVER>\addons\jarvis`) also work via it. The `addons` folder in Explorer will need to be opened once using the new address with the same Samba login.

The speaker and phone at home still connect to 192.168.178.86 — nothing changes for them.

## What Becomes Possible from Anywhere

- deploy the server (`python tools/deploy_addon.py`) — you will just need to click "Update" in HA, and the HA interface will also open at `http://homeassistant:8123`;
- update the speaker over the air: `python tools/send.py ota` (no cable needed, except for the initial 0.4.0 firmware — older versions do not support over-the-air updates);
- view logs and status: `send.py "devices; log 30; last"`, ask `ask …`.
