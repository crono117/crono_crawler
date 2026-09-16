FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers DATA_DIR=/app/data
WORKDIR /app
COPY requirements*.txt ./
ARG INSTALL_BROWSER=0
RUN pip install --no-cache-dir -r requirements.txt
RUN if [ "$INSTALL_BROWSER" = "1" ]; then pip install --no-cache-dir -r requirements-browser.txt && python -m playwright install --with-deps chromium; fi
COPY . .
RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/data /app/staticfiles && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["python", "deploy/entrypoint.py"]
