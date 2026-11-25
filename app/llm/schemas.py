"""LLM 구조화 출력. 자유 텍스트 대신 스키마로 받아야 다음 노드가 안정적으로 쓴다."""
from typing import Literal

from pydantic import BaseModel, Field


class Classification(BaseModel):
    category: Literal["code_bug", "config", "infra", "dependency", "data", "unknown"]
    needs_code_fix: bool = Field(description="애플리케이션 코드 수정이 필요한 장애인지")
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(description="한 줄 요약")
    keywords: list[str] = Field(default_factory=list, description="런북 검색에 쓸 핵심 키워드")


class RootCause(BaseModel):
    root_cause: str
    evidence: list[str] = Field(default_factory=list)
    suspected_area: str = Field(description="의심되는 모듈, 엔드포인트, 설정 이름")
    next_steps: list[str] = Field(default_factory=list, description="담당자가 할 일. 코드 수정 방향 포함")
    suspected_files: list[str] = Field(default_factory=list, description="고칠 파일. 제공된 코드의 ### 경로 그대로")


class FixProposal(BaseModel):
    patch: str = Field(description="소스 디렉터리 기준 unified diff (--- a/경로, +++ b/경로)")
    explanation: str
    risk: Literal["low", "medium", "high"] = "medium"


class SecuritySummary(BaseModel):
    summary: str = Field(description="오늘 보안 결과를 세 줄 안으로")
    top_actions: list[str] = Field(default_factory=list, description="먼저 처리할 항목, 최대 5개")
