=== File: ideas.md ===
# Ideas and Scenarios (2026-10-19, to choose from)

Principle from [use-cases.md](use-cases.md): the column does what is inconvenient to do on a phone.
Size: **S** — evening, **M** — day, **L** — several days. "Required" — what should already be in place.
Phrases are given as examples of how it sounds.

## Decisions 2026-10-20 (second wave, #38-#50, see market-research.md)

**Doing:** #38 fast path (simple commands can be executed silently or as short as possible, not always replying),
#39 master's voice (replying only to him) and "who is in the room", #40 homework and tests from WebUntis plus school
mail and Teams (the user often misses tasks in Teams), #41 "help me remember" (low priority),
#42 whisper, #43 room security (only in a safe form, see below), voice messages from Telegram are read aloud
on the column at home, #45 automation guesses (part of the "living" Maga), #46 translator for any languages,
#47 news by topics (try it), #50 breaks based on computer activity (feasible).
**Not doing:** #44 (there is "don't listen"), #48 (enough screen), #49.
**Constraint:** hidden eavesdropping and retelling of other people's conversations in the room (parents, brothers, guests) are
not done: this is recording people without their knowledge (§120 StGB in Austria, a criminal offense), not just a
privacy issue. Instead: "who was in the room" only by the voices of those who recorded a sample themselves, an "unknown
voice while I'm away" signal without recording and transcription, and a summary only of one's own conversations with Maga.

## Decisions 2026-10-20

**Doing:** #1 (light goes out smoothly over ~30 s, all of it), #2, #4, #5 (asks not every day, but randomly, and about uncompleted tasks of the day too),
#6 (progress bar on the screen and "Claude is waiting for you" indication), #7 (clap-clap switches all room lights),
#8, #9 (alarm clock only according to WebUntis schedule), #15 (ideas go to Google), #16, #18,
#19 (colors from the track cover, desk backlight only, effects and segments), #20 (plus: after dawn, lamps
do not return to their previous colors), #22 (emotional mode is always enabled), #23, #25 (plus visual on the screen),
#26 (squeeze maximum), #27 (school bus, warn about delays), #28, #29 (in the bot),
#31 (voice only), #33, #34 (together with the summary), #35, #36, #37.

**Not doing:** #3, #10, #11, #12, #13, #14, #17, #21 (no smart sockets), #24.
**Later:** #30, #32.

**New from user:** Maga proposes and writes new skills for himself using Claude Code;
Maga learns from the user and improves (to feel "alive"); works only when the user
is in the room (presence detection in the room: phone BLE beacon, fallback: LD2410 presence radar).

## Top 7: Most Value for Less Money

1. **#1. «Мага, спокойной ночи» ("Maga, good night") — one scenario for the whole evening** (M). The light smoothly fades into a warm
   "sunset" over a couple of minutes (WLED supports `transition`), the screen and sound quiet down, one phrase out loud:
   «Будильник на 7:10, завтра первое дело в 10, ночью дождь» ("Alarm set for 7:10, first task tomorrow at 10, rain at night"). The column checks that the phone is on
   charging, and stays silent until morning after that. Required: Google Calendar (exists), HA lights (exist).
2. **#2. Quieter and shorter at night** (S). From `night_start` to `night_end`, replies are whispered and in a single
   phrase, speaker volume drops automatically (`volume` is already in the protocol), prompt includes "speak
   quietly, briefly". Currently, Maga speaks at night just like during the day, while people nearby are sleeping.
3. **#3. «Что я пропустил?» ("What did I miss?")** (M). While you are away or wearing headphones, the column collects phone notifications
   (the bridge already receives them). Then: «Три сообщения от мамы, одно от банка, остальное шум» ("Three messages from mom, one from the bank, the rest is noise").
   Gemini will filter out the noise itself. Required: `bridge.py` (`notifs`), fetch history from it.
4. **#4. Language partner mode: German** (M). Currently the prompt says "Russian only". The phrase
   «давай по-немецки» ("let's speak in German") switches Maga to conversation in German: answers with simple phrases,
   gently corrects mistakes, "word of the day" once a day, repeating words you confused. You
   already have "Learn German" in your tasks: Maga will ask about it himself in the evening. The Gemini Live voice is
   very well-suited for this.
