FROM python:3.12-slim
WORKDIR /app
# psycopg is only needed for the PostgreSQL path; SQLite needs nothing extra.
RUN pip install --no-cache-dir "psycopg[binary]>=3.1"
COPY inventory_service ./inventory_service
COPY schema ./schema
EXPOSE 8000
CMD ["python", "-m", "inventory_service", "--host", "0.0.0.0"]
