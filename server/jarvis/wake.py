"""Слово пробуждения на сервере: модели openWakeWord на onnxruntime, без самого пакета
openwakeword (он тянет tflite, scipy и scikit-learn, а tflite под новый Python не ставится).

Цепочка как в openWakeWord: звук кусками по 80 мс → мел-спектр (melspectrogram.onnx) →
признаки по окну из 76 кадров (embedding_model.onnx) → последние 16 признаков → вероятность
слова (hey_jarvis_v0.1.onnx). Модели лежат в data/models, при первом запуске скачиваются.
"""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MODELS_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
FEATURE_MODELS = ("melspectrogram.onnx", "embedding_model.onnx")
DEFAULT_WORD = "hey_jarvis_v0.1"

CHUNK = 1280          # 80 мс при 16 кГц — шаг модели
CONTEXT = 160 * 3     # хвост прошлого куска для мел-спектра (как в openWakeWord)
MEL_WINDOW = 76       # кадров мел-спектра на один признак
MEL_KEEP = 970        # ~10 с мел-спектра
FEAT_KEEP = 120       # ~10 с признаков
WARMUP = 5            # первые оценки после старта — мусор, считаем нулём


def ensure_models(model_dir: Path, word: str = DEFAULT_WORD) -> None:
    """Скачать недостающие модели (около 4 МБ) с релизов openWakeWord."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for name in (*FEATURE_MODELS, f"{word}.onnx"):
        path = model_dir / name
        if path.is_file() and path.stat().st_size > 0:
            continue
        log.info("скачиваю модель %s", name)
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(MODELS_URL + name, tmp)
        tmp.replace(path)


class WakeWord:
    """Потоковый детектор: process() принимает PCM 16 кГц 16 бит любыми кусками и
    возвращает самую высокую вероятность слова за обработанные куски (0…1)."""

    def __init__(self, model_dir: Path | str, word: str = DEFAULT_WORD):
        import onnxruntime as ort  # тяжёлый импорт — только когда детектор нужен

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        model_dir = Path(model_dir)

        def load(name: str):
            return ort.InferenceSession(str(model_dir / name), sess_options=opts,
                                        providers=["CPUExecutionProvider"])

        self._mel = load("melspectrogram.onnx")
        self._emb = load("embedding_model.onnx")
        self._word = load(f"{word}.onnx")
        self._mel_in = self._mel.get_inputs()[0].name
        self._emb_in = self._emb.get_inputs()[0].name
        self._word_in = self._word.get_inputs()[0].name
        self._n_feat = self._word.get_inputs()[0].shape[1]   # 16 для hey_jarvis
        self.reset()

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.int16)
        self._context = np.zeros(0, dtype=np.int16)
        self._mels = np.ones((MEL_WINDOW, 32), dtype=np.float32)
        noise = np.random.default_rng(0).integers(-1000, 1000, 16000 * 4).astype(np.int16)
        self._feats = self._embed_clip(noise)
        self._chunks = 0

    def _melspec(self, x: np.ndarray) -> np.ndarray:
        out = self._mel.run(None, {self._mel_in: x.astype(np.float32)[None, :]})[0]
        return np.squeeze(out) / 10 + 2   # как в openWakeWord: ближе к исходной модели Google

    def _embed(self, window: np.ndarray) -> np.ndarray:
        return self._emb.run(None, {self._emb_in: window[None, :, :, None]})[0].reshape(-1, 96)

    def _embed_clip(self, x: np.ndarray) -> np.ndarray:
        mel = self._melspec(x)
        windows = [mel[i:i + MEL_WINDOW] for i in range(0, mel.shape[0] - MEL_WINDOW + 1, 8)]
        return self._emb.run(None, {self._emb_in: np.array(windows, dtype=np.float32)[..., None]})[0].reshape(-1, 96)

    def process(self, pcm: bytes) -> float:
        self._pending = np.concatenate((self._pending, np.frombuffer(pcm, dtype=np.int16)))
        best = 0.0
        while self._pending.shape[0] >= CHUNK:
            chunk, self._pending = self._pending[:CHUNK], self._pending[CHUNK:]
            audio = np.concatenate((self._context, chunk))
            self._context = audio[-CONTEXT:]
            self._mels = np.vstack((self._mels, self._melspec(audio)))[-MEL_KEEP:]
            self._feats = np.vstack((self._feats, self._embed(self._mels[-MEL_WINDOW:])))[-FEAT_KEEP:]
            out = self._word.run(None, {self._word_in: self._feats[None, -self._n_feat:].astype(np.float32)})[0]
            self._chunks += 1
            score = float(np.ravel(out)[0]) if self._chunks > WARMUP else 0.0
            best = max(best, score)
        return best
