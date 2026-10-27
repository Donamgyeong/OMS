# Cloudflare Tunnel로 OMS 외부 연결

현재 준비 범위는 배포 파일과 아래 설정 절차다. Cloudflare 계정·도메인·실제 터널·토큰은 아직 생성하거나 연결하지 않았다. 예시의 `oms.example.com`은 사용할 실제 주소로 모두 교체한다.

## 1. 배포 구조

```text
직원 브라우저 ─ HTTPS ─ Cloudflare ─ 암호화 터널 ─ cloudflared
                                                    │ HTTP, Docker 네트워크
                                                    ▼
                                                   app ─ 내부 네트워크 ─ db
```

- `compose.cloudflare.yaml`은 독립적인 운영 Compose다. `compose.yaml`과 합치지 않고 **대신 사용**한다. DSM 리버스 프록시는 거치지 않는다.
- 앱·DB·터널 컨테이너 모두 호스트 포트를 공개하지 않는다. 라우터 포트 포워딩, NAS 인바운드 80/443 개방, NAS용 공개 TLS 인증서는 이 구조에 필요하지 않다.
- `cloudflared`와 앱은 `tunnel` 네트워크를 공유한다. DB는 `internal: true`인 `database` 네트워크에만 연결되어 터널 컨테이너가 DB에 직접 연결하지 못한다. 앱은 Entra 인증을 위해 외부 HTTPS에 접속할 수 있다.
- 브라우저 TLS는 Cloudflare에서 종료하고 같은 Docker 호스트 안의 origin은 `http://app:8000`으로 연결한다. 내부 origin을 `https://app:8000`으로 설정하면 TLS 오류가 난다.
- Django의 Secure 쿠키·CSRF·HTTPS 리다이렉트는 유지한다. Cloudflare가 방문자의 프로토콜로 덮어쓰는 `X-Forwarded-Proto`를 앱에서 신뢰한다. 앱에 다른 프록시나 직접 공개 포트를 추가하지 않는다.
- 터널은 전송 경로다. OMS 로그인과 직원 접근 권한은 기존 Entra SSO가 담당한다. 초기 구성에는 Cloudflare Access의 별도 로그인 게이트를 추가하지 않는다.

## 2. 준비할 값

| 값 | 확인 위치 / 용도 |
| --- | --- |
| 실제 OMS 도메인 | 예: `oms.company.com`. Cloudflare에 연결되어 DNS를 관리할 수 있는 도메인 |
| 앱 이미지 | 개발 PC/CI에서 빌드한 ARM64 이미지 태그 |
| Django SECRET_KEY / DB 비밀번호 | 배포 담당자가 생성한 별도 무작위 값 |
| Entra tenant/client/secret | [SSO 앱 등록 문서](entra-sso.md) |
| Cloudflare Tunnel token | 아래에서 생성할 remotely-managed tunnel의 connector token. Cloudflare API token과 다름 |

`cloudflared` 공식 ARM64 이미지는 `2026.10.3`으로 고정했다. NAS가 인터넷에서 이미지를 받을 수 없으면 개발 PC에서 `docker pull --platform linux/arm64 cloudflare/cloudflared:2026.10.3` → `docker save` → NAS의 `docker load`로 가져온다. 앱과 DB 이미지도 준비한다. 업데이트는 버전을 명시적으로 변경하고 검증한다.

