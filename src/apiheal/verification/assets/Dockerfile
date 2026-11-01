FROM python:3.11-slim
ARG OPENAI_SDK_VERSION
ARG APIHEAL_HARNESS_SHA256
RUN python -m pip install --no-cache-dir --only-binary=:all: \
    "openai==${OPENAI_SDK_VERSION}" "pytest==8.4.2" "packaging==25.0"
COPY harness.py /opt/apiheal/harness.py
COPY pytest.ini /opt/apiheal/pytest.ini
LABEL org.apiheal.sdk="${OPENAI_SDK_VERSION}" \
      org.apiheal.harness-sha256="${APIHEAL_HARNESS_SHA256}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 HOME=/tmp
USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/apiheal/harness.py"]
