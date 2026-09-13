FROM python:3.12-slim

WORKDIR /app

# FFmpeg הוא תלות מערכת (לא Python) — נדרש להמרת הודעות קוליות מוואטסאפ
# (OGG/Opus וכו') ל-MP3 לפני תמלול (ר' app/transcription.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
