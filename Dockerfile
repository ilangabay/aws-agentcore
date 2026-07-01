FROM public.ecr.aws/docker/library/python:3.11-slim
 
WORKDIR /app
 
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
COPY backend_http.py .
 
EXPOSE 8080
 
ENV HARNESS_ARN="arn:aws:bedrock-agentcore:us-east-1:474668391771:harness/harness_mayeultest-ctS3wcwdvk"
 
CMD ["uvicorn", "backend_http:app", "--host", "0.0.0.0", "--port", "8080"]