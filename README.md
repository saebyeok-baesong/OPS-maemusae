# OPS-maemusae

매무새의 AI 기반 장애 대응 자동화입니다. Alertmanager 경보를 받아 분류하고, 원인을 분석해 Discord 장애 알림으로 알리고, 승인되면 롤백합니다.

- 입력은 Alertmanager webhook입니다. payload 예시는 [SPEC fixtures](https://github.com/saebyeok-baesong/SPEC-maemusae/tree/main/fixtures)에 둡니다.
- 롤백은 INFRA 레포에 이전 이미지 digest로 되돌리는 PR을 여는 것입니다. 클러스터를 직접 바꾸지 않습니다.

결정 배경은 [SPEC adr/0001](https://github.com/saebyeok-baesong/SPEC-maemusae/blob/main/adr/0001-repo-structure.md)을 보세요.
