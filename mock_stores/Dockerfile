FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.141.1 uvicorn==0.54.0
COPY crew/__init__.py /app/crew/__init__.py
COPY crew/catalog.py /app/crew/catalog.py
COPY mock_stores/app.py /app/mock_stores/app.py
USER 65532:65532
CMD ["uvicorn", "mock_stores.app:app", "--host", "0.0.0.0", "--port", "8080"]
