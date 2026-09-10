# Pinned to 3.12: the app runs on 3.14 fine locally, but a container image is
# the one place to be conservative — every wheel in the dependency tree has a
# 3.12 build, so a dependency bump can never fail the image build for lack of a
# prebuilt wheel.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/packages:/app/services

WORKDIR /app

# Dependencies first, in their own layer, so a code change does not reinstall
# the world.
# Only the ml image installs the inference runtime (rembg + onnxruntime,
# ~200MB). api and worker do not need it to start, and a slimmer image is a
# faster rollout and a smaller attack surface.
ARG INSTALL_ML=false

COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && if [ "$INSTALL_ML" = "true" ]; then \
         pip install -e ".[dev,ml]"; \
       else \
         pip install -e ".[dev]"; \
       fi

COPY packages ./packages
COPY services ./services
COPY config ./config
COPY scripts ./scripts

# Non-root. A container that can write its own code is a container that can be
# persistently modified by whatever gets a shell in it.
RUN useradd --create-home --uid 10001 stylist && chown -R stylist:stylist /app
USER stylist

EXPOSE 8000
CMD ["uvicorn", "stylist_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
