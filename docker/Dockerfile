FROM python:3.11.18-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata curl && \
    rm -rf /var/lib/apt/lists/*

# Pre-bake Python dependencies (no runtime installs). certifi 2026.10.23 drops the compromised
# eMudhra roots and two more emSign roots; requests, dxpy and botocore verify with it.
RUN pip install --no-cache-dir --cert /etc/ssl/certs/ca-certificates.crt \
    certifi==2026.10.23 \
    dxpy==0.363.0 \
    fsspec==2024.3.1 \
    fsspec-dnanexus==0.2.6 \
    requests==2.28.2 \
    PyYAML==6.0.2 pydicom==2.4.4

WORKDIR /app
COPY app/main.py /app/main.py
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/main.py"]

