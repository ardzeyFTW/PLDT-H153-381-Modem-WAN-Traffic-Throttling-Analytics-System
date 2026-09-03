FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NO_BROWSER=1 \
    PORT=8085

WORKDIR /app

# Install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY snmp_poller.py .
COPY app.js .
COPY index.html .
COPY styles.css .
COPY .env.example .

# Expose web port
EXPOSE 8085

# Run the analytics engine
CMD ["python", "snmp_poller.py"]
