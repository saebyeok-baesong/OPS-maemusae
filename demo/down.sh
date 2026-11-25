#!/usr/bin/env bash
# 데모를 모두 내리고 볼륨(gitops 저장소, 두 DB)까지 지운다. 이미지는 남긴다
. "$(dirname "$0")/lib.sh"
# api 컨테이너는 compose가 아니라 deployer가 만들었다
dc stop deployer >/dev/null 2>&1 || true
docker rm -f watchnyang-api >/dev/null 2>&1 || true
COMPOSE_PROFILES=discord dc down -v --remove-orphans
