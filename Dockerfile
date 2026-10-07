FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config
COPY data/lender_registry.yaml ./data/
RUN pip install --no-cache-dir .
# stdio transport: run with `docker run -i --env-file .env lending-risk-mcp`
ENV LRC_POLICY_PATH=/app/config/policy.yaml \
    LRC_REGISTRY_PATH=/app/data/lender_registry.yaml \
    HMDA_DB_PATH=/app/data/hmda.sqlite \
    LRC_AUDIT_LOG=/app/logs/audit.jsonl
ENTRYPOINT ["lending-risk-mcp"]
