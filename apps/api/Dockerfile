FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/packages/shared:/app/apps/api
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY packages/shared packages/shared
COPY apps/api apps/api
COPY migrations migrations
COPY alembic.ini .
RUN useradd --uid 10002 --no-create-home app
USER app
EXPOSE 8000
CMD ["uvicorn", "codeforge_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
