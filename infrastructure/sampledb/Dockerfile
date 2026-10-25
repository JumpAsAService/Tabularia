# The sample data generator (Faker + numpy + pandas). A throwaway image:
# it runs once to fill the two databases, then it can be removed.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENTRYPOINT ["python", "generate.py"]