5. **#5. Habits with snark** (S). Pulled from Google Tasks: "Workout", "Pool", "Feed the hamster": in the evening
   «Тренировка сегодня была?» ("Did you work out today?"), response goes into the task as completed, upon skipping
   for the third consecutive day: «Хомяк уже начал писать жалобу» ("The hamster has already started writing a complaint"). Required: `google_tasks` (exists),
   routines branch (`routines`).
6. **#6. Claude Code reports via voice** (S). The Claude Code `Stop` hook sends a short post to the server
   (the computer bridge already exists), Maga says: «Сессия с Telegram закончила, три коммита» ("Telegram session finished, three commits") or
   highlights the desk green. Especially convenient when you are waiting for a long task and stepped away.
7. **#7. Clap-clap — bedside light** (M, firmware). Two claps in a row right on the column itself (simple
   sound analysis on ESP, without a wake word and without a server) toggle the bedside lamp. Instantly, no need to speak
   out loud at night, works without the internet. False positives are caught using a threshold and
   a couple of strikes with an interval of 0.3–0.7 s.

## Sleep and Morning

- **#8. Gentle awakening** (S). Sunrise is already implemented. 30 seconds after turning off the alarm,
  quietly: «Доброе утро, 7:10, на улице 9 градусов, первое дело в 10» ("Good morning, 7:10, 9 degrees outside, first task at 10"). If the phone is still not
  unlocked, "definitely awake" (1.2) wakes you up more harshly.
- **#9. Calendar-based alarm** (M). In the evening: «Завтра у тебя ничего до 11, будильник на 7:10
  можно сдвинуть на 9:30?» ("You have nothing until 11 tomorrow, can the 7:10 alarm be shifted to 9:30?"). Decided by voice. The bridge sets the alarm on the phone (Android intent).
- **#10. Sleep diary** (S). Bridge already tracks sleep/wake times. Once a week in Telegram: average sleep,
  latest bedtimes, «в четверг ты лёг в 3:12, молодец» ("on Thursday you went to bed at 3:12, well done").
- **#11. Evening light warms up automatically** (S). From sunset, lamps shift towards 2700 K (`color_temp_kelvin`
  exists). By voice «режим вечер» ("evening mode") or automatically.
- **#12. Sleep sounds** (M). Rain, noise, fireplace with sleep timer (1.9 from use-cases). Playing
  pre-prepared files from the server to the speaker: audio streaming is already supported.

## At the Computer

- **#13. Pomodoro with desk backlight** (S). «Фокус 50» ("Focus 50"): desk backlight cold white, timer ring along the edge
   of the screen, at the end the desk smoothly turns green and Maga says: «Перерыв, встань» ("Break, get up"). On the 10th
   minute of the break, the desk turns blue again. Everything is ready: WLED, screen timer, `home_set` with color.
- **#14. Focus mode** (M). «Мага, фокус» ("Maga, focus"): mirror phone notifications to the column only from
  selected people, cooler lighting, shorter replies, column stays silent except for urgent matters.
- **#15. Dictating notes into a project** (S). «Запиши в идеи: …» ("Note to ideas: ...") adds a line to the required file or
  Google Tasks with a tag. Suitable for study and projects: thoughts won't get lost while hands are on the keyboard.
- **#16. Reading email on demand** (S). «Прочитай, что написал банк» ("Read what the bank wrote"): subject and essence, not the full text
  (`google_mail_unread` exists, add reading a single email).

## Smart Home

- **#17. Scenes with a single phrase** (S). «Кино» ("Movie"): dim overhead lights, dark blue cabinet backlight, desk
  turned off. «Работа» ("Work"): cold white on the desk. Call WLED presets and assemble via HA scenes.
- **#18. «Мама зовёт» ("Mom is calling")** (S). When parents write or call, the desk backlight flashes warm briefly
  (bridge already shows a card on the screen, add a light flash).
- **#19. Music-reactive lighting** (L). From the mini-player on the phone, take the album art, calculate the dominant color and
  paint the desk and the Light Bar. Beautiful, but many moving parts.
