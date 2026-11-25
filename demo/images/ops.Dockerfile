# OPS 이미지에 git만 더한다. INFRA_REPO_BACKEND=local-git이 git CLI로 로컬 저장소를 읽고 쓴다. 운영 이미지에는 넣지 않는다
ARG BASE=watchnyang-ops:base
FROM ${BASE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
USER 10001
