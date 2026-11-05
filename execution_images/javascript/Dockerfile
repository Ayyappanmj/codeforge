FROM node:22-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox \
 && mkdir -p /opt/codeforge /sandbox && chown 10001:10001 /sandbox
COPY entrypoint.js /opt/codeforge/entrypoint.js
ENV HOME=/sandbox NODE_ENV=production
USER 10001:10001
WORKDIR /sandbox
ENTRYPOINT ["node", "/opt/codeforge/entrypoint.js"]