- **#20. «Я ушёл» / «Я дома» ("I'm leaving" / "I'm home")** (M). Upon presence detection (`presence` branch): left, and Maga turns off everything
  unnecessary and reminds you what you left on (smart plugs). Arrived, and lights turn on smoothly.
- **#21. Iron and socket control** (S). «Мага, что у меня сейчас включено?» ("Maga, what is currently on?") (`home_list`
  shows states), with an evening check before sleep.

## Conversation and Character

- **#22. Mood by voice** (S, trying out). Gemini's emotional mode can already be enabled by voice
  («включи эмоциональный режим» ("enable emotional mode")). If the model accepts it, Maga matches the tone of fatigue or
  joy. Test and keep if you like it.
- **#23. Character by time of day** (S). Milder in the morning, more sarcastic in the evening, calm after 23:00.
  A single parameter in the prompt derived from the current hour.
- **#24. One-minute evening reflection** (S). Optional: «Как день? Одно, что получилось» ("How's your day? One thing that went well"). The answer
  goes as a line into the diary (Google Tasks or a file), once a week Maga retells it.
- **#25. Voice games** (M). «Города» ("Cities"), «20 вопросов» ("20 Questions"), German vocabulary quiz. Die
  and coin are already on the screen.
- **#26. Eye reactions to sound** (M, firmware). Eyes turn slightly towards a loud sound, blink to the
  beat of music. Adds more life than it seems.

## Information

- **#27. Transit** (M). Wiener Linien: «Когда следующий трамвай?» ("When is the next tram?") and «пора выходить» ("time to leave") according to the calendar.
  Open data, no API key needed.
- **#28. «Возьми зонт» ("Take an umbrella")** (S). On «я ушёл» ("I'm leaving") and rain in the forecast.
- **#29. Weekly summary in Telegram** (S). Sleep, tasks, how many times the column replied, what it learned
  that is new. At the same time, highlights what can be cleaned up in memory.

## Hardware

- **#30. Sensors directly on the column** (S, ~10 €). BME280 (temperature, humidity, pressure) via I²C to
  ESP32-S3: «Тут 27 градусов, душно» ("It's 27 degrees here, stuffy"). SCD40 (CO₂) even more useful for sleep: «CO₂ 1400, открой
  окно» ("CO₂ is 1400, open the window"). Data goes to HA and into the conversation.
- **#31. «Не слушай» ("Don't listen") button** (S). Physical or touch: microphone is disabled programmatically,
  red dot on the screen, by voice «не слушай час» ("don't listen for an hour"). Parents are home, this provides peace of mind.
- **#32. Microphone power capacitor** (S). 10 µF + 100 nF at INMP441 (roadmap stage 2):
  fewer clicks, better wake word detection.

## Reliability and Maintenance

- **#33. Column is not mute without a server** (M, firmware). Clocks, timers, and alarm-reminders
  work independently if the server or Wi-Fi drops; "no connection" on the screen.
- **#34. Memory backup** (S). Once a week the bot sends you `brain.db` as a file in Telegram.
  Maga's memory is valuable, losing it would be a pity.
- **#35. Gemini auto-fallback** (S). When Gemini returns 503 (like the bot did today), switch to
  a backup model and announce it once instead of staying silent.
- **#36. System health via voice** (S). «Мага, как ты?» ("Maga, how are you?"): version, connection, errors over the past day, battery
  charge, status of development sessions.
- **#37. Set of voice test checks** (M). 20–30 recordings via `rec` («Мага» ("Maga") with varying volume,
  background speech, TV) and a script that runs them through the wake-word detector. This way "Maga" is tuned
  by numbers, not by ear. Next step: train your own word model (stage 5).

## Proposed Order

1. #2 quieter at night, #1 «спокойной ночи» ("good night"), #7 clap-clap: all about sleep, the main scenario.
2. #3 what I missed, #5 habits, #6 Claude Code reports.
3. #4 German, #13 Pomodoro with desk, #17 scenes.
4. #30 CO₂ sensor and #31 «не слушай» ("don't listen") button, when there is time for hardware.

The `presence` and `routines` branches are needed for #5, #20, #28: they rely on them.
