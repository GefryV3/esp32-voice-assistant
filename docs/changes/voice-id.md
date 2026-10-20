=== File: voice-id.md ===
# voice-id: owner's voice (#39), "who is in the room", security (#43)

## Done
New module `server/jarvis/voiceid.py` (+ minimal additive edits to `voice.py`, `telegram.py`,
`__main__.py`, `config.py`, `.env.example`, `pyproject.toml` comment) and `tests/test_voiceid.py` (35 tests).

- **Model**: WeSpeaker ResNet34 (VoxCeleb), ONNX 26.5 MB, ECAPA-class quality (EER ~1% on VoxCeleb).
  Chosen instead of ECAPA-TDNN: no ready-made ECAPA in ONNX with features inside is available, whereas this works on
  `onnxruntime` + numpy without torch (Python 3.12 Windows and `python:3.13-slim`, **no new dependencies**).
  Features (Kaldi fbank 80) are computed on numpy. Downloaded on first start to `data/models` (`ensure_model`,
  like Vosk), not present in the repository. Source: sherpa-onnx release on GitHub (link verified, 200 OK).
- **Tested on real model** (downloaded in scratchpad, not in repository): two Windows synthetic voices
  (Zira/Hazel), 4 phrases each. Same voice, different phrases: similarity 0.69–0.83; different voices: 0.22–0.42.
  256-number vector, ~70 ms per 1.5 s of speech on this PC. This is pure synthesis, not a live microphone.
- **Voiceprints**: `voiceprints(name, embedding, ts)` table in brain.db. Vectors only, audio is not stored.
  Comparison: cosine similarity with a person's averaged vector.
- **Recording** `voice enroll [name]` (console and Telegram): 10 short phrases on the speaker screen (`caption`), a beep tone,
  5 s of recording via `audio.record` (general microphone stream), speech duration ≥1 s required (otherwise one retry),
  saved if ≥5 successful attempts, otherwise discarded. Runs in the background, result arrives in Telegram. Re-recording replaces the old one.
  For another person (`voice enroll аня` ("anya")), the first phrase is consent, which they read out loud themselves; nobody is recorded
  without an explicit command; `voice forget <name>` deletes.
- **Verification after wake word**: after «Мага» ("Maga"), the conversation does not open immediately; the server accumulates 1-2 s of speech (by
  sound level), compares it with the owner, and only then starts the conversation; accumulated audio is appended to
  Gemini (latency ~0.5 s on short phrases, almost none on long ones). Stranger / recorded other person: in log
  `чужой голос, сходство 0.31 (порог 0.50)` ("stranger voice, similarity 0.31 (threshold 0.50)"), speaker is silent. No speech for 4 s after wake word: nothing opens.
- **Modes**: `voice on|off` (off by default; `on` refuses without owner voiceprints or model),
  `voice any [min]` — for everyone for N minutes (30 by default, max 480; `any 0` disables), `voice threshold [0.2…0.9]`
  (+ `JARVIS_VOICEID_THRESHOLD`, 0.5 by default), `voice status`, `voice list`.
- **Uncertainty policy**: similarity within threshold−0.10 and it is currently night (`bridge.night()`) → **we answer**
  (hoarse/sleepy voice, speaker by the bed). Daytime same similarity → we remain silent. Model failure, comparison error
  → we answer (no proof that a stranger is speaking). Recorded other person (similarity ≥ threshold)
  also gets no answer at night.
- **Who is in the room**: Gemini tool `who_is_here` (via `brain.extensions`). Who spoke last and who
  was heard after the wake word over the last 15 minutes. In process memory only, not written to disk. Recognizes only
  people who enrolled themselves; the rest are "unknown". Works while in `owner` mode (otherwise check is not performed).
- **Security** `guard on|off` (pref `guard`, off by default): owner is not present via `presence.here()`, but microphone hears
  speech (≥3 s by level over 20 s) → single message in Telegram: time, seconds of speech, whose voice (owner / name /
  unknown / undetermined). Antispam 10 minutes. Without recording and transcription; 4 s of audio for identification live
  in memory. Does not work during conversation, sample recording, and silence mode `mute`.

## Intentionally not done (confirmed in docs/ideas.md)
Hidden recording, transcription and retelling of strangers' conversations, background room recording. In Austria, this is criminally
punishable; we only do voice recognition by voiceprint and security metadata.

## New variables and commands
`JARVIS_VOICEID=on|off` (off — model is not downloaded, commands remain without effect), `JARVIS_VOICEID_THRESHOLD=0.5`.
Console/Telegram: `voice status|list|threshold [x]|on|off|any [min]|enroll [name]|forget <name>`, `guard on|off`.
Commands `voice idle…` (screen) are not affected. The voiceprints table is created automatically.

## What accuracy to expect (honestly)
- Model benchmark on clean speech: ~1% error. At home it is worse: cheap INMP441 at 1-3 m, room echo, 1-2 s of speech,
  colds, whispering at night, Russian speech on an English model. Realistically: owner is recognized in 85-95% of utterances with
  threshold 0.5, an adult stranger is filtered out in ~90-95%. A similar voice (brother, father, child of the same age)
  may pass, while whispering and shouting lower the similarity. This is **convenience, not security**: a speaker recording can fool it.
- Short commands («Мага, свет» ("Maga, light"), 0.5-1 s of speech) are more reliable than long ones.
- Sound-level security catches both TV and music: false positives are possible (hence the 3 s threshold and antispam).

## What the user must measure at home
1. `voice enroll` in a quiet room, at the usual spot (by the bed), then again at the computer.
2. While mode is off: say «Мага, …» ("Maga, ...") a couple of dozen times and watch `log`: in owner mode, lines
   `голос хозяина, сходство …` ("owner voice, similarity ..."); record spread (normal, whisper, after waking up, with head turned).
   Set threshold below minimum of "own" by 0.05-0.1 and above maximum of "strangers" (ask family members to say «Мага» ("Maga")).
3. Night window: whisper similarity must be in [threshold−0.10, threshold); if lower — `voice threshold` down.
4. Response time after wake word (addition of ~0.5 s on short phrases) and that a wake word without speech does not open conversation.
5. Whether verification interferes with the wake word during barge-in/follow-up (check only on wake word, not on continuation of conversation).
6. `guard on`, leave the room for 10 minutes (presence away) and turn on the TV in it: how many false messages.

## Not tested live
Everything on fallbacks and synthetic voices. Not tested: recording samples on a real speaker (caption
signing on screen, `tone` beep, 5-second window), verification latency in combat, behavior on a live microphone,
Telegram security messages, operation as an addon image (onnxruntime is already in the image, model will download on first start:
internet access from the addon to github.com is required). Deployment and firmware untouched.

## Merging
Common files: in `voice.py` +3 points (`voiceid` attribute, `voiceid.on_audio` call before detector, `voiceid.begin` call
on wake word), `telegram.py` (attribute and route `voice`/`guard`), `__main__.py` (creation and background
model download; `brain.extensions` and `hub.extras` receive `voiceid`), `config.py` (2 fields). Version and
CHANGELOG not changed.
