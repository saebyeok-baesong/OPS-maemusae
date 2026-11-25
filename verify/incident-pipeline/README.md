# 장애 대응 파이프라인 검증

README "지키는 것"의 약속 중 순서가 꼬일 때 깨지는 것을 두 가지로 확인합니다.

- `IncidentPipeline.tla`: 워커 큐 처리(`app/worker.py` `process_analyze`)와 롤백 승인(`app/rollback.py` `approve`)의 TLA+ 모델입니다. TLC가 워커 둘, 승인자 둘, 장애 최대 2개 안에서 가능한 모든 순서를 봅니다.
- `tests/test_faults.py`: 모델이 찾은 순서를 실제 코드에 장애를 주입해 다시 일으킵니다. 지금 깨지는 약속은 `xfail(strict=True)`입니다.

```bash
./verify/incident-pipeline/run.sh            # TLC, 검사 11개. 처음에는 tla2tools.jar 1.7.4 를 받는다
.venv/bin/python -m pytest tests/test_faults.py -rxX
```

`run.sh`는 검사마다 기대 결과(통과 또는 깨질 불변식)와 실제 결과를 비교해 `ok`/`FAIL`을 찍습니다. 반례는 `out/<검사 이름>.log`에 남습니다.

## 약속(불변식)

| 불변식 | 한 문장 | 깨지면 |
| --- | --- | --- |
| `CardOnce` | 장애 하나에 장애 알림은 하나만 올라간다 | 같은 장애 알림이 둘 올라가 롤백 버튼도 둘이 된다 |
| `NoSilentDrop` | 큐가 메시지를 XACK 하면 장애 알림이 올라가 있다 | 접수된 장애가 Discord에 아무것도 남기지 않는다 |
| `ButtonWorks` | 올라간 롤백 버튼이 가리키는 장애 기록에는 `rollback_pr`이 있다 | 버튼을 눌러도 "실행할 수 있는 조치가 없습니다"가 나온다 |
| `ExecuteOnce` | 장애 하나의 롤백 `_merge`는 한 번만 돈다 | 동시에 누른 두 사람이 둘 다 merge를 시도한다 |
| `NoStuckApproval` | `actions`가 `running`이면 그 승인은 아직 돌고 있다 | 승인이 영원히 "실행 중"에 걸려 다시 누를 수 없다 |

## 주입하는 장애

| 장애 | 모델 | 실제 코드에서 주입하는 방법(`tests/test_faults.py`) | 현실에서 생기는 경우 |
| --- | --- | --- | --- |
| 워커 강제 종료 | `Crash(w)`: `process_analyze`의 외부 호출 사이 어디서든 | `kill_once`로 `store.get`, `infra.open_rollback_pr`, `store.set_status`, `queue.ack`, `store.set_card`, `discord.post` 중 하나에서 `WorkerKilled`(BaseException) | kill -9, OOMKilled, 노드 종료, `queue.ack`의 `RedisConnectionError` |
| 붙잡기(heartbeat) 끊김 | `HeartbeatLost(w)`: 워커는 계속 도는데 메시지가 회수 대상이 된다 | `reclaim_stale("w2", min_idle_ms=0)`으로 w2가 회수하고, w1이 처리하는 중간에 w2를 끼워 넣는다(F2는 재진입, F3는 스레드) | 그 워커만 Redis와 15분(`queue_claim_idle_ms`) 넘게 끊김. `holding()`은 실패를 경고로만 남긴다 |
| GitHub 오류 | `Plan(w)`의 실패 분기 | `fail_open_pr`: `open_rollback_pr`가 502 `HTTPStatusError` | GitHub 5xx, 동시 PUT의 409 |
| LLM 오류 | `Graph(w)`의 실패 분기 | `Nodes.classify`가 `RuntimeError("LLM 503")` | OpenAI 5xx, 시간 초과 |
| 승인 봇 강제 종료 | `CrashApprover(a)`: `start_action` 뒤, `finish_action` 전 | `infra.current_images`에서 `WorkerKilled` | approvals Pod 재시작, OOM |
| 동시 클릭 | 승인자 둘이 아무 순서로 | `threading.Barrier`로 두 스레드가 같은 순간에 `approve` | 두 팀이 같은 알림의 버튼을 같이 누름 |

## 검사와 재현하는 장애

