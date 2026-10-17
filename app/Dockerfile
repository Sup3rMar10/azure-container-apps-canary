FROM python:3.13-alpine

# Patch OS packages at build time so the image passes the vulnerability gate it will face in CI.
RUN apk upgrade --no-cache && adduser -D -u 10001 app
WORKDIR /app
COPY main.py .
USER 10001

ENV PYTHONUNBUFFERED=1
# Python is PID 1 and installs a SIGTERM handler, so `docker stop` and the platform's shutdown both drain cleanly.
CMD ["python", "main.py"]
