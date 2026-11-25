# 데모 전용 "나쁜 배포" v2. v1 이미지에 회귀 버그 하나를 심는다. SERVICE 레포는 바꾸지 않는다
ARG BASE=watchnyang-api:v1
FROM ${BASE}
USER root
COPY break-terms.py /tmp/break-terms.py
RUN python /tmp/break-terms.py /app/app/users/service.py && rm /tmp/break-terms.py
USER app
