# OMS · 초과 근무 사전 승인 시스템

Synology DS423에서 실행하기 위한 Django 기반 프로젝트 템플릿.

## 구성

- Python 3.12 / Django 5.2 LTS / PostgreSQL 17
- Django Templates / HTMX 2.0.8 / Bootstrap 5.3.8 / 자체 CSS / Gunicorn / WhiteNoise
- ARM64 앱 이미지 / Container Manager Compose / DSM HTTPS 리버스 프록시
- HTMX·Bootstrap은 라이선스와 함께 로컬 정적 자산으로 포함한다. 외부 CDN 없이 초기 화면을 제공하며 HTMX 상호작용은 업무 화면 구현 시 확장한다. 원본 재취득은 `python scripts/fetch_frontend.py`로 수행한다.

## 구현 범위

- 커스텀 사용자, 부서, 초과 근무 정책, 계획, 결재 이력, 실적 모델 및 초기 마이그레이션
- 로그인·POST 로그아웃, 이번 달 대시보드, 개인·부서·전체 조회 범위
- 슈퍼유저 전용 기초정보 관리 및 임시 계획·실적 입력
- DB 연결을 확인하는 `/health/`, 컨테이너 영구 볼륨, 운영 보안 설정

아직 실제 운영용 완성 시스템은 아니다. 계획 제출·회수·결재 서비스, 월 16시간 동시성 제어, 정책 적용, 승인자 지정, 직원용 입력 화면, 변경 이력, 조직 이력, 알림, API 연동, 로그인 시도 제한은 후속 구현 대상이다. 현재 정책과 결재 모델은 데이터 구조만 제공하며 승인 판단을 수행하지 않는다. 초과 근무 실적은 수동 입력하고 근무 일정 기반 자동 계산은 추후 구현한다.

일반 관리자 역할은 전체 업무 조회 권한이다. Django Admin 접근은 별도의 슈퍼유저로 제한한다. Admin은 초기 설정용이며 일반 직원·부서장 업무 화면을 대신하지 않는다.

## 로컬 실행 (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DJANGO_DEBUG='1'
$env:DJANGO_USE_SQLITE='1'
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver
```

`http://127.0.0.1:8000/`에서 로그인, `/admin/`에서 부서·사용자·정책을 등록한다. 부서장 생성 후 부서의 head를 지정할 수 있다. 개발용 SQLite는 PostgreSQL의 동시성·잠금 검증을 대체하지 않는다. `.env`는 Compose가 읽으며 로컬 Python에서는 환경변수를 직접 설정해야 한다.

venv 생성 시 ensurepip 오류가 발생하는 PC에서는 `python -m venv --without-pip .venv`로 생성하고, 전역 pip가 있다면 `python -m pip --python .\.venv\Scripts\python.exe install -r requirements.txt`로 설치한다.

## 검증

```powershell
$env:DJANGO_DEBUG='1'
$env:DJANGO_USE_SQLITE='1'
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test
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
