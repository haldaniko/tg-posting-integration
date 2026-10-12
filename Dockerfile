FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN useradd --create-home bot && mkdir -p /data/photos && chown -R bot:bot /data
USER bot
ENV DATABASE_URL=sqlite:////data/bot.sqlite3 PHOTO_DIR=/data/photos
CMD ["tg-posting-bot"]

