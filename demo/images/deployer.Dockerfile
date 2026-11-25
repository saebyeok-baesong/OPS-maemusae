# 데모 전용 배포기(Argo CD 대역)와 로컬 gitops 저장소 도구. docker CLI와 git만 있으면 된다
FROM docker:29-cli
RUN apk add --no-cache git
COPY deployer.sh gitops.sh /usr/local/bin/
ENTRYPOINT []
CMD ["deployer.sh"]
