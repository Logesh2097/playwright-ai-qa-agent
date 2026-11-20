# Browser runner: the official Playwright MCP image (browser included) plus a
# small supervisor that starts one server per session.
#
# The version is pinned on purpose. The tool permission table in
# backend/app/browser/policy.py and the options in supervisor.py were written
# against this exact version; upgrade them together and run `make be-test-browser`.
FROM mcr.microsoft.com/playwright/mcp:v0.0.86

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY supervisor.py /runner/supervisor.py

USER node
WORKDIR /home/node
ENV PYTHONUNBUFFERED=1
EXPOSE 8900
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/runner/supervisor.py"]
