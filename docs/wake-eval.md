# Wake Word Evaluation on Recordings

Why: After changing the threshold, model, or microphone, quickly check whether the smart speaker has
become deaf (failed to hear «Мага» ("Maga")) or overly talkative (detects the word where it isn't).
The `server/tools/wake_eval.py` script runs WAV files through the same detectors used on the server:
Vosk («Мага» ("Maga")) and openWakeWord («Hey Jarvis»).

## How to Record from the Speaker

The server must be running, and the speaker connected. From the `server` folder:

```
python tools/send.py rec 5
```

The speaker records 5 seconds of microphone audio, the file is saved to `server/recordings/` (its name
is printed). Next, move and name the file so the script knows what is expected of it:

- `data/eval/yes_01.wav` — the recording contains the word («Мага, включи свет» ("Maga, turn on the light")),
  the detector must trigger;
- `data/eval/no_01.wav` — no word: normal conversation, TV, music, your speech without the word,
  noise, silence. It must not trigger.

Tips: say the word from different locations (from the bed, from the computer), quietly and loudly,
with background noise (music, fan). For `no_`, neighbor words («мама» ("mom"), «магазин» ("store"),
«сага» ("saga"), «ага» ("aha")) and long recordings of normal conversation (`rec 30`) are especially
useful. 10–20 recordings of each type are enough; the sample rate and format are the same as the
speaker's (16 kHz, 16 bit), the script will convert any other files itself.

## Running

```
python tools/wake_eval.py                       # both detectors
python tools/wake_eval.py --detectors vosk      # «Мага» ("Maga") only
python tools/wake_eval.py --threshold 0.6       # different threshold
```

Example output:

```
vosk (Мага): порог 0.5, записей со словом 12, без слова 20
  пропуски: 1 из 12 — yes_07.wav (0.00)
  ложные срабатывания: 0 из 20
```

Exit code 0 — all clean, 1 — there are misses or false positives, 2 — no recordings in the folder.
Vosk has a score of 0 or 1 (word heard or not), so the threshold does not affect it; openWakeWord has
a smooth score of 0…1 and the threshold matters (it is set by `JARVIS_WAKE_THRESHOLD`).

Recordings are personal, so `server/data/` is not tracked in git.
