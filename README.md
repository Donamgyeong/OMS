# OMS · 초과 근무 사전 승인 시스템

Synology DS423에서 실행하기 위한 Django 기반 초과 근무 사전 승인 시스템.

## 구성

- Python 3.12 / Django 5.2 LTS / PostgreSQL 17
- Django Templates / HTMX 2.0.8 / Bootstrap 5.3.8 / 자체 CSS / Gunicorn / WhiteNoise
- ARM64 앱 이미지 / Container Manager Compose / DSM HTTPS 리버스 프록시
- HTMX·Bootstrap은 라이선스와 함께 로컬 정적 자산으로 포함한다. 외부 CDN 없이 초기 화면을 제공하며 HTMX 상호작용은 업무 화면 구현 시 확장한다. 원본 재취득은 `python scripts/fetch_frontend.py`로 수행한다.

## 구현 범위

- 사용자·부서·부서장·지정 승인자·정책·휴일·사용자별 소정근무 기준 관리
- 직원용 계획 입력, 제출 전 계산, 임시 저장·제출·회수·수정·재기안·취소
- 월별 누적 계산, 기준 이내 무승인 확정, 기준 초과 시 지정 부서장 결재함으로 전달
- 승인·반려, 사유 및 변경 전후 이력, 시스템 내 결과 알림
- 실제 출퇴근 수동 입력, 소정근무·휴일에 따른 초과 근무 자동 계산, 계획 연결·차이 조회
- 월·부서·사용자·계획 상태·실적 등록 여부 검색, 개인·부서·전체 조회 범위
- DB 연결을 확인하는 `/health/`, 컨테이너 영구 볼륨, 운영 보안 설정

기획의 2단계 핵심 기능을 구현했다. PostgreSQL 사용자 행 잠금으로 동시 제출·결재를 직렬화하고, 버전 확인으로 오래된 화면의 수정과 중복 결재를 막는다. 승인 대기 시간을 누적에 포함하며 정책·누적·소속·승인자와 변경 이력을 보존한다. 야간·월 경계 계획은 일별·월별 배분하며, 휴게 시간은 근무 구간 끝에서 차감한다.

일반 관리자 역할은 전체 조회 및 `/setup/` 기초정보 관리 권한이다. 결재는 해당 계획의 지정 승인자만 가능하며 자기 결재는 차단한다. Django Admin은 슈퍼유저 전용이고 업무 데이터는 읽기 전용이다. 업무 입력은 직원 화면에서 수행한다.

기간별 조직·근무 유형 관리, 대결·마감, 엑셀 다운로드, 장기 미처리 알림, 외부 API·사내 로그인·메신저 연동, 로그인 시도 제한은 후속 범위다. 상세 사용법과 현재 입력 기준은 [docs/core-workflow.md](docs/core-workflow.md)를 참고한다.

## Docker 개발 환경

Docker Desktop 또는 Docker Engine과 Compose를 사용한다. 호스트 Python 설치는 필요 없다. 운영 Compose와 DB 볼륨을 분리한 개발 설정이다.

```sh
docker compose -p oms-dev -f compose.dev.yaml up -d --build --wait
docker compose -p oms-dev -f compose.dev.yaml exec app python manage.py migrate
docker compose -p oms-dev -f compose.dev.yaml exec app python manage.py createsuperuser
```

`http://127.0.0.1:18000/`에서 로그인하고 **기초정보 관리**에서 부서 → 사용자 → 부서장 → 정책 순으로 등록한다. 부서장 본인 신청은 사용자 정보의 지정 승인자 또는 상위 부서장을 설정한다. 정책은 적용 시작일과 기준(기본 960분)을 등록해야 제출할 수 있다.

코드는 바인드 마운트되므로 수정 시 서버가 자동 재시작된다. 개발 DB는 `oms-dev_dev_db` 볼륨에 보존된다. `docker compose -p oms-dev -f compose.dev.yaml stop`으로 중지한다. 개발용 고정 비밀값과 HTTP 설정은 NAS 운영에 사용하지 않는다.

## 검증

```sh
docker compose -p oms-dev -f compose.dev.yaml exec app python manage.py check
docker compose -p oms-dev -f compose.dev.yaml exec app python manage.py makemigrations --check --dry-run
docker compose -p oms-dev -f compose.dev.yaml exec app python manage.py test
```

NAS 설치·배포·백업 절차: [docs/deployment-synology.md](docs/deployment-synology.md)

Docker ARM64·PostgreSQL 및 실제 HTTP 검증 결과와 재현 절차: [docs/verification.md](docs/verification.md)

기능 기획: [docs/overtime-approval-plan.md](docs/overtime-approval-plan.md)

## 프로젝트 구조

```text
config/             설정, URL, WSGI
core/               업무 모델, 권한 조회, 관리자, 테스트
templates/          로그인 및 대시보드
static/             자체 CSS (외부 CDN 불필요)
scripts/            운영 서버 실행
docs/               기획 및 NAS 배포 문서
Dockerfile          비관리자 계정으로 실행하는 앱 이미지
compose.yaml        DS423 ARM64 앱·DB 배포
.env.example        운영 환경변수 예시
```

의존성은 지원 버전 범위로 지정했다. 실제 배포 전 검증한 정확한 버전으로 잠금 파일을 만들고, 앱 이미지는 고유 릴리스 태그로 관리한다. ARM64 이미지·PostgreSQL은 Docker 에뮬레이션에서 검증했으며 NAS 실기기와 HTTPS 환경은 배포 시 확인한다.
