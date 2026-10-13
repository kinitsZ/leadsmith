FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .

COPY agency_profile.yaml ./
COPY examples ./examples

RUN useradd --create-home app && mkdir -p /app/data && chown app /app/data
USER app

ENV LEADSMITH_DATABASE_URL=sqlite:////app/data/leadsmith.db
EXPOSE 8000
CMD ["uvicorn", "leadsmith.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
