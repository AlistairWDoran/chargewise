# ChargeWise backend — API + ingestion pipeline in one image.
#
# Build (from backend/):  docker build -t chargewise-backend .
# The same image serves the API (default CMD) and runs the scheduled
# pipeline (see docker-compose.nas.yml), so there is one artefact to manage.

FROM python:3.12-slim

# tzdata: the cost engine resolves the IOG core window in Europe/London.
RUN pip install --no-cache-dir tzdata

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.110" "uvicorn[standard]>=0.29" "httpx>=0.27" \
    "pydantic>=2.6" "pydantic-settings>=2.2" "sqlalchemy>=2.0"

# Then the application code and the scheduler loop.
COPY chargewise ./chargewise
COPY scheduler.sh ./scheduler.sh
RUN pip install --no-cache-dir --no-deps . && chmod +x /app/scheduler.sh

# SQLite lives on a mounted volume at /app/data (see compose files).
RUN mkdir -p /app/data
ENV TZ=Europe/London

EXPOSE 8000
CMD ["uvicorn", "chargewise.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
