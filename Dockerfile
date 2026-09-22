FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    sqlite3 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user and required directories
RUN useradd -m stockuser && \
    mkdir -p /data/input /data/output /data/interim /external/strategy && \
    ln -s /data /app/data && \
    chown -R stockuser:stockuser /app /data /external

# Install Python dependencies directly as stockuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

USER stockuser

# Copy source code and default configs
COPY --chown=stockuser:stockuser . .

# Default command
ENTRYPOINT ["python", "src/orchestrator.py"]
CMD ["daily"]
