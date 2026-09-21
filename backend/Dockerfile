FROM python:3.12-slim

# Tesseract OCR（本地图片文字提取，含中文）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-chi-sim \
        libgomp1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    rm -rf /tmp/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["python", "main.py"]
