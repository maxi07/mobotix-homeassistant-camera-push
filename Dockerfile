FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py storage.py wsgi.py gunicorn_config.py ./

RUN mkdir -p /data/images && chown -R nobody:nogroup /data /app
USER nobody

EXPOSE 18425

CMD ["gunicorn", "--config=gunicorn_config.py", "--bind=0.0.0.0:18425", "--workers=1", "--threads=2", "--access-logfile=-", "--error-logfile=-", "wsgi:app"]