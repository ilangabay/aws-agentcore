FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend_http.py .

# ECS/App Runner health checks and routing expect the app on a known port.
EXPOSE 8080

# HARNESS_ARN can be overridden as an environment variable in the task/service config.
ENV HARNESS_ARN="arn:aws:bedrock-agentcore:us-east-1:474668391771:harness/harness_mayeultest-ctS3wcwdvk"

CMD ["uvicorn", "backend_http:app", "--host", "0.0.0.0", "--port", "8080"]