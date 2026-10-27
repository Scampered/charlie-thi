# The Python server: Flask + gunicorn + ChatterBot + Scrapy on Python 3.7.
# Your code is NOT copied in: docker-compose.yml mounts this folder as /app,
# so a changed .py file only needs "docker compose restart flask6".
FROM python:3.7-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# compilers, needed to install some of the Python packages
RUN apt-get update && \
    apt-get -y install --no-install-recommends gcc g++ build-essential libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /
RUN pip install --no-cache-dir -r /requirements.txt

# Language data ChatterBot 1.0.4 needs. Downloaded once here, so the first
# "hi" doesn't have to download it (and it works without internet afterwards).
RUN python -m nltk.downloader -d /usr/local/share/nltk_data \
        stopwords wordnet punkt averaged_perceptron_tagger

# gunicorn loads the app as "app.create_app", so it must start in the folder above /app
WORKDIR /