| 검사 | 기대 | 주입 | 재현하는 장애 | pytest |
| --- | --- | --- | --- | --- |
| S1 | 통과 | 워커 강제 종료(게시와 `set_card` 사이 제외), GitHub 오류 | 배포 직후 api 예외를 분석하던 워커가 재시작돼도 알림은 하나, 버튼은 merge된다 | `test_S1_*` |
| S2 | 통과 | 전부(동시 클릭, 승인 봇 종료, 붙잡기 끊김 포함) | 두 사람이 동시에 눌러도 `_merge`는 한 번 | `test_S2_*` |
| F1 | 통과(2026-11-26 고침) | LLM 오류 + 실패 경로 어디서든 워커 종료(게시와 `set_card` 사이 포함) | 고치기 전에는 `dead_letter`가 XACK한 뒤에 실패 알림을 올려서, 그 사이에 죽으면 장애가 DLQ에만 가고 Discord에는 아무것도 없었다. 지금은 알림을 올리고 `set_card`로 남긴 뒤 DLQ로 보낸다 | `test_F1_*` |
| F2 | `CardOnce` 깨짐 | 붙잡기 끊김 | 두 워커가 같이 처리하고 둘 다 `card_message_id`가 없다고 읽어 알림이 둘 올라간다 | `test_F2_*` (xfail) |
| F3 | `ButtonWorks` 깨짐 | 붙잡기 끊김 + GitHub 오류 | w2가 기록을 읽고, w1이 PR을 열어 버튼 알림을 올린 뒤, w2의 PR 열기가 실패해 `rollback_pr: null`로 덮어쓴다. 버튼을 누르면 `NO_PROPOSAL` | `test_F3_*` (xfail) |
| F4 | `NoStuckApproval` 깨짐 | 승인 봇 강제 종료 | `actions`가 `running`으로 남아 다시 눌러도 `ALREADY`. 롤백은 사람이 INFRA PR을 직접 merge해야 한다 | `test_F4_*` (xfail) |
| K1 | `CardOnce` 깨짐 | 게시와 `set_card` 사이 워커 종료 | 알림이 두 번 간다. `worker.py` 주석이 받아들인 틈이지만 README는 "한 번만"이라고 쓴다 | `test_K1_*` (xfail) |
| M1 | `CardOnce` 깨짐 | S1과 같음, 코드 변형 `no_card_check` | `if not record.card_message_id` 를 지우면 다시 처리할 때마다 알림이 또 간다 | S1 ack 경우가 실패 |
| M2 | `ButtonWorks` 깨짐 | S1과 같음, 코드 변형 `no_pr_reuse` | `plan_rollback`의 `previous.get("rollback_pr")` 재사용을 지우면 다시 처리할 때 GitHub 오류가 버튼을 죽인다 | `test_S1_redelivery_*` 실패 |
| M3 | `ExecuteOnce` 깨짐 | 동시 클릭, 코드 변형 `no_unique_action` | `start_action`이 unique 충돌을 무시하면 두 승인이 모두 `_merge`를 돈다 | `test_S2_*` 실패 |
| M4 | `NoSilentDrop` 깨짐 | F1과 같음, 코드 변형 `dlq_before_post` | 고치기 전 순서(`dead_letter` 먼저, 게시 나중)로 되돌리면 조용한 유실이 다시 생긴다 | 고치기 전 `worker.py`에서 `test_F1_*[discord.post]` 실패 |

M1~M4는 검사가 실패할 수 있다는 증거입니다. 2026-11-26에 실제 코드를 같은 모양으로 망가뜨린 사본(M4는 고치기 전 `worker.py`)에서 `tests/test_faults.py`를 돌려 각각 위 테스트가 실패하는 것을 확인했습니다.

## 범위

- 장애 하나, 큐 메시지(AnalyzeJob) 하나, 워커 둘, 승인자 둘, 주입 장애는 합쳐서 2개까지입니다.
- Discord 게시는 늘 성공한다고 봅니다. 실제 `DiscordClient.post`는 실패하면 빈 문자열을 돌려주고 `set_card`를 건너뛰므로, 다시 처리하면 알림이 또 갈 수 있습니다. 모델 밖입니다.
- `open_rollback_pr`는 같은 branch면 같은 PR을 돌려준다고(멱등) 봅니다. 그 안의 GitHub 호출 순서는 보지 않습니다.
- GitHub merge는 이미 merge된 PR을 거절한다고 봅니다. `_merge`의 이미지와 PR 모양 확인(STALE, REJECTED)은 `tests/test_rollback.py`가 봅니다.
- `ResolvedJob`(해소 답글), 알림 접수(`app/main.py` 지문 중복 제거), 보안 보고는 모델 밖입니다.
- LangGraph 체크포인트는 "끝남/아님" 둘로만 봅니다. 노드 단위 재개는 `test_killed_worker_resumes_from_the_checkpoint`가 봅니다.
