# Образ аддона. Контекст сборки — папка, которую готовит tools/deploy_addon.py.
FROM python:3.13-alpine

# tzdata — чтобы datetime.now() знал летнее время по переменной TZ от Home Assistant
RUN apk add --no-cache tzdata

WORKDIR /app
COPY pyproject.toml ./
COPY jarvis ./jarvis
RUN pip install --no-cache-dir --root-user-action=ignore .

# токен и прочие JARVIS_* — из server/.env разработчика; /data переживает перезапуск и обновление
COPY jarvis.env ./.env
ENV JARVIS_DATA=/data PYTHONUNBUFFERED=1

CMD ["python", "-m", "jarvis"]
