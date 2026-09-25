FROM python:3.12-alpine

ARG GIT_SHA=unknown
ENV AGENT_QA_GIT_SHA=${GIT_SHA} PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY app.py ./app.py
COPY data/synthetic-customer.json ./data/synthetic-customer.json

RUN addgroup -S app && adduser -S -G app app && chown -R app:app /app
USER app
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=2s --retries=3 CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/ready', timeout=1)"
CMD ["python", "app.py"]
