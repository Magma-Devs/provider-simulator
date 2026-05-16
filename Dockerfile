FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy stdlib Python sources at /app root AND the vendored proto stubs at
# /app/cosmos_pb2/ (MAG-1780). cosmos_pb2/__init__.py splices its own path
# onto sys.path so generated absolute imports resolve.
COPY *.py ./
COPY cosmos_pb2/ ./cosmos_pb2/
EXPOSE 18545 18546 18547 19000 18548 18549 18550
CMD ["python", "-u", "run.py"]
