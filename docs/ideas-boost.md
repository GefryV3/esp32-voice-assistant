# Ideas that Take Maga to the Next Level (2026-10-20)

A second, broader search: what cutting-edge voice assistants and companions are doing, what Gemini Live itself brings to the table, and what people actually want from smart speakers. Numbering continues from [market-research.md](market-research.md): ideas **#51** and onward. All items are tailored specifically for you: the HTL school, bedroom, computer, Android, Home Assistant, Telegram, and Claude Code.

Sources: [Gemini 3.8 Live (DataCamp)](https://www.datacamp.com/blog/gemini-3-8-live), [Live Models Comparison](https://micdrop.dev/blog/gpt-live-vs-gpt-realtime-vs-gemini-live), [Home Assistant and MCP](https://home-assistant.io/integrations/mcp), [AI Pendants and Daily Memory](https://the-gadgeteer.com/2026/05/05/best-ai-wearables-2026/), [Friend: Proactive Companion](https://vantaige.io/ai-tool/friend), [Sleep Monitoring via Aqara FP2 Radar](https://mightygadget.com/aqara-presence-sensor-fp2-sleep-monitoring/), [Voice Learning Recite](https://appgoblin.info/apps/6759579263), [Reachy Mini Desktop Robot](https://huggingface.co/blog/reachy-mini).

## What's New Regarding Our Own "Engine" (Gemini 3.8 Live)

- **Emotional dialogue removed from API.** I tested this with a live request: `enableAffectiveDialog` closes the connection with error 1007. We used to enable it by default. **Fixed**: disabled by default, and upon rejection, the server disables it permanently. We were wasting time attempting connection on every reset.
- **Asynchronous functions enabled by default.** While Maga is executing a tool (lights, search), he can keep talking ("one second, searching…") and voice the result when it's ready, at the right moment (`WHEN_IDLE`, `SILENT`, `INTERRUPTED`). This gives a "live" rhythm to long tasks without dead silence.
- **Extended thinking** (`low/medium/high`) for multi-step tasks. The base model struggles more with tool chains (30% in the τ-Voice test vs. 37.7% for its predecessor), so thinking should be enabled for "complex" tasks.
- **Understands 97 languages natively and switches on the fly**: a direct foundation for a translator and conversational practice.
- **Real-time video**: you can "show" something with your phone (expensive in tokens, sessions up to 2 minutes).
- **Price**: approximately $0.005 per minute of incoming speech and $0.018 for outgoing, ~1$ per hour of conversation. For us, this is pennies.

## Ideas

Size: **S** evening, **M** day, **L** several days. Coolness: ★ to ★★★.

### Top Priorities

- **#51. "Maga in your pocket"** ★★★ (L). Right now Maga only lives in the speaker and in Telegram text. But you spend most of your day at school and on the road. Voice mode in our "Speaker" app: you hold down a button or press on your Nothing Ear earbuds, speak, and it's the exact same Maga with the same memory, schedule, homework, and smart home (the stream goes to our server). On the bus: «Мага, что у меня завтра?» ("Maga, what do I have tomorrow?"), «добавь в идеи…» ("add to ideas..."), «включи дома свет» ("turn on the lights at home"). Changes the speaker's status from a device into your constant personal entity.
- **#52. Voice "approver" for Claude Code** ★★★ (M). You already get "Claude is waiting for permission" on the screen. Let's go further: Maga reads aloud what exactly Claude is asking permission for («Claude хочет удалить папку build, можно?» ("Claude wants to delete the build folder, is that okay?")), and you answer by voice "yes" or "no". The Claude Code hook (`PermissionRequest`/`PreToolUse`) waits for a server response with a timeout, defaulting to "ask on screen". When sitting at the computer with multiple sessions, this turns the speaker into a remote control for all development: «Мага, как там сессии?» ("Maga, how are the sessions?"), «разреши» ("allow"), «останови вторую» ("stop the second one").
- **#53. Custom "emotion hearing"** ★★★ (M). Gemini removed emotional dialogue, but we can make our own, and better. The server already hears you: by voice (pitch, tempo, energy, pauses; a small ONNX emotion recognition model on CPU can be used) we determine "tired / annoyed / alert / sad" and pass it to Maga as a string prompt: "user sounds tired." Maga gets softer and briefer when you're exhausted, and teases you when you're alert. Nothing leaves outside except the tag word.
- **#54. Context engine ("do not disturb when busy")** ★★★ (M). Right now, every feature has its own "not at night" and "don't listen when". Let's create a single `context()` service: online classes (Teams call, confirmed by a PC program), playing in fullscreen mode, phone call, on the bus (by schedule and geolocation), sleeping, guests (unknown voice), silent mode. All proactive modules (habits, briefing, guesses, spontaneous remarks, breaks, news) query it before speaking. A single feature makes Maga noticeably less intrusive and much more "empathetic."
- **#55. Sound events instead of eavesdropping** ★★★ (M). A secure alternative to "listening to the room": sound recognition rather than speech (a model like YAMNet on the server): **knock on the door, doorbell, fire alarm sensor, breaking glass, dog, neighbor's alarm clock**. While you're away, in Telegram: "knocked at 14:12, 3 times"; at home: «в дверь позвонили» ("someone rang the doorbell"). No speech, recording, or transcription: privacy is preserved (builds on #43). The fire alarm is genuinely useful for safety.

### School and Studies

- **#56. Educational "second brain"** ★★★ (L). You throw a whiteboard photo, PDF, or notes to the bot in Telegram. Maga stores and indexes them (Gemini File Search or custom search), then answers based on them: «что мы проходили по Antriebstechnik?» ("what did we cover in Antriebstechnik?"), recognizes text from photos and gives a review plan. Oral exam: Maga quizzes you on your materials, evaluates answers (like the Recite app) and handles spaced repetition (idea #41, but on real notes). For HTL, this is the most useful thing we can build.
- **#57. "Coming home from school"** ★★ (S). Expands on #27: when you're near the school, via phone geolocation Maga itself warns you «автобус домой через 7 минут, выходи» ("bus home in 7 minutes, head out") or «автобус отменён, следующий в 15:25» ("bus canceled, next one at 15:25"), factoring in walking time to the bus stop.
- **#58. School "observers"** ★★ (M). Generalize to "notify me when…" (see #59), including for school: lesson cancellation in the morning, a new substitute, homework for a subject, a new task in Teams.

### New Features

- **#59. "Notify me when" observers** ★★★ (M). Background tasks with an expiration deadline: "watch the price of X and tell me when it drops below 50", "notify me when update Y is released", "tell me if the bus is more than 5 minutes late", "check if results have been posted." Search via Tavily, scheduled checks, response by voice and in Telegram. They turn background agents into true "errands," similar to Alexa+ and ElliQ.
- **#60. Codeless voice macros** ★★ (S). «Когда скажу „погнали“, включи холодный свет на столе, запусти таймер на 50 минут и поставь тишину» ("When I say 'let's go', turn on the cool desk light, start a 50-minute timer, and set silence"). Programming by voice: Maga memorizes the macro and invokes it via the fast path (#38) almost instantly. Complements "guesses" (#45) and skills.
- **#61. Voice computer control** ★★ (M). Via a PC app and a whitelist: «заблокируй компьютер» ("lock the computer"), «убавь звук на компьютере» ("lower computer volume"), «открой Fusion» ("open Fusion"), «создай скриншот» ("take a screenshot"), «выйди из Teams» ("quit Teams"). Without full access: only what you explicitly allowed in the list.
- **#62. "Where things are" and search through your own life** ★★ (S). «Мага, я положил паспорт в синюю папку» ("Maga, I put my passport in the blue folder"): later «где паспорт?» ("where is my passport?"). Plus search through your past conversations and decisions: «что я решил насчёт велосипеда?» ("what did I decide about the bike?"), «когда я последний раз говорил про ремонт?» ("when did I last talk about repairs?") (full-text search and embeddings over general history; your conversations only).
- **#63. Smart sleep-phase alarm clock** ★★★ (M, phone or hardware). Wakes you up not at the exact 6:20, but at the lightest sleep moment within the 6:05–6:20 window (like Sleep as Android). Sensor: phone accelerometer on the bed (app already exists) or an LD2410 radar pointed at the bed (not the whole room, so it doesn't trigger on others). At the same time: sleep tracking records automatically, «спокойной ночи» ("good night") triggers when you actually lie down.
- **#64. Real-time translation via video** ★ (M). Pointed the phone camera at text or a menu, Maga translated it (video in Gemini 3.8 Live). Useful for German texts at school and traveling.

### Fun Extras

- **#65. Extended thinking "for the smart mode"** ★ (S). For complex queries (e.g., building a weekly schedule from homework, classes, and tasks), Maga switches to `thinking: medium` and briefly says «подумаю» ("let me think").
- **#66. Personas on demand** ★ (S). "Teacher / friend / coding partner mode" (like Alexa+ Brief, Chill, Sweet personalities, but our own full-fledged roles with different delivery styles); stored in prefs, switchable by voice.
- **#67. Custom voice** ★ (L). Clone or pick a unique signature voice (Gemini offers 30 ready-made ones, custom voices aren't available yet). Low priority.

## My Choice: What I Would Build First

1. **#54 Context engine**: cheap and improves everything we've already built.
2. **#51 "Maga in your pocket"**: changes daily usage the most while you're at school and on the go.
3. **#56 Educational second brain**: the most useful thing for school.
4. **#52 Voice Claude Code approver**: matches your working style, and almost everything for it is already built (hook, screen).
5. **#53 Custom emotion hearing** and **#55 Sound events**: that sought-after "liveliness" and safety without eavesdropping.
6. **#59 Observers** and **#63 Smart alarm clock**: two "wow" features at a low cost.

Hardware is hardly needed in this list: #63 can run on the phone, #55 uses the microphone already in place.
