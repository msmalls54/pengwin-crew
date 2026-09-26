FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
RUN useradd --uid 10001 --create-home runner
WORKDIR /runner
COPY sandbox/worker.py /runner/worker.py
RUN pip install --no-cache-dir httpx==0.28.1 playwright==1.55.0
USER 10001:10001
ENTRYPOINT []
