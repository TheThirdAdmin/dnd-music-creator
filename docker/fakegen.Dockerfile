FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" "numpy>=2.0"
COPY fakegen fakegen
ENV FAKEGEN_DELAY_S=2 FAKEGEN_MAX_DURATION=60
EXPOSE 8001
CMD ["uvicorn", "fakegen.app:app", "--host", "0.0.0.0", "--port", "8001"]
