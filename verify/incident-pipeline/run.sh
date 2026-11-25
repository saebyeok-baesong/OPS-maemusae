#!/usr/bin/env bash
# IncidentPipeline.tla 를 TLC 로 검사한다. 표의 줄마다 설정 하나를 만들어 돌리고, 결과가 기대와 같은지 본다.
#   ./verify/incident-pipeline/run.sh            전부
#   ./verify/incident-pipeline/run.sh F2         이름에 F2 가 들어간 것만
# 반례 trace 는 verify/incident-pipeline/out/<이름>.log 에 남는다.
set -euo pipefail
cd "$(dirname "$0")"

TLA_VERSION=1.7.4
TLA_SHA256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
JAR="${TLA2TOOLS_JAR:-$HOME/.cache/tla/tla2tools-$TLA_VERSION.jar}"
if [[ ! -f "$JAR" ]]; then
  mkdir -p "$(dirname "$JAR")"
  curl -fsSL -o "$JAR" "https://github.com/tlaplus/tlaplus/releases/download/v$TLA_VERSION/tla2tools.jar"
fi
got=$(shasum -a 256 "$JAR" | cut -d' ' -f1)
if [[ "$got" != "$TLA_SHA256" ]]; then
  echo "tla2tools.jar sha256 가 다르다: $got (기대 $TLA_SHA256)" >&2; exit 2
fi

# 이름 | 기대(pass 또는 깨질 불변식) | 검사할 불변식 | 장애와 변형(상수)
# 상수 기본값: MaxFaults 2, 나머지 FALSE, Mutation "none"
CHECKS=(
  # 지키는 약속: 주입한 장애가 있어도 통과해야 한다
  "S1-worker-crash|pass|TypeOK CardOnce ButtonWorks|PRFails=TRUE"
  "S2-double-click|pass|TypeOK ExecuteOnce|Partition=TRUE PRFails=TRUE ApproverCrash=TRUE CrashAfterPost=TRUE"
  # F1 은 고쳤다: 워커가 실패 경로 어디서 죽어도(게시와 set_card 사이 포함) 알림 없이 XACK 되지 않는다
  "F1-silent-drop|pass|TypeOK NoSilentDrop|PRFails=TRUE CrashAfterPost=TRUE"
  # 지금 코드에서 깨지는 것(발견)
  "F2-double-card|CardOnce|TypeOK CardOnce|Partition=TRUE"
  "F3-button-lost|ButtonWorks|TypeOK ButtonWorks|Partition=TRUE PRFails=TRUE"
  "F4-stuck-approval|NoStuckApproval|TypeOK NoStuckApproval|ApproverCrash=TRUE"
  "K1-crash-after-post|CardOnce|TypeOK CardOnce|CrashAfterPost=TRUE"
  # 검사가 실패할 수 있다는 증거: 코드를 망가뜨린 변형은 S1, S2 와 같은 장애에서 깨져야 한다
  "M1-no-card-check|CardOnce|TypeOK CardOnce ButtonWorks|PRFails=TRUE Mutation=\"no_card_check\""
  "M2-no-pr-reuse|ButtonWorks|TypeOK CardOnce ButtonWorks|PRFails=TRUE Mutation=\"no_pr_reuse\""
  "M3-no-unique-action|ExecuteOnce|TypeOK ExecuteOnce|Mutation=\"no_unique_action\""
  "M4-dlq-before-post|NoSilentDrop|TypeOK NoSilentDrop|PRFails=TRUE CrashAfterPost=TRUE Mutation=\"dlq_before_post\""
)

mkdir -p out
fail=0
for row in "${CHECKS[@]}"; do
  IFS='|' read -r name expect invariants overrides <<<"$row"
  [[ $# -gt 0 && "$name" != *"$1"* ]] && continue
  cfg="out/$name.cfg"
  {
    echo "SPECIFICATION Spec"
    echo "CONSTANTS Workers = {w1, w2}  Approvers = {a1, a2}"
    for kd in MaxFaults=2 CrashAfterPost=FALSE Partition=FALSE PRFails=FALSE ApproverCrash=FALSE 'Mutation="none"'; do
      k=${kd%%=*} v=${kd#*=}
      for kv in $overrides; do [[ ${kv%%=*} == "$k" ]] && v=${kv#*=}; done
      echo "CONSTANT $k = $v"
    done
    for inv in $invariants; do echo "INVARIANT $inv"; done
  } >"$cfg"
  log="out/$name.log"
  java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC -config "$cfg" -workers auto -deadlock -cleanup \
    -metadir "out/states-$name" IncidentPipeline.tla >"$log" 2>&1 || true
  if grep -q "Model checking completed. No error has been found" "$log"; then
    got=pass
  elif v=$(grep -oE "Invariant [A-Za-z]+ is violated" "$log" | head -1); then
    got=$(awk '{print $2}' <<<"$v")
  else
    got="error(로그 확인: $log)"
  fi
  states=$(grep -oE "[0-9,]+ distinct states found" "$log" | tail -1 | cut -d' ' -f1)
  if [[ "$got" == "$expect" ]]; then
    printf 'ok    %-22s %-16s %s states\n' "$name" "$got" "${states:-?}"
  else
    printf 'FAIL  %-22s 기대 %s, 결과 %s (%s)\n' "$name" "$expect" "$got" "$log"
    fail=1
  fi
done
exit $fail
