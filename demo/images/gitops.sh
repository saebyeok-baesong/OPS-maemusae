#!/bin/sh
# 로컬 INFRA 레포(bare git) 도구. GitHub의 INFRA-watch-nyang과 같은 gitops/apps/images.yaml을 담는다.
#   gitops.sh init <api 이미지>                 처음 한 번. 이미 있으면 그대로 둔다
#   gitops.sh deploy <api 이미지> <커밋 메시지>  CI 이미지 PR이 merge된 것처럼 main에 커밋한다
#   gitops.sh current                           main의 api 이미지
set -eu
GIT_DIR=/gitops/infra.git
FILE=gitops/apps/images.yaml
git() { command git -c safe.directory='*' -c user.name=ci-image-pr-bot -c user.email=ci@localhost "$@"; }

render() {
  cat <<YAML
# 앱 이름: 저장소@sha256:digest (SPEC contracts/images-yaml.md). 로컬 데모 저장소다
# 앱 줄 사이 빈 줄은 지우지 않는다

api: $1
YAML
}

commit() {
  work=$(mktemp -d)
  git clone -q "$GIT_DIR" "$work"
  mkdir -p "$work/$(dirname $FILE)"
  render "$1" > "$work/$FILE"
  git -C "$work" add "$FILE"
  if git -C "$work" diff --cached --quiet; then
    echo "이미 $1 이다. 커밋하지 않았다"
  else
    git -C "$work" commit -q -m "$2"
    git -C "$work" push -q origin HEAD:main
    echo "main <- $2"
  fi
  rm -rf "$work"
}

case "${1:-}" in
  init)
    if [ -d "$GIT_DIR/refs" ]; then echo "gitops 저장소가 이미 있다"; exit 0; fi
    # api, worker, approvals(uid 10001)와 이 도구(root)가 같은 저장소에 쓴다
    git init -q --bare --shared=0777 -b main "$GIT_DIR"
    chmod 0777 "$GIT_DIR"
    commit "$2" "chore: api 첫 배포"
    ;;
  deploy) commit "$2" "$3" ;;
  current) git --git-dir="$GIT_DIR" show "main:$FILE" | sed -n 's/^api:[[:space:]]*//p' ;;
  *) echo "usage: gitops.sh init|deploy|current" >&2; exit 2 ;;
esac
