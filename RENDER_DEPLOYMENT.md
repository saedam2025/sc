# Render 영구 저장 배포 확인

이 프로젝트의 업무 DB, 설정, 업로드 파일, 암호화 키는 모두 `DATA_DIR` 아래에 저장됩니다.
Render에서는 `render.yaml`이 `/mnt/data`에 5GB Persistent Disk를 연결하고
`DATA_DIR=/mnt/data`를 설정합니다.

Blueprint로 기존 서비스를 갱신할 때는 `render.yaml`의 `name` 값을 Render Dashboard에
표시된 기존 서비스 이름과 같게 맞춘 뒤 동기화해야 새 서비스를 별도로 만들지 않습니다.

기존 Dashboard 방식 서비스라면 **Disks** 메뉴에서 Persistent Disk의 Mount Path를
`/mnt/data`로 지정하고, **Environment**에서 `DATA_DIR=/mnt/data`를 설정해야 합니다.
디스크는 빌드 단계가 아니라 실행 단계에서 연결됩니다.

다음 자료가 함께 유지됩니다.

- `saedam.db`: 스마트명세서 작업그룹·광고이미지·자동적용 구분 문구·메일계정·발송이력, 증명서 회사·작업그룹·발급/발송 상태
- `saedam.db`: 스마트공문발송 회사정보·직인, 공문 템플릿, 수신자 목록, 발송 사용기록(첨부파일 포함), API 사용량(토큰 집계)도 같은 DB의
  `smart_document_companies`/`smart_document_templates`/`smart_document_recipients`/`smart_document_history`/`smart_document_deliveries`/`smart_document_attachments`
  테이블에 저장되므로, 동일한 Persistent Disk를 유지하는 일반 재배포에서는 등록·수정·삭제 내용이 함께 유지됩니다.
- `verified_contract/`: 인증전자계약 회사설정·메일계정·계약 제목/분류·약관·도장·서명·완료 PDF
- `security/`: 세션, 계정 비밀번호, 업로드 파일 암호화 키

Render 실행 중 실제 Persistent Disk가 확인되지 않으면 앱은 임시 저장소로 실행하지
않고 오류로 중단됩니다. 이는 정상 동작처럼 보이다가 다음 배포 때 설정이 사라지는 상황을
막기 위한 보호장치입니다. 긴급 진단 외에는 `ALLOW_EPHEMERAL_DATA_ON_RENDER`를 설정하지
마세요.

배포 로그에 아래처럼 표시되면 스마트명세서의 `새 작업그룹`, `광고 이미지`,
`자동적용 구분 문구`가 같은 Persistent Disk의 DB에 저장되는 상태입니다.

```text
영구 저장소 확인 완료: DB=/mnt/data/saedam.db, Persistent Disk=True
```

광고 이미지 파일 업로드도 별도 임시 폴더에 두지 않고 암호화된 값으로
`/mnt/data/saedam.db`의 `payroll_image_assets` 테이블에 저장됩니다. 작업그룹은 같은 DB의
`payroll_workgroups` 테이블, 자동적용 구분 문구는 `payroll_mail_templates.match_keywords`에
저장되므로 동일한 Persistent Disk를 유지한 일반 재배포에서는 등록·수정·삭제 내용이 함께
유지됩니다. 단, 기존 디스크를 삭제하거나 다른 새 디스크로 교체하면 이전 데이터는 자동으로
따라가지 않으므로 Render의 기존 `saedam-data` 디스크를 유지해야 합니다.

## 메신저 Web Push 환경변수

Render의 **Environment**에는 아래 세 값을 설정하고, 재배포할 때도 같은 키 쌍을 유지해야
합니다. 키 쌍을 새로 만들면 이미 알림을 켠 브라우저의 기존 구독은 무효가 됩니다.

- `VAPID_PUBLIC_KEY`: URL-safe Base64 형식의 공개키
- `VAPID_PRIVATE_KEY`: 위 공개키와 한 쌍인 비밀키
- `VAPID_SUBJECT`: `mailto:admin@saedam.org`처럼 연락 가능한 `mailto:` 또는 HTTPS URL

키를 변경한 뒤 첫 배포에서는 사용자가 인트라넷을 새로고침하면 클라이언트가 기존 키와
새 공개키를 비교해 해당 기기의 구독을 자동으로 다시 만듭니다. `registration failed - push
service error`가 특정 사내 PC에서만 계속되면 앱 서버가 아니라 그 PC의 Chrome/Edge 푸시
백엔드 연결 문제일 가능성이 높으므로 브라우저 업데이트·완전 재시작 후 사내 방화벽/프록시의
FCM 또는 WNS 차단 여부를 확인해야 합니다.

## 학부모 알림 환경변수

학부모 알림도 위의 VAPID 키를 함께 사용합니다. 이미 운영 중인 메신저 키를 유지하면 되며,
학부모 구독정보는 인트라넷 회원 구독과 별도 테이블에 저장됩니다.

- `PUBLIC_BASE_URL=https://works.saedam.org`: SMS와 강사 안내에 넣을 공개 주소
- `PARENT_SMS_WEBHOOK_URL`: 기존 문자 발송 서비스로 연결할 HTTPS 웹훅 주소
- `PARENT_SMS_WEBHOOK_TOKEN`: 웹훅 Bearer 토큰(필요한 경우)
- `PARENT_SMS_SENDER`: 등록된 문자 발신번호

SMS 웹훅에는 `POST` JSON으로 `to`, `from`, `text`를 전달하며 HTTP 2xx를 성공으로
판정합니다. 웹훅을 설정하지 않아도 관리화면에서 학부모·강사 링크와 완성된 문자 문구를
복사할 수 있지만, 실제 SMS 발송과 최초 발송 완료 처리는 하지 않습니다.

아이폰·아이패드의 Web Push는 학부모가 Safari에서 등록 페이지를 홈 화면에 추가한 뒤
그 아이콘으로 페이지를 열어 알림을 허용해야 합니다. Android Chrome과 데스크톱
Chrome/Edge는 등록 페이지에서 바로 허용할 수 있습니다.
