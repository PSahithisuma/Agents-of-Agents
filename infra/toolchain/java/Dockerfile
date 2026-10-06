# =============================================================================
# Java/Spring Boot Toolchain Image
# Used by Cloud Build QA Gate for Java jobs
#
# Contains: Temurin JDK 21, Maven 3, Semgrep, Trivy, gitleaks, Flyway
# Built once, pushed to Artifact Registry vibeforge-docker
# =============================================================================

FROM eclipse-temurin:21-jdk-alpine AS base

LABEL org.opencontainers.image.title="VibeForge Java Toolchain"
LABEL org.opencontainers.image.version="21"

# ── System tools ──────────────────────────────────────────────────────────────
RUN apk add --no-cache \
    bash \
    curl \
    git \
    python3 \
    py3-pip \
    bc \
    unzip \
    wget

# ── Maven 3.9 ─────────────────────────────────────────────────────────────────
ENV MAVEN_VERSION=3.9.6
ENV MAVEN_HOME=/opt/maven
RUN wget -q "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
    && tar -xzf apache-maven-${MAVEN_VERSION}-bin.tar.gz -C /opt/ \
    && mv /opt/apache-maven-${MAVEN_VERSION} ${MAVEN_HOME} \
    && ln -s ${MAVEN_HOME}/bin/mvn /usr/local/bin/mvn \
    && rm apache-maven-${MAVEN_VERSION}-bin.tar.gz

# ── Semgrep ───────────────────────────────────────────────────────────────────
RUN pip3 install --no-cache-dir semgrep

# ── Trivy ─────────────────────────────────────────────────────────────────────
RUN wget -q https://github.com/aquasecurity/trivy/releases/latest/download/trivy_$(uname -m).tar.gz \
    -O /tmp/trivy.tar.gz 2>/dev/null || \
    wget -q "https://github.com/aquasecurity/trivy/releases/download/v0.50.0/trivy_0.50.0_Linux-64bit.tar.gz" \
    -O /tmp/trivy.tar.gz \
    && tar -xzf /tmp/trivy.tar.gz -C /usr/local/bin trivy \
    && rm /tmp/trivy.tar.gz

# ── gitleaks ──────────────────────────────────────────────────────────────────
RUN wget -q "https://github.com/gitleaks/gitleaks/releases/download/v8.18.0/gitleaks_8.18.0_linux_x64.tar.gz" \
    -O /tmp/gitleaks.tar.gz \
    && tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks \
    && rm /tmp/gitleaks.tar.gz

# ── Maven settings.xml — points to Artifact Registry (replaces Nexus) ─────────
# Cloud Build substitutes ${_REGION} and ${PROJECT_ID} at runtime
COPY settings.xml /settings.xml

# ── Warm Maven local repo with Spring Boot BOM ────────────────────────────────
# This runs at image BUILD time so the cache is baked in
# Result: Cloud Build jobs start with dependencies already cached
COPY warmup/pom.xml /warmup/pom.xml
RUN mvn -B dependency:go-offline \
        -f /warmup/pom.xml \
        -s /settings.xml \
        -Dmaven.repo.local=/root/.m2 \
        2>&1 || true   # tolerate network failure during image build

ENV JAVA_HOME=/opt/java/openjdk
ENV M2_HOME=${MAVEN_HOME}
WORKDIR /workspace
