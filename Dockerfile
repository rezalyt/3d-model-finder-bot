FROM node:22-bookworm-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY search-service/package.json ./search-service/package.json
RUN cd search-service && npm install --omit=dev

COPY requirements.txt ./requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PATH="/opt/venv/bin:$PATH"
ENV SEARCH_SERVICE_URL="http://127.0.0.1:8787"
ENV SEARCH_PORT="8787"

CMD ["python3", "render_server.py"]
