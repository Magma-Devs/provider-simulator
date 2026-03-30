FROM python:3.12-slim
WORKDIR /app
COPY *.py .
EXPOSE 18545 18546 18547 19000
CMD ["python", "-u", "server.py"]

