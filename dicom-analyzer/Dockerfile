FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && mv /usr/local/bin/TotalSegmentator /usr/local/bin/TotalSegmentator-real \
    && cp /app/totalsegmentator-wrapper.py /usr/local/bin/TotalSegmentator \
    && chmod +x /usr/local/bin/TotalSegmentator /usr/local/bin/TotalSegmentator-real
COPY server.py ./server.py
COPY totalsegmentator-wrapper.py ./totalsegmentator-wrapper.py
ENV PYTHONUNBUFFERED=1
CMD ["sh","-c","uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}"]
