# 이미지 하나(ECR 저장소 ops), 진입점 여럿:
#   api          uvicorn app.main:app --host 0.0.0.0 --port 8000   (기본)
#   worker       python -m app.worker
#   approvals    python -m app.approvals
#   migrate      python -m app.db                 (배포 전 Job 또는 init container)
#   ingest-s3    python -m app.security.ingest_s3 (CronJob. CI 보안 스캔 결과를 S3에서 가져온다)
FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 임베딩 모델을 이미지에 넣는다. 런타임에 받으면 워커 Pod가 뜰 때마다 첫 장애가 늦어지고 외부 다운로드가 필요하다.
# 모델 이름은 app/config.py의 embedding_model 기본값과 같아야 한다
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')" \
 && chmod -R a+rX /opt/fastembed
ENV HF_HUB_OFFLINE=1

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .
COPY knowledge ./knowledge

RUN useradd -m -u 10001 runner
USER 10001

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
