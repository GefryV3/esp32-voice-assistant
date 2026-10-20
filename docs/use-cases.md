=== File: use-cases.md ===
# Use Cases

The smart speaker sits in my bedroom — by the bed and at the computer. There is family at home,
my room is my own. Phone — OnePlus 13s (Android), with the Home
Assistant app and MacroDroid; headphones — Nothing Ear (a). In Home Assistant — only my bedroom:
lights "Bedroom" (group), "Bar", "Desk backlight", "Cabinet backlight" (by segments),
"Lamp", "Bedside lamp".

**Main principle:** the speaker only does what is inconvenient to do on the phone: when it
is charging or lying aside, when hands and eyes are busy at the computer, when you need
information without a single action. Connection to the phone — via its companion bridge app (Section 3).

Priority:
- 🟢 **base** — this is why the speaker is needed, included in the first version;
- 🟡 **later** — useful, but after base;
- ⚪ **idea** — discuss, maybe skip.

---

## 1. Sleep and Morning — Bedside

- **1.1 Sunrise** — already exists: 20 minutes before the alarm, all the room lights smoothly
  fade up (Home Assistant automation). The speaker does not touch them.
- **1.2 "Actually woke up"** 🟢 — the alarm rings on the phone as usual. If I turned it off
  but never unlocked the phone (fell back asleep), a few minutes later the speaker wakes me up —
  by voice and rather snarkily.
- **1.3 "How much will I sleep?"** 🟢 — «Если уснёшь сейчас — 6 ч 40 мин до будильника» ("If you fall asleep now — 6h 40m until the alarm").
- **1.4 Night, while phone is charging** 🟢 — the screen almost dims out, the speaker is silent and shows
  only what matters.
- **1.5 Time at night** 🟢 — dim warm clock, visible without picking up a bright phone.
- **1.6 Sleeping with a sarcastic remark** 🟢 — at 1 AM I am on the phone, and the alarm is at 7: «Подъём через
  6 часов. Просто к сведению» ("Wake-up in 6 hours. Just for your information"). In the morning: «Лёг в 02:13, спал 5 ч 40 мин» ("Went to bed at 02:13, slept 5h 40m").
- **1.7 Bedtime check** 🟢 — with a single phrase or automatically at a set time: alarm at
  7:30, phone on charger (40%), rain tomorrow, first task at 10.
- **1.8 Morning without getting up** 🟢 — weather, first task, when to leave, next tram
  (Wiener Linien open data).
- **1.9 Sleep sounds** 🟡 — rain, noise, with a sleep timer.

## 2. At the Computer

- **2.1 Focus timer** 🟢 — a ring around the screen edge counts down 25/50 minutes, then a break.
- **2.2 Thought on the fly** 🟢 — «Запомни: …» ("Remember: ...") — note straight to the phone.
- **2.3 Phone ↔ Computer** 🟢 — "Share → Open on computer" on the phone —
  link opens in the PC browser; "send to phone" — back.
- **2.4 Claude Code on the speaker** 🟡 — a long task is running, walked away — screen shows "finished" or
  "waiting for input".
- **2.5 Meeting soon** 🟡 — 5 minutes before a calendar event — on screen.

Everything about the computer is handled by a small program on it: whether I am sitting (touched mouse and keyboard),
laptop battery, open link.

## 3. Phone (Custom Android Bridge App)

The app maintains a connection with the server while I am at home and can do things that the Home
Assistant app cannot. Installed via APK file.

- **3.1 Notifications when I am at the computer** 🟢 — if the phone is not in hand (screen is off)
  and I am at the computer, notifications are mirrored to the speaker: app, sender, a couple of lines,
  disappear in ~8 seconds. Noisy apps — in exclusions, at night — only important ones.
  Also comes through when Mom texts.
- **3.2 Voice reply** 🟢 — «ответь: буду через 10 минут» ("reply: will be there in 10 minutes") — the reply goes to the same chat
  (Telegram, WhatsApp — anywhere a notification has a "Reply" button).
- **3.3 Incoming call on screen** 🟢 — «Звонит мама» ("Mom is calling") prominently; «сбрось» ("dismiss") — decline, «перезвоню» ("call back") —
  decline and send SMS.
- **3.4 Mini player** 🟢 — what is playing on the phone: title and progress ring along the edge;
  «дальше» ("next"), «пауза» ("pause") by voice.
- **3.5 "Where is my phone?"** 🟢 — phone rings at full volume even in silent mode.
- **3.6 Batteries** 🟢 — Nothing Ear (a) headphones (battery level known by phone if connected to it,
  or Windows if to PC), phone, laptop — reminds me while I am near a charger.
- **3.7 "Send this to me"** 🟢 — speaker sends reply, link, address to the phone.
- **3.8 Speaker settings** 🟢 — in the app, Apple-style: when to fall asleep, night, brightness,
  which notifications to mirror, how snarky the character is, speaker status.
- **3.9 Telegram — the same conversationalist outside home** 🟡 — same memory, same reminders.

## 4. Conversation, Memory, Reminders

- **4.1 Ask anything** 🟢 — short, by voice; long — "send it to me" (3.7).
- **4.2 Clarify without wake word** 🟢 — speaker listens for ~5 more seconds.
- **4.3 Interrupt** 🟡 — «стоп» ("stop") during a response.
- **4.4 Memory** 🟢 — «запомни» ("remember"), «что ты обо мне знаешь» ("what do you know about me"), «забудь» ("forget").
- **4.5 Reminders** 🟢 — by time and by event: «когда приду домой — напомни…» ("when I get home — remind me...").
- **4.6 Character** 🟢 — restrained, with snarky remarks; not "Jarvis".

## 5. Room Lighting (via Home Assistant)

- **5.1 Bedside lighting** 🟢 — «выключи всё» ("turn off everything"), «ночник» ("nightlight" — only "Bedside lamp"),
  «свет на стол» ("desk light").
- **5.2 Scenes** 🟡 — «я сплю» ("I am sleeping" — everything turns off, speaker goes to night mode), «работаю» ("working"), «кино» ("movie").
- **5.3 Room** ⚪ — temperature, open window before rain — if sensors are added.

## 6. Bedroom Screen

- **6.1 Real backlight** 🟢 — controllable backlight (BL output) or complete turn-off at night.
- **6.2 Night palette** 🟢 — warm, minimum light, no motion.
- **6.3 Face** 🟢 — already done: eyes with clock, falling asleep, talking, timer, expressions
  (mockup [design/screen-mockup.html](../design/screen-mockup.html)). Polishing "premiumness".
- **6.4 Room buddies** ⚪ — mini-games on screen.

---

## What Was Dropped

- Kitchen, news, mail, shopping lists, long conversations about tasks — the phone is faster.
- Gaming mode at the computer — not needed.
- Bot for parents and their status — they won't use it; their messages and calls will
  reach me anyway through notification mirroring (3.1, 3.3).

## Open Questions

1. Screen backlight output (BL / BLK / LED) — does it exist and where is it connected? (will check later)
2. Which apps should not be mirrored to the speaker?
