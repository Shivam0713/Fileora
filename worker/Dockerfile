FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
ENV PYTHONPATH=/app
CMD ["celery","-A","app.workers.celery_app:celery","worker","--loglevel=INFO"]
