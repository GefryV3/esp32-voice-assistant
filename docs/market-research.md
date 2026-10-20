# What Similar Devices Can Do and What Maga Lacks (2026-10-20)

Review: Amazon speakers (Alexa+), Yandex (Alisa), Google (Gemini for Home), desktop companions (Vector, EMO,
Loona, ElliQ), and open-source projects (Home Assistant Voice PE, Rhasspy, OVOS). Goal: find functions we
lack that fit your life (school, bedroom, computer, Home Assistant, Android, Telegram). Numbering continues
[ideas.md](ideas.md): ideas starting with **#38**.

Sources: [Alexa+ and personalities (TechCrunch)](https://techcrunch.com/2026/02/25/amazons-ai-powered-alexa-gets-new-personality-options/),
[Alexa+ review 2026](https://tech.yahoo.com/home/articles/4-reasons-why-alexa-actually-091700283.html),
[What's new in Google Home](https://support.google.com/googlehome/answer/15962877?hl=en),
[Google Home Speaker and Gemini](https://blog.google/products-and-platforms/devices/google-nest/google-home-speaker-gemini-features/),
[Alisa features 2026 (WinBug)](https://winbug.ru/skrytye-funktsii-alisy-2026/),
[Alisa blog](https://alice.yandex.ru/blog/2026/archived-devices),
[desktop robots 2026](https://the-gadgeteer.com/2026/05/17/emotional-ai-companion-robots/),
[Home Assistant Voice PE](https://thepihut.com/products/home-assistant-voice-preview-edition),
[open voice projects](https://www.stork.ai/alternatives/rhasspy).
Some items (whisper, security, Alexa "hunches") are described based on well-known functions of these products
rather than the sources above; we will cross-check with documentation before implementation.

## What Maga Already Has and Does Not Worse, Often Better

| Feature in Products | Maga |
|---|---|
| Continuing conversation without wake word (Alisa 20 s, Google Continued Conversation) | present, 6 s, 14 s in German |
| Interrupting the answer (barge-in) | present |
| Personalities "Brief / Chill / Sweet" (Alexa+) | sarcasm slider, time-of-day character, "too long" feedback |
| Memory (Google window is 15 minutes) | long-term memory, shared Telegram history, nightly reflection |
| Proactivity (ElliQ) | rare spontaneous utterances, questions about habits, briefing |
| Timers, alarms, reminders, notes, lists | present; alarm tied to school schedule |
| Find phone, calls, notifications, media player | present (phone bridge) |
| "Do not listen" mode (Alisa and Voice PE Mute button) | by voice («не слушай час» ("don't listen for an hour")), no hardware button |
| Local wake word | on server, custom «Мага» ("Maga") |
| Games, trainers (Alisa: «Легко сказать» ("Easy to say"), games) | German, «Города» ("Cities"), «20 вопросов» ("20 questions"), quiz |
| On-device emotions (EMO, Vector: hundreds of expressions) | eyes, reactions to sound and music |
| Smart home without cloud (Alisa: Zigbee, Matter) | home goes through local HA |

## What Maga Lacks (Candidates, #38 and Beyond)

Size: **S** evening, **M** day, **L** several days.

- **#38. Fast path for simple commands** (M). On Alexa and Alisa, «включи свет» ("turn on the light"), «таймер на 5 минут» ("timer for 5 minutes") execute instantly without a large model. In Maga, everything goes through Gemini (first audio response 0.6–0.8 s). Idea: a short list of frequent commands (room light, brightness, timer, «который час» ("what time is it"), «стоп» ("stop")) is recognized locally by Vosk on the server and executed immediately, with a short tone or screen label instead of a long answer. Benefit: almost instant, works even during Gemini issues, uses less quota.
- **#39. Recognizes you by voice** (L). Alexa, Google, and Alisa have voice profiles. For Maga, this solves your request to «работать, только когда я в комнате» ("work only when I'm in the room"): respond only to your voice, ignoring brothers, parents, and the TV. Option: a small voice fingerprint model on the server (ECAPA/Resemblyzer), recording 10 phrases for training, checking after the wake word. Risk: errors on a noisy microphone, calibration needed.
- **#40. Homework and exams from WebUntis** (M). Now that the schedule is read, from it (and from "Hausaufgaben" and "Prüfung") we can extract: «завтра контрольная по AM» ("tomorrow is the AM test"), «что задали» ("what was assigned"), an evening reminder about uncompleted tasks. Equivalent to Alisa's "homework help," but tied to your real schedule. Need to check if the school exposes Hausaufgaben and Prüfungen through this entry point.
- **#41. "Help me remember"** (M). Alisa has this skill: learns text in parts. For studying: dictate notes or a list of terms, Maga quizzes you in a loop (like with German words), repeats according to a schedule. You can reuse the table and repetition logic from German.
- **#42. Whisper response** (S). Alexa has a whisper mode: if you speak in a whisper, it answers in a whisper. Ideal at the bedside at night: the server detects a whisper by level and spectrum, enables night mode for this conversation (volume even lower, hint «шёпотом» ("in a whisper")). A small tweak to what has already been done for the night.
- **#43. Room security while you are away** (M). Alisa has a "guard mode", Alexa Guard, Vector "guard": listens for sounds and sends a notification. For you: when presence shows you are not in the room, but the speaker hears a voice or footsteps, a message and a short recording arrive in Telegram. All locally on the server. Requires your decision on privacy (there are other people at home).
- **#44. "Do not remember" mode** (S). Alisa's "Incognito mode". Maga temporarily stops writing history and learning from the conversation. Useful for chats you don't want stored. Just a flag for history and reflection.
- **#45. Automation "hunches"** (M). Alexa Hunches: notices patterns and suggests a routine. Maga sees your repeating actions (light at 23:30, alarm at 6:20) and suggests: «Ты каждый вечер гасишь свет в 23:30, сделать это само?» ("You turn off the light every evening at 23:30, should I do it automatically?"). Relies on the already existing learning module and HA.
- **#46. Real-time translator** (S). Google "Interpreter". Maga translates a phrase or conversation (Russian ↔ German), convenient for school and practice. Gemini Live can do this; we need a mode and a prompt.
- **#47. News on your topics** (S). Google's "Home Brief" and "News Brief". The morning briefing is already there; we add 3 short news items on topics you choose (tech, Austria, gadgets) via background search agents.
- **#48. Maga states on backlighting** (S). Alisa Station 3 has Ambilight, Voice PE has an LED ring. You have WLED lights: the "Stick" or desk can gently pulse when Maga is listening or thinking. Currently looks like decoration, do it only if you like it.
- **#49. Hardware Mute button** (S, hardware). Voice PE and Alisa have a physical microphone switch. We chose «не слушай» ("don't listen") by voice, but a button gives absolute confidence (especially when guests are over).
- **#50. Breaks based on actual computer activity** (S). ElliQ has proactive "stand up, stretch" prompts. You have data on computer work, so it can remind you to take a break and drink water after 2 hours straight. You declined Pomodoro and focus mode, so this is only if you want it; disabled by default.

## What Others Have Much of, But We Do Not Need

Music and multi-room, stores and shopping, calls to family, "Intercom", kids' fairy tales and parental control,
cameras and doorbells, smart plugs (you don't have any), SOS for the elderly, subscriptions (Alexa+ around $15–20
per month, Google Home Premium, Yandex Plus): Maga has no subscription, you pay only for Gemini usage.

## What Is Worth Doing First

1. **#38 fast path** and **#42 whisper**: cheap, felt immediately.
2. **#40 homework and exams** and **#41 "help me remember"**: most useful for school.
3. **#39 owner's voice**: solves «работать, только когда я» ("work only when it's me"), but complex, do after night home checks.
4. **#43 security** and **#45 hunches**: interesting, but require your decisions.
