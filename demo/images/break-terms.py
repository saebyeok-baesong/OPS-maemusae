"""GET /api/auth/terms가 없는 약관 코드를 찾다가 KeyError로 500을 내게 바꾼다. 원본 모양이 다르면 빌드를 멈춘다."""
import sys
from pathlib import Path

path = Path(sys.argv[1])
before = "def list_terms(db: Session) -> list[Term]:\n    return repository.list_current_terms(db)\n"
after = ("def list_terms(db: Session) -> list[Term]:\n"
         "    terms = repository.list_current_terms(db)\n"
         "    current = {term.code: term for term in terms}\n"
         "    return [current[\"TERMS_OF_SERVICE_V2\"], *terms]\n")
text = path.read_text()
if before not in text:
    sys.exit(f"{path}: list_terms 모양이 달라 회귀 버그를 심지 못했다. demo/images/break-terms.py를 SERVICE 코드에 맞춘다")
path.write_text(text.replace(before, after))
