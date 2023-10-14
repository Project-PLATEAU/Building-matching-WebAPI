# syntax = docker/dockerfile:1.2
FROM ubuntu:20.04

WORKDIR /app

RUN apt update
RUN apt install -y python3-pip libx11-6 libgl1-mesa-dev libpq-dev
RUN python3 -m pip install -U pip setuptools
COPY ./requirements.txt /tmp/
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m pip install -r /tmp/requirements.txt

COPY ./app /app/app
COPY README.md ./wsgi.py /app/

ENV FLASK_APP=app FLASK_DEBUG=1

CMD ["python3", "-m", "flask", "run", "--host=0.0.0.0", "--port=5000"]
