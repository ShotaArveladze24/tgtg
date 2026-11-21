FROM python:3.12-slim
WORKDIR /app
COPY taskbot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY taskbot /app/taskbot
ENV TASKBOT_DB=/data/taskbot.db
VOLUME ["/data"]
CMD ["python", "-m", "taskbot"]
