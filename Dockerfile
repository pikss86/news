FROM python:3.12-slim-bookworm AS tdlib-builder

ARG TDLIB_GIT_REF=022d60202e446ad1287b9fb68e687c8a0760788b

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates cmake g++ git gperf make libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git init /src/tdlib \
    && git -C /src/tdlib remote add origin https://github.com/tdlib/td.git \
    && git -C /src/tdlib fetch --depth 1 origin "${TDLIB_GIT_REF}" \
    && git -C /src/tdlib checkout FETCH_HEAD

RUN cmake -S /src/tdlib -B /src/tdlib/build \
    -DCMAKE_BUILD_TYPE=Release \
    -DTD_ENABLE_JNI=OFF

RUN cmake --build /src/tdlib/build --target tdjson -j2

FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends libssl3 libstdc++6 zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system collector \
    && useradd --system --gid collector --home-dir /app collector

COPY --from=tdlib-builder /src/tdlib/build/libtdjson.so /usr/local/lib/libtdjson.so

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY tests ./tests
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN pip install --no-cache-dir ".[dev]" \
    && mkdir -p /var/lib/tdlib \
    && chown -R collector:collector /app /var/lib/tdlib \
    && chmod +x /usr/local/bin/docker-entrypoint.sh \
    && ldconfig

USER collector
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
