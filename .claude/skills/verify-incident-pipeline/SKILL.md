---
name: verify-incident-pipeline
description: 장애 대응 파이프라인의 순서 약속(장애 알림 한 번, 조용한 유실 없음, 롤백 버튼 동작, 롤백 실행 한 번, 승인이 running 에 걸리지 않음)을 TLA+ 모델과 장애 주입 테스트로 확인한다. app/worker.py process_analyze·plan_rollback·holding, app/core/queue.py, app/core/store.py 의 set_status·set_card·start_action·finish_action, app/rollback.py approve·_merge, app/approvals.py 를 바꿀 때 쓴다.
---

# 장애 대응 파이프라인 검증

워커가 죽거나, 한 워커만 Redis와 끊기거나, GitHub가 오류를 주거나, 두 사람이 동시에 승인을 눌러도 README "지키는 것"의 약속이 지켜지는지 봅니다. 장애와 검사의 전체 표는 `verify/incident-pipeline/README.md`에 있습니다. 먼저 그 문서를 읽습니다.

## 이 변경이 해당되는지

아래 표의 함수 본문, 그 안의 호출 순서, 또는 SQL(특히 `ON CONFLICT`, `result || %s`)이 바뀌면 해당됩니다. `git diff 5941eae -- <파일>`로 sync 커밋 뒤의 변경을 봅니다.

| 코드 | 모델(`IncidentPipeline.tla`) | 테스트(`tests/test_faults.py`) |
| --- | --- | --- |
| `app/worker.py` `process_analyze` | `Graph`부터 `Ack`까지 한 호출이 한 action. 실패 분기는 `SetFailed`, `FailRec`, `PostFail`, `FailCard`, `DeadLetter` 순서 | S1, F1, F2, F3, K1 |
| `app/worker.py` `run_graph` | `Graph`, `ckpt` | S1 |
| `app/worker.py` `plan_rollback` | `Plan` (`seenPR` 재사용, open_pr 실패) | S1, F3 |
| `app/worker.py` `holding`, `run_forever` | `HeartbeatLost`, `OwnerGone`, `Reclaim` | F2, F3 |
| `app/core/queue.py` `consume`, `reclaim_stale`, `keep_claimed`, `ack`, `dead_letter` | `Consume`, `Reclaim`, `Ack`, `DeadLetter` | 전부 |
| `app/core/store.py` `get`, `set_status`, `set_card` | `GetRec`, `SetStatus`, `SetCard` | S1, F2, F3, K1 |
| `app/core/store.py` `start_action`, `finish_action` | `Start`, `Finish` | S2, F4 |
| `app/rollback.py` `approve`, `_merge` | `Click`, `Start`, `Merge`, `Finish`, `CrashApprover` | S2, F4 |
| `app/approvals.py` `handle_click`, `approve_from_cli` | `Click` | S2 |

**Sync 커밋: `5941eae`** (feat/local-demo, F1 수정 포함). 이 커밋에서 위 코드를 모델과 한 줄씩 맞춰 봤습니다. 모델이 새 커밋의 코드를 그대로 그린다는 것을 확인한 뒤에만 이 값을 옮깁니다. 테스트가 통과했다는 것만으로는 옮기지 않습니다.

## 돌리기

```bash
./verify/incident-pipeline/run.sh                         # TLC 검사 11개, 1분 안쪽
.venv/bin/python -m pytest tests/test_faults.py -rxX      # docker compose up -d postgres 필요
```

- `run.sh`: TLC(tla2tools 1.7.4, Java 11 이상)를 쓰고, jar가 없으면 `~/.cache/tla/`에 받습니다. `TLA2TOOLS_JAR`로 다른 경로를 줄 수 있습니다. 줄마다 `ok`면 기대와 같고, `FAIL`이면 다릅니다. `run.sh F3`처럼 이름 일부로 하나만 돌릴 수 있습니다.
- 지금 기대값: S1, S2, F1은 통과합니다(F1은 2026-11-26에 고쳤습니다). F2~F4, K1은 지금 코드에서 깨지는 약속(발견)이라 해당 불변식이 깨지는 것이 기대값입니다. M1~M4는 망가뜨린 변형이라 깨져야 합니다.
- pytest 기대값: S1, S2, F1 통과, F2~F4와 K1은 `XFAIL`.