앱의 ARM64 빌드·NAS 전송 방법은 [NAS 배포 문서의 이미지 준비](deployment-synology.md#1-이미지-준비-개발-pc)를 따른다. Cloudflare 배포에는 아래 파일도 NAS에 함께 복사한다. 앱·DB 공통 설정을 변경할 때는 두 운영 Compose 파일에 필요한 변경을 함께 반영한다.

## 3. Cloudflare 계정·도메인·터널 만들기

1. Cloudflare 계정을 준비하고 도메인을 추가한다. Cloudflare가 안내하는 네임서버를 등록기관에서 적용하고 도메인이 Active가 될 때까지 확인한다. 기존 DNS·메일 레코드는 보존한다. 이미 Cloudflare에서 관리하는 도메인이면 이 단계는 생략한다.
2. Cloudflare 대시보드의 **Networking → Tunnels**에서 터널을 생성한다. Cloudflare One 화면에서는 **Networks → Connectors → Cloudflare Tunnels**로 표시될 수 있다. 유형은 `cloudflared`, 이름은 예를 들어 `oms-nas`로 지정한다.
3. Docker 설치 안내에 표시되는 connector token을 확보한다. 대시보드의 전체 `docker run ... --token ...` 명령을 실행하지 않고, 토큰 값만 아래 비밀 파일에 보관한다. 인증용 `cert.pem`이나 Cloudflare 계정 API token은 이 구성에 필요하지 않다.
4. 터널의 **Routes / Published application routes**에서 경로를 추가한다.

| 항목 | 설정 |
| --- | --- |
| Public hostname | 실제 주소, 예: `oms.company.com` |
| Path | 비워서 OMS 전체 경로 사용 |
| Service type | HTTP |
| Service URL | `app:8000` — 전체 주소로 입력하는 UI에서는 `http://app:8000` |
| HTTP Host Header | 기본값 유지. 별도로 지정한다면 실제 OMS 도메인만 사용 |
| HTTP/2 to origin | 끔. Gunicorn에는 HTTP/1.1 사용 |
| Disable TLS verification | 사용하지 않음. origin은 HTTP이므로 TLS 검증 설정 자체가 불필요 |

컨테이너의 `localhost`는 해당 컨테이너 자신이다. Service URL에 `localhost:8000`, NAS 관리 주소, DB 주소를 넣지 않는다. 공개 경로 저장 시 생성되는 DNS가 해당 hostname을 터널에 연결하는지 확인하고, 같은 hostname에 충돌하는 A/AAAA/CNAME 레코드가 있으면 용도를 확인한 뒤 정리한다. 추가 wildcard hostname은 등록하지 않는다.

5. 해당 hostname의 Cloudflare edge 인증서가 Active인지 확인하고 **Always Use HTTPS**를 켠다. 다른 사내 서비스도 같은 zone을 사용한다면 기존 zone 설정의 적용 범위를 확인하고 해당 hostname에만 HTTP→HTTPS Redirect Rule을 적용해도 된다. 두 방식 중 하나를 사용한다.
6. **Cache Rules**에서 이 hostname 전체를 Bypass cache로 설정한다. 예시 조건은 `(http.host eq "oms.company.com")`이다. 인증 callback·직원 화면에 Cache Everything이나 강제 Edge TTL이 적용되지 않게 기존 규칙과 우선순위를 확인한다. 초기에는 `/static/`도 함께 우회하여 단순하게 운영한다.
7. 로그인·callback에 반복적인 브라우저 challenge가 걸리면 실제 세션/CSRF 흐름을 점검하고 해당 규칙 범위를 조정한다. 전역 보안 기능을 일괄 해제하지 않는다.

## 4. NAS 파일과 환경변수

NAS의 배포 폴더(예: `/volume1/oms/`)에 다음을 준비한다.

```text
compose.cloudflare.yaml
.env                         # .env.cloudflare.example을 복사하고 실제 값 입력
secrets/
  cloudflared-token          # 터널 token 한 줄만 저장
```

`.env`에는 도메인을 다음과 같이 일치시킨다.

```dotenv
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,oms.company.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://oms.company.com
ENTRA_REDIRECT_URI=https://oms.company.com/accounts/entra/callback/
```

Entra의 Web redirect URI에도 **동일한 HTTPS callback**을 등록한다. `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`를 모두 입력하고, SECRET_KEY·DB 비밀번호·`APP_IMAGE`도 설정한다. Microsoft 로그아웃을 별도로 켜면 SSO 문서의 복귀 주소 설정도 적용한다. 네 개의 Entra 값 중 일부만 입력한 상태로 앱을 시작하지 않는다.

터널 token은 `.env`, Compose command, 채팅, Git에 넣지 않는다. 터널 컨테이너에만 `/run/secrets/cloudflared_token` 파일로 전달한다. `secrets/`와 `.env`는 Git과 앱 이미지 빌드에서 제외했다. Compose secrets는 이 구성에서 파일의 읽기 전용 마운트이며 암호화 저장소는 아니다.

NAS 셸에서 아래처럼 비공개 디렉터리를 만든 뒤 운영 담당자가 편집기로 토큰 파일을 생성한다. 편집기에는 **토큰 한 줄만** 넣고 파일을 저장한다. 토큰을 포함한 셸 명령은 입력하지 않는다.

```sh
mkdir -p secrets
chmod 700 secrets
chmod 600 .env
# 운영 담당자가 편집기로 secrets/cloudflared-token을 생성한다.
chmod 444 secrets/cloudflared-token
```

비밀 디렉터리는 호스트에서 담당자만 접근하도록 `700`으로 유지하고, 마운트할 파일만 `444`로 설정한다. 이렇게 하면 기본 UID 65532의 비루트 cloudflared가 읽을 수 있다. 일반 Compose에서 file 기반 secrets의 uid/gid/mode 재매핑은 지원되지 않으므로 YAML의 mode 값만으로 권한을 해결하지 않는다. NAS 공유 폴더 ACL도 다른 사용자에게 `secrets/` 접근 권한을 부여하지 않도록 확인한다. Docker 관리 권한은 신뢰하는 운영 담당자만 가진다는 전제다.

`CLOUDFLARE_TUNNEL_TOKEN_FILE`로 다른 파일 경로를 사용할 수도 있다. 토큰 파일과 `.env`는 운영 백업에서 접근을 제한한다. `APP_PORT`는 Cloudflare 전용 Compose에서 사용하지 않는다.

## 5. 최초 실행

이하 명령은 배포 폴더 기준이다. 같은 `-p oms`와 `-f compose.cloudflare.yaml`을 모든 운영 명령에서 사용한다. 이 프로젝트 이름을 유지해야 같은 DB·media 볼륨을 사용한다. 기존 데이터가 있는 프로젝트와 합치거나 이름을 변경하기 전에는 볼륨 연결을 별도로 확인한다.

```sh
docker compose -p oms -f compose.cloudflare.yaml config --quiet
docker compose -p oms -f compose.cloudflare.yaml up -d db
docker compose -p oms -f compose.cloudflare.yaml run --rm app python manage.py migrate --noinput
docker compose -p oms -f compose.cloudflare.yaml run --rm app python manage.py check --deploy --fail-level ERROR
docker compose -p oms -f compose.cloudflare.yaml up -d app
docker compose -p oms -f compose.cloudflare.yaml ps
docker compose -p oms -f compose.cloudflare.yaml up -d cloudflared
```

`depends_on: service_healthy`가 DB 및 앱 준비를 확인한다. 앱은 SSO 필수값 점검 후 시작한다. HSTS 미설정 경고는 실제 도메인 운영을 확인한 뒤 적용 여부를 정한다. `cloudflared`는 앱이 healthy일 때 시작하며 실제 token이 없으면 터널 연결이 되지 않는다. 기본 Docker Compose CLI를 기준으로 하며 NAS의 Container Manager에서 secrets 및 파일 구문을 처리하지 못하면 지원되는 Compose CLI로 실행한다.

시작 후 다음을 확인한다.

```sh
docker compose -p oms -f compose.cloudflare.yaml ps
docker compose -p oms -f compose.cloudflare.yaml exec cloudflared cloudflared tunnel --metrics 127.0.0.1:2000 ready
```

readiness는 Cloudflare와의 연결 상태다. 앱·DNS·외부 요청까지 성공했다는 의미는 아니므로 아래 외부 점검도 수행한다. connector 로그는 기본 warn이며 10MB × 3개로 회전한다. 장애 로그에 요청 URI가 포함될 수 있으므로 접근·보관을 제한하고, 인증 중에는 헤더와 URL을 기록하는 debug 로그를 켜지 않는다. Cloudflare Logpush 등을 구성할 때도 callback query와 인증 정보를 보관하지 않도록 필드를 선택한다.

최초 관리자가 공개 주소에서 SSO 로그인해 업무 설정 대기에 도달한 뒤 권한을 부여한다.

```sh
docker compose -p oms -f compose.cloudflare.yaml exec app python manage.py bootstrap_entra_admin <사용자-Object-ID> --superuser
```

이후 부서·직원·승인자·정책 설정은 [SSO 운영 문서](entra-sso.md)를 따른다.

## 6. 외부 점검과 장애 진단

사내 Wi-Fi 외에 모바일 데이터 등 외부 네트워크에서도 확인한다.

1. `http://<도메인>/`이 HTTPS로 이동하고 `https://<도메인>/health/`가 `{"status":"ok"}`를 반환한다.
2. `/accounts/login/`에서 Microsoft 회사 계정 로그인이 가능하고 callback 후 대기 또는 업무 화면으로 돌아온다.
3. 앱 역할 없는 계정과 비활성 계정은 거부된다. 설정 완료 직원은 조회·입력·결재를 수행하고 CSRF 오류나 HTTPS 리다이렉트 반복이 없다.
4. 응답의 Secure/HttpOnly 세션 쿠키와 업무 화면의 `Cache-Control: private, no-store`를 확인한다. Cloudflare가 업무 HTML을 캐시하지 않아야 한다. 해당 응답의 `CF-Cache-Status`는 DYNAMIC/BYPASS 등이며 HIT이면 규칙을 다시 확인한다.
5. 로그아웃 후 업무 화면 접근이 차단되고 NAS의 8000/5432/DSM 포트를 외부로 열지 않았는지 확인한다.

| 증상 | 확인할 사항 |
| --- | --- |
| 1033 / connector unhealthy | token 파일 존재·읽기 권한, 올바른 터널 token인지, outbound DNS와 Cloudflare 연결 |
| 502 / connector healthy | 공개 route의 `http://app:8000`, 앱 health, Docker 네트워크, origin을 HTTPS로 잘못 지정했는지 |
| 400 DisallowedHost | 공개 Host가 `DJANGO_ALLOWED_HOSTS`에 있는지, Host Header를 `app`/`localhost`로 덮어썼는지 |
| HTTPS redirect 반복 | 방문자 HTTPS 강제와 원래 `X-Forwarded-Proto` 전달 여부, 중간 프록시 추가 여부 |
| CSRF 403 | HTTPS origin·도메인·프록시 Host가 일치하는지, 쿠키가 유지되는지 |
| AADSTS50011 | Entra 앱 등록과 `ENTRA_REDIRECT_URI`가 문자·경로·scheme까지 일치하는지 |
| Permission denied token-file | 호스트 비밀 디렉터리 700 / 파일 444 및 NAS ACL·마운트 경로 |
| QUIC 연결 실패 | outbound UDP 7844 확인. UDP 사용이 불가능하면 `.env`의 `CLOUDFLARED_PROTOCOL=http2`로 변경하고 connector 재생성 |

방화벽은 Cloudflare 공식 목록에 따라 outbound TCP/UDP 7844, DNS를 허용한다. 앱의 Entra HTTPS와 이미지 다운로드에는 별도 outbound 443이 필요하다. 7844 연결 문제를 해결하려고 NAS 인바운드 포트를 열지 않는다.

## 7. 업데이트·토큰 교체·차단

앱 업데이트 전 DB/media를 백업하고 기존 NAS 배포 문서의 절차를 따른다. 모든 명령에 `-p oms -f compose.cloudflare.yaml`을 적용한다. 프로젝트 볼륨은 유지하며 `down -v`를 사용하지 않는다. 앱 시작 시 자동 마이그레이션하지 않는다.

토큰 교체는 Cloudflare에서 해당 터널의 token을 회전하고, `cloudflared`를 중지한 뒤 기존 파일을 편집하거나 교체하고 읽기 권한을 복원한다. Compose 파일을 변경할 필요는 없다. 파일을 새 inode로 교체하는 경우도 반영되도록 단순 restart 대신 재생성한다.

```sh
docker compose -p oms -f compose.cloudflare.yaml stop cloudflared
# 담당자가 secrets/cloudflared-token을 새 값으로 교체한다.
chmod 444 secrets/cloudflared-token
docker compose -p oms -f compose.cloudflare.yaml up -d --force-recreate cloudflared
```

해당 터널의 다른 connector가 있다면 각각 새 token 적용 여부를 확인한다. 이미 연결된 connector까지 종료해야 하는 상황은 Cloudflare의 connector 제거/연결 해제도 함께 수행한다. 외부 공개를 즉시 중단하려면 이 구성의 `cloudflared`를 중지하고 Cloudflare에서 공개 route도 비활성화한다. 앱·DB 볼륨을 삭제할 필요는 없다.

## 8. 준비 완료와 남은 설정

저장소에는 Cloudflare 전용 Compose·환경변수 예시·비밀 파일 제외 규칙을 준비했다. 아직 실제 도메인·Cloudflare 계정·터널·DNS·토큰·Entra 앱·NAS 배포를 설정한 상태는 아니다. 앱 이미지와 DB의 검증은 실제 외부 접속, Cloudflare TLS, MFA 정책 검증을 대신하지 않는다.

## 공식 근거

- [Cloudflare Tunnel 시작](https://developers.cloudflare.com/tunnel/get-started/)
- [token-file, 실행 옵션과 로그 수준](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/)
- [Cloudflare의 X-Forwarded-Proto 처리](https://developers.cloudflare.com/fundamentals/reference/http-headers/)
- [origin 연결 옵션](https://developers.cloudflare.com/tunnel/reference/origin-parameters/)
- [로그인·동적 콘텐츠 캐시 문제](https://developers.cloudflare.com/cache/troubleshooting/dynamic-content-and-login-issues/)
- [outbound 방화벽 요구사항](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)
- [Docker Compose secrets 파일 권한 제약](https://docs.docker.com/reference/compose-file/services/#secrets)
