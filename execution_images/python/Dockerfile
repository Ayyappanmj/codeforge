FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox \
 && mkdir -p /opt/codeforge /sandbox && chown 10001:10001 /sandbox
COPY entrypoint.py /opt/codeforge/entrypoint.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/sandbox
USER 10001:10001
WORKDIR /sandbox
ENTRYPOINT ["python3", "-I", "/opt/codeforge/entrypoint.py"]
