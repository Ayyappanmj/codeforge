FROM docker:27-cli AS dockercli

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/packages/shared:/app/apps/worker
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Only the docker CLI is copied in; the daemon socket is mounted by docker-compose (worker only).
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY packages/shared packages/shared
COPY apps/worker apps/worker
RUN useradd --uid 10003 --no-create-home app
USER app
CMD ["python", "-m", "codeforge_worker.main"]
