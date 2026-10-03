# ---- stage 1: build the React dashboard ----
FROM node:20-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- stage 2: the API (serves the dashboard at /) ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY tradelens ./tradelens
# editable install keeps the package next to frontend/dist, which is where the app looks for the UI
RUN pip install -e .
COPY --from=ui /ui/dist ./frontend/dist
RUN useradd --create-home --uid 10001 tradelens && chown -R tradelens /app
USER tradelens
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "tradelens.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
