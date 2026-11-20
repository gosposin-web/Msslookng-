FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/var/data/story-cloud COOKIE_SECURE=true
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 gosu && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10001 --create-home story
WORKDIR /app
COPY --from=build /install /usr/local
COPY app ./app
COPY start.sh ./start.sh
RUN chmod 755 /app/start.sh
EXPOSE 8000
CMD ["/app/start.sh"]
