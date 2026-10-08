# Clean-container Cardkingdom probe. Build context must contain ONLY
# acceptance_cardkingdom_container.py (see README.md); no datasets or credentials.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0

# The base image ships version-matched browsers in /ms-playwright but not the
# Python package for the system interpreter.
RUN pip install --no-cache-dir playwright==1.63.0

COPY acceptance_cardkingdom_container.py /probe/acceptance_cardkingdom_container.py

# Non-root so Chromium keeps its sandbox (run with seccomp_profile.json).
USER pwuser
WORKDIR /home/pwuser

ENTRYPOINT ["python", "/probe/acceptance_cardkingdom_container.py", "--output", "/evidence"]
