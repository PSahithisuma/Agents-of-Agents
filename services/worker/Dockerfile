FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build context = project root; requirements are in services/worker/
# JDK 21 + Maven for the generator's compile gate
COPY --from=eclipse-temurin:21-jdk /opt/java/openjdk /opt/java/openjdk
COPY --from=maven:3.9-eclipse-temurin-21 /usr/share/maven /usr/share/maven
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH=/opt/java/openjdk/bin:/usr/share/maven/bin:$PATH
COPY services/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Security scan tools: Semgrep in its own venv (no clash with pinned libs) and the gitleaks binary
RUN python -m venv /opt/semgrep && /opt/semgrep/bin/pip install --no-cache-dir "semgrep>=1.90,<2" \
    && ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep \
    && curl -sSL https://github.com/gitleaks/gitleaks/releases/download/v8.18.4/gitleaks_8.18.4_linux_x64.tar.gz \
       | tar -xz -C /usr/local/bin gitleaks \
    && curl -fsSL https://github.com/aquasecurity/trivy/releases/download/v0.75.0/trivy_0.75.0_Linux-64bit.tar.gz \
       | tar -xz -C /usr/local/bin trivy \
    && mkdir -p /opt/trivy-cache

# Worker application code
COPY services/worker/ .

# agents/ package lives at project root (mounted as volume in Docker Compose;
# baked into the image for Cloud Run where no volume mounts exist)
COPY agents/ /project/agents/
ENV AGENTS_ROOT=/project

CMD ["python", "run.py"]

