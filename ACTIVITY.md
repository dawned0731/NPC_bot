# 활동 일지

- `/퀘스트`를 `/활동`으로 변경했습니다. 봇 시작 시 기존 전역 동기화로 반영됩니다.
- 출석 1,200 XP, 채팅 30회 완료 300 XP, 5명 이상 음성방 15분마다 150 XP입니다. 채팅/음성의 일반 활동 경험치는 별도이며 기존 집계 조건은 유지합니다.
- 출석, 채팅 진행도, 다음 음성 보상까지의 시간, 오늘 음성 보상 횟수, 다음 레벨까지의 XP를 표시합니다. 조회 자체는 보상을 지급하지 않습니다.
- 정규 시즌 외에는 경험치 지급 중단 안내를 표시합니다. 변경 전 이미 지급된 보상은 소급 조정하지 않습니다.
- 서버의 `current_season_type`에 맞춰 봄/여름/가을/겨울 배너와 임베드 색상을 선택합니다. 값이 없으면 달력 계절을 사용합니다. 이전에 전송된 메시지는 자동 수정하지 않으며 새 명령 실행에 반영합니다.
- 신입 소개 DM, 명령어 DM, DM 실패 시 스레드 안내는 모두 `/활동`을 안내합니다. 이미 전송된 DM은 다시 보내지 않습니다.
- 채팅 완료 알림은 기존 붓터치 이미지 대신 실제 지급 XP를 보여주는 계절색 임베드를 사용합니다.

## 배너 제작

내장 image_gen 도구로 제작한 PNG 원본을 `assets/activity/{spring,summer,fall,winter}.png`에 저장했습니다. 2172×724, 글자 없는 3:1 풍경 배너입니다. Discord 첨부 파일로 전송하므로 외부 이미지 호스팅이 필요 없습니다.

공통 생성 프롬프트:

Create one finished wide decorative Discord community banner, approximately 3:1 landscape, for Korean community '사계절, 그 사이'. No text, no lettering, no logos, no watermark. Premium calm editorial gouache and paper-cut landscape illustration, subtle paper grain, refined restrained palette, layered curved hills along lower third, botanical framing on left and right edges, broad serene open center. Cozy sociable atmosphere, polished contemporary design, not childish, no UI or borders. [SEASON]. Fill the whole rectangular canvas. This is one of four matching seasonal banners with same balanced composition.

계절별 [SEASON]:

- SPRING: pale blush cherry blossoms, fresh sage leaves, soft cream pink sky
- SUMMER: turquoise water ripples, verdant leaves, pale aqua sky
- AUTUMN: amber maple leaves, terracotta hills, pale apricot sky
- WINTER: frosted pine branches, soft lavender snow hills, silver blue sky
