# Runs coldcall_agent.py (the LiveKit worker) + scheduler_poller.py (Airtable
# schedule polling) together — see entrypoint.sh. One image serves either
# campaign; which one is decided entirely by the CAMPAIGN env var set on the
# Render service, not by anything baked in here.
#
# PII note: leads-us.json / leads-filtered-by-region.json (real names,
# phones, emails) are baked into this image below. The Docker Hub repo this
# gets pushed to MUST be private — never push this image to a public repo.
FROM python:3.12-slim

# onnxruntime (pulled in via livekit's turn-detector/silero VAD) needs
# libgomp on Debian slim base images, not present by default.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependencies first, separate layer, so code-only changes don't reinstall.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Application code — only what's needed at runtime, not the one-off local
# setup/diagnostic scripts (setup_*.py, trust_hub_*.py) or docs.
COPY coldcall_agent.py coldcall_prompt.py dialer.py scheduler_poller.py ./

# Leads lists — private-repo-only, see note above.
COPY leads/us/leads-us.json leads/us/leads-us.json
COPY leads/france/leads-filtered-by-region.json leads/france/leads-filtered-by-region.json

COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
