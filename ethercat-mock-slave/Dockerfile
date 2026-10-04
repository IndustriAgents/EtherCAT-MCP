FROM python:3.11-slim

WORKDIR /app

# The mock uses only the standard library, so installing the project is all
# it takes. No fallback: if the install fails, the build fails.
COPY pyproject.toml README.md ethercat_mock_slave.py ./
RUN pip install --no-cache-dir .

# Listen on all interfaces inside the container. run-container.sh publishes
# the port on the host's loopback only.
ENV MOCK_ETHERCAT_HOST=0.0.0.0 \
    MOCK_ETHERCAT_PORT=6700 \
    MOCK_ETHERCAT_UPDATE_INTERVAL=1.0 \
    MOCK_ETHERCAT_VERBOSE=false

EXPOSE 6700/tcp

USER nobody

CMD ["ethercat-mock-slave"]
