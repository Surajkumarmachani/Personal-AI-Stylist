# LiteLLM for Cloud Run. Compose mounts config.yaml; Cloud Run has no bind
# mounts, so the config is baked in here instead. The base tag matches compose.
FROM ghcr.io/berriai/litellm:main-stable
COPY infra/litellm/config.yaml /app/config.yaml
EXPOSE 4000
CMD ["--config", "/app/config.yaml", "--port", "4000"]
