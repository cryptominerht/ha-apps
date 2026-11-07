ARG BUILD_FROM
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kidlock.py quiz.html config.example.yaml run.sh ./
COPY questions ./questions
RUN chmod +x /app/run.sh
CMD ["/app/run.sh"]
