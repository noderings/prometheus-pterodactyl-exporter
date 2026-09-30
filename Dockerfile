FROM python:3.12-slim
WORKDIR /app
COPY ptero_exporter.py .
EXPOSE 9222
CMD ["python3", "ptero_exporter.py"]
