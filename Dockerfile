FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY litellm_roi ./litellm_roi
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 roi && mkdir /data && chown roi:roi /data
USER roi
ENV ROI_DATA_DIR=/data
EXPOSE 8787
CMD ["litellm-roi", "--host", "0.0.0.0", "--no-browser"]
