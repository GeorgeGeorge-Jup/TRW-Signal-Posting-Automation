FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Force Python to flush stdout immediately — required for Railway log visibility
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

CMD ["python", "main.py"]
