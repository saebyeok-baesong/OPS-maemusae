#!/bin/sh
# Argo CD 대역. 로컬 gitops 저장소 main의 images.yaml api 줄이 가리키는 이미지로 api 컨테이너를 맞춘다.
# docker 소켓을 쓰므로 데모 전용이다. OPS 컨테이너에는 소켓을 주지 않는다
set -u
GIT_DIR=/gitops/infra.git
NAME=watchnyang-api
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) deployer $*"; }

while true; do
  ref=$(git -c safe.directory='*' --git-dir="$GIT_DIR" show main:gitops/apps/images.yaml 2>/dev/null \
        | sed -n 's/^api:[[:space:]]*\([^[:space:]]*\).*/\1/p')
  if [ -n "$ref" ]; then
    want="sha256:${ref#*@sha256:}"
    have=$(docker inspect -f '{{.Image}}' "$NAME" 2>/dev/null || true)
    running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || echo false)
    if [ "$want" != "$have" ] || [ "$running" != true ]; then
      log "sync api -> $ref"
      docker rm -f "$NAME" >/dev/null 2>&1
      if docker run -d --name "$NAME" --network watchnyang-demo --network-alias api -p 18080:8000 \
           --env-file /demo/service.env --memory 512m \
           --label demo.logs=true --label demo.namespace=maemusae --label demo.app=api \
           "$want" >/dev/null; then
        log "deployed api ${want#sha256:}"
      else
        log "docker run failed for $ref"
      fi
    fi
  fi
  sleep 2
done
