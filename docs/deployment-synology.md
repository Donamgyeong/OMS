# Synology DS423 배포

## 전제

- DS423은 ARM64 CPU와 RAM 2GB를 사용한다. 설치된 DSM의 패키지 센터에서 Container Manager 지원 여부를 확인한다.
- 개발 PC 또는 CI에서 ARM64 이미지를 빌드하고 NAS는 이미지를 실행한다.
- 초기 컨테이너 메모리 상한은 각각 384MB다. 보장 사용량이 아니며 DSM·기존 패키지와 함께 실제 사용량을 측정하여 조정한다.
- Compose는 DB 포트를 공개하지 않고 앱 포트를 NAS 루프백에만 연결한다.

## 1. 이미지 준비 (개발 PC)

Docker 엔진을 실행한 상태에서 프로젝트 루트에서 실행한다.

```sh
docker buildx build --platform linux/arm64 -t oms:0.1.0 --load .
docker save -o oms-0.1.0-arm64.tar oms:0.1.0
```

이미지 파일을 NAS로 전송한 후 Container Manager에서 가져오거나 SSH에서 `docker load -i oms-0.1.0-arm64.tar`를 실행한다. 사내 레지스트리를 사용하는 경우 고유 버전 태그로 push/pull해도 된다.

## 2. 설정

NAS의 전용 프로젝트 폴더(예: `/volume1/docker/oms`)에 `compose.yaml`과 `.env.example`을 복사하고 `.env`를 만든다.

- `APP_IMAGE=oms:0.1.0`으로 지정한다.
- SECRET_KEY와 DB 비밀번호를 각각 충분히 긴 무작위 값으로 변경한다. 예시는 실제 비밀값이 아니다.
- 실제 서비스 도메인을 ALLOWED_HOSTS와 CSRF_TRUSTED_ORIGINS에 설정한다. healthcheck를 위해 localhost도 유지한다.
- DEBUG=0, HTTPS 관련 설정을 유지한다.
- `.env`의 파일 접근 권한은 운영 담당자로 제한한다.
- [Entra 앱 등록·역할 할당](entra-sso.md)을 완료하고 `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ENTRA_REDIRECT_URI`를 설정한다. 미설정 시 운영 시작 검사가 실패한다.
- 새 DB에서만 POSTGRES_* 초기 계정 설정이 적용된다. 기존 볼륨의 비밀번호는 환경변수 변경만으로 바뀌지 않는다.

## 3. DSM 리버스 프록시

DSM 로그인 포털의 고급 설정에서 HTTPS 도메인(443)을 `http://127.0.0.1:8000`으로 전달하고 인증서를 연결한다.

- 원래 Host를 앱에 전달한다.
- `X-Forwarded-Proto`를 실제 접속 방식에 따라 프록시에서 덮어쓴다. HTTPS 전용 프록시는 `https`로 설정한다.
- 앱이 해당 헤더를 신뢰하므로 외부에서 앱 포트로 직접 접근시키지 않는다.
- DSM 관리 포트와 PostgreSQL 포트를 인터넷에 공개하지 않는다.
- 초기 접근은 사내망 또는 회사 VPN으로 제한한다.

## 4. 최초 실행

아래 명령은 NAS 프로젝트 디렉터리의 셸 기준이다. Container Manager 프로젝트 기능을 사용할 때도 동일한 Compose 파일을 사용한다.

```sh
docker compose up -d db
docker compose run --rm app python manage.py migrate
docker compose run --rm app python manage.py check --deploy
docker compose up -d app
docker compose ps
docker compose logs --tail=100 app
```

DB 초기화 직후 준비될 때까지 기다린 후 migrate를 실행한다. 환경에 따라 `docker compose` 대신 `docker-compose`가 제공될 수 있다.

최초 관리자가 회사 계정으로 로그인한 뒤 아래 명령으로 권한을 부여한다. 사용자 Object ID는 Entra에서 확인한다.

```sh
docker compose exec app python manage.py bootstrap_entra_admin <사용자-Object-ID> --superuser
```

기초정보 관리에서 부서·직원 업무 정보·승인자·정책을 설정한다. 직원은 첫 SSO 로그인으로 생성되며, 사번·활성 부서·승인자 설정 후 업무 설정 완료를 체크한다. 전체 절차와 차단·복구 명령은 [SSO 운영 문서](entra-sso.md)를 참고한다.

`check --deploy`의 HSTS 경고는 HTTPS·도메인 운영을 확인한 후 적용 여부를 결정한다. HSTS는 현재 기본 설정에 넣지 않았다. MFA와 로그인 보호는 Entra 정책을 설정하고, 프록시 로그에서도 callback query string을 제외한다.

## 5. 업데이트

1. DB와 첨부파일을 백업한다.
2. 새 버전 이미지를 가져오고 APP_IMAGE를 변경한다.
3. `docker compose stop app`으로 쓰기를 중지한다.
4. `docker compose run --rm app python manage.py migrate`를 실행한다.
5. `docker compose up -d app`으로 앱을 교체한다.
6. healthcheck, 로그인, 권한별 조회와 로그를 확인한다.

앱 시작 시 자동 마이그레이션하지 않는다. DB 변경을 명시적인 배포 단계로 관리한다. 롤백은 DB 스키마 호환성을 확인하고 필요하면 백업을 복구한다.

## 6. 백업과 복구

DSM 작업 스케줄러에서 프로젝트 경로로 이동한 뒤 아래 예시를 실행할 수 있다. 실제 작업에는 실패 알림, 보관 기간, 외부 복사를 추가한다.

```sh
umask 077
mkdir -p backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "backups/oms-$(date +%Y%m%d-%H%M%S).dump"
```

첨부파일은 media 영구 볼륨에 저장할 예정이며 해당 볼륨도 백업한다. 앱 이미지에는 사용자 데이터가 없다. DB와 파일의 시점 일치가 필요하면 앱 쓰기를 중단하고 함께 백업한다. NAS 내 백업 외에 별도 장치 또는 저장소로 복사한다.

복구 시험은 격리된 신규 DB에서 먼저 수행한다.

```sh
docker compose exec -T db sh -c 'pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < backups/selected.dump
```

위 복구 명령은 빈 대상 DB를 전제로 한다. 운영 DB에 그대로 실행하지 않는다. `docker compose down -v`는 영구 데이터를 지우므로 일반 중지·업데이트 절차에 사용하지 않는다.

## 참고

- https://www.synology.com/en-us/products/DS423
- https://www.synology.com/en-us/dsm/packages/ContainerManager
- https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/
- https://docs.docker.com/build/building/multi-platform/
