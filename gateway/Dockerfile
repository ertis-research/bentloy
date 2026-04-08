FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY service.py /app/service.py

EXPOSE 3000

CMD ["bentoml", "serve", "service:GatewayService", "--host", "0.0.0.0", "--port", "3000"]
