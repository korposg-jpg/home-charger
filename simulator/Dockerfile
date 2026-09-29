FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY evsim ./evsim
RUN pip install --no-cache-dir . && useradd --uid 568 evsim
USER 568
EXPOSE 9100

CMD ["evsim"]
