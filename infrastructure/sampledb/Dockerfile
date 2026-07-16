# Generatore dei dati di esempio (Faker + numpy + pandas). Immagine usa-e-getta:
# gira una volta per popolare i due DB, poi si può rimuovere.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENTRYPOINT ["python", "generate.py"]
