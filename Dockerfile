FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIND_HOST=0.0.0.0 DATABASE_PATH=/data/vintedbot.db
WORKDIR /service
RUN useradd --uid 10001 --create-home bot && mkdir /data && chown bot:bot /data
COPY --chown=bot:bot app /service/app
USER bot
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "-m", "app.server"]