## 결과 읽기

TLC 결과는 pstack `/statechart` 스킬로 읽습니다. 반례(`out/<이름>.log`)는 가장 짧은 경로를 쉬운 말로 풀어 주고, 통과한 검사는 상태 이동 그림을 그려 줍니다.

`FAIL`이 나오면 다음 순서로 가립니다.

1. **반례를 코드 순서로 옮겨 봅니다.** trace의 action 이름은 위 표로 코드 호출에 대응합니다. 예: `GetRec(w2)` → `Plan(w1)` → `SetStatus(w1)` → `Post(w1)` → `Plan(w2)` 실패 → `SetStatus(w2)`는 F3입니다.
2. **실제 코드로 재현합니다.** 같은 순서를 `tests/test_faults.py`의 `kill_once`, `redeliver`, `fail_open_pr`, 스레드 끼워 넣기로 만듭니다. 실제 코드에서도 깨지면 진짜 버그입니다. 사용자에게 반례를 코드 말로 알리고, 불변식이나 모델을 약하게 고쳐 통과시키지 않습니다.
3. **실제 코드에서 안 깨지면 모델링 오류입니다.** 모델의 한 action이 코드에서는 둘이거나(그 사이에 죽을 수 있음), 반대로 코드에서 원자적인 것을 모델이 쪼갰거나, 코드에 있는 확인을 모델이 빠뜨린 경우입니다. 모델을 코드에 맞춥니다.
4. **pytest만 깨지면:** S1/S2가 실패하면 변경이 약속을 깼습니다. `XPASS(strict)`로 F/K 테스트가 실패하면 변경이 그 발견을 고쳤습니다. 아래 "고친 뒤"를 따릅니다.

## 의도한 변경일 때 모델 고치기

- 코드에 외부 호출(Redis, Postgres, Discord, GitHub)이 하나 늘면 모델에도 action을 하나 늘립니다. 워커는 그 사이에서 죽을 수 있어야 합니다(`Crash`는 `pc[w] # "idle"`인 어디서나 됩니다).
- 각 action 주석에 대응하는 코드 호출을 적습니다. 이 저장소의 이름(`set_card`, `rollback_pr`, `start_action`)을 씁니다.
- 새 약속은 불변식으로 더하고 `run.sh` `CHECKS`에 통과 검사 하나와, 그 약속을 지키는 코드 줄을 지운 변형(`Mutation`) 검사 하나를 더합니다. 변형 검사가 깨지는 것을 본 적이 없으면 그 불변식은 아무것도 증명하지 않습니다.
- `tests/test_faults.py`에 같은 장애를 실제 코드에 주입하는 테스트를 더하고, docstring에 주입한 장애, 재현하는 장애, 모델 검사 이름을 적습니다. `verify/incident-pipeline/README.md`의 두 표도 같이 고칩니다.

### 발견을 고친 뒤

F2~F4나 K1을 고치면 세 곳을 같이 바꿉니다(F1을 고친 방식이 예입니다: `PostFail`과 `FailCard`를 `DeadLetter` 앞으로 옮기고, 고치기 전 순서는 변형 `dlq_before_post`(M4)로 남겼습니다): 모델 action, `run.sh` `CHECKS`의 그 줄을 `pass` 기대로, 테스트의 `xfail`을 지웁니다. README 표의 기대도 고칩니다.

## 범위 밖

Discord 게시 실패(빈 문자열), `open_rollback_pr` 내부의 GitHub 호출 순서, `_merge`의 STALE/REJECTED 확인(`tests/test_rollback.py`가 봄), `ResolvedJob`, 알림 접수의 지문 중복 제거, 보안 보고는 모델에 없습니다. 이 부분을 바꾸는 변경에는 이 검증이 아무것도 말해 주지 않습니다.

검증이 코드와 어긋났는데 이번 변경에서 다시 맞추지 못했으면 `/pstack:maintain-formal-verification-skill`로 다시 맞춥니다.
