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

- `saedam.db`: 스마트명세서 작업그룹·광고이미지·메일계정·발송이력, 증명서 회사·작업그룹·발급/발송 상태
- `verified_contract/`: 인증전자계약 회사설정·메일계정·계약 제목/분류·약관·도장·서명·완료 PDF
- `security/`: 세션, 계정 비밀번호, 업로드 파일 암호화 키

Render 실행 중 실제 Persistent Disk가 확인되지 않으면 앱은 임시 저장소로 실행하지
않고 오류로 중단됩니다. 이는 정상 동작처럼 보이다가 다음 배포 때 설정이 사라지는 상황을
막기 위한 보호장치입니다. 긴급 진단 외에는 `ALLOW_EPHEMERAL_DATA_ON_RENDER`를 설정하지
마세요.
