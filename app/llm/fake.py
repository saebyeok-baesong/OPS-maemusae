"""API 키 없이 흐름을 돌려보는 가짜 LLM. 테스트와 로컬 compose(LIGHT_MODEL=fake:light)가 쓴다.

responder(schema, messages)가 스키마 인스턴스를 돌려주면 LangChain의
with_structured_output(include_raw=True) 결과와 같은 모양으로 감싼다.
"""
from collections.abc import Callable

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from app.llm.schemas import Classification, FixProposal, RootCause, SecuritySummary

Responder = Callable[[type[BaseModel], list[BaseMessage]], BaseModel]


class _FakeRunnable:
    def __init__(self, name: str, schema: type[BaseModel], responder: Responder, calls: list):
        self.name, self.schema, self.responder, self.calls = name, schema, responder, calls

    def invoke(self, messages: list[BaseMessage]) -> dict:
        self.calls.append((self.name, self.schema.__name__))
        parsed = self.responder(self.schema, messages)
        tokens_in = sum(len(str(m.content)) for m in messages) // 4
        tokens_out = len(parsed.model_dump_json()) // 4
        raw = AIMessage(content="", usage_metadata={"input_tokens": tokens_in, "output_tokens": tokens_out,
                                                    "total_tokens": tokens_in + tokens_out})
        return {"raw": raw, "parsed": parsed, "parsing_error": None}


class FakeChatModel:
    def __init__(self, name: str, responder: Responder, calls: list):
        self.name, self.responder, self.calls = name, responder, calls

    def with_structured_output(self, schema: type[BaseModel], include_raw: bool = False, method: str = ""):
        return _FakeRunnable(self.name, schema, self.responder, self.calls)


def fake_model_factory(responder: Responder, calls: list) -> Callable[[str], FakeChatModel]:
    return lambda name: FakeChatModel(name, responder, calls)


def demo_responder(schema: type[BaseModel], messages: list[BaseMessage]) -> BaseModel:
    """알림 문구의 단서로 그럴듯한 답을 만든다. 실제 판단 품질과는 상관없다."""
    prompt = str(messages[-1].content)
    if schema is Classification:
        if "OOMKilled" in prompt:
            return Classification(category="infra", needs_code_fix=False, confidence=0.9,
                                  summary="메모리 한도 초과로 컨테이너가 종료됐다", keywords=["OOMKilled", "memory", "limits"])
        if "NotReady" in prompt:
            return Classification(category="dependency", needs_code_fix=False, confidence=0.85,
                                  summary="api가 DB에 연결하지 못해 준비 상태 확인이 실패한다",
                                  keywords=["NotReady", "database", "OperationalError", "연결"])
        if "CrashLoop" in prompt:
            return Classification(category="config", needs_code_fix=False, confidence=0.8,
                                  summary="기동 중 설정을 읽다 실패해 재시작을 반복한다", keywords=["CrashLoopBackOff", "Secret", "설정"])
        return Classification(category="code_bug", needs_code_fix=True, confidence=0.85,
                              summary="요청 처리 중 처리하지 못한 예외", keywords=["exception", "500", "traceback"])
    if schema is RootCause:
        return RootCause(root_cause="스택트레이스의 마지막 프레임에서 예외가 처리되지 않았다(가짜 LLM 응답)",
                         evidence=["스택트레이스 마지막 프레임"], suspected_area="스택트레이스의 모듈",
                         next_steps=["직전 배포의 변경을 확인한다", "같은 request_id의 로그를 본다"])
    if schema is FixProposal:
        # 가짜 LLM은 코드를 고치지 못한다. diff가 아니므로 수정안 없이 원인 분석으로 끝난다
        return FixProposal(patch="", explanation="가짜 LLM 응답", risk="low")
    if schema is SecuritySummary:
        return SecuritySummary(summary="새 결과를 심각도 순으로 확인한다(가짜 LLM 응답)", top_actions=["CRITICAL부터 처리한다"])
    raise ValueError(f"demo_responder: unknown schema {schema.__name__}")
