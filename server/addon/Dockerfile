# Образ аддона. Контекст сборки — папка, которую готовит tools/deploy_addon.py.
# Debian, а не Alpine: onnxruntime (слово пробуждения) есть готовым только под glibc.
FROM python:3.13-slim

# tzdata — чтобы datetime.now() знал летнее время по переменной TZ от Home Assistant
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY jarvis ./jarvis
RUN pip install --no-cache-dir --root-user-action=ignore .

# токен, ключ Gemini и прочие JARVIS_* — из server/.env разработчика; /data переживает
# перезапуск и обновление (настройки колонки, модели слова пробуждения, записи)
COPY jarvis.env ./.env
ENV JARVIS_DATA=/data PYTHONUNBUFFERED=1

CMD ["python", "-m", "jarvis"]
