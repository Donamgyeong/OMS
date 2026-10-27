# SSH로 NAS 자동 배포

개발 PC에서 다음 명령을 실행한다. 기본 배포 경로는 `/volume1/oms/`, Compose 프로젝트 이름은 `oms`다.
기존 배포의 경로와 프로젝트 이름을 그대로 사용해야 같은 DB·media 볼륨을 연결한다.

```sh
python3 scripts/deploy_nas.py --host admin@nas-address
```

## 최초 준비

- 개발 PC: Python 3.9 이상, Docker Buildx, SSH 클라이언트와 실행 중인 Docker 엔진.
- NAS: SSH, Docker Compose v2 (`--wait`, `--wait-timeout` 지원), `tar`, `sha256sum`.
- SSH 키 인증과 호스트 키 확인을 먼저 완료한다. SSH config 별칭도 사용할 수 있다.
  스크립트는 `BatchMode=yes`를 사용하며 비밀번호 입력과 호스트 키 검증 생략을 지원하지 않는다.
- 기본 원격 Docker 명령은 `sudo -n docker`다. NAS 계정이 이 명령을 비밀번호 입력 없이 실행할 수 있어야 한다.
  이미 Docker 직접 실행 권한이 있는 계정이라면 `--no-sudo`를 지정한다.
- NAS 배포 폴더는 SSH 계정이 쓸 수 있어야 하며, 운영용 `.env`와 Cloudflare 터널 token 파일을 미리 준비한다.
  `.env`의 도메인·CSRF origin·Entra callback을 일치시킨다. 환경변수의 `APP_IMAGE`는 배포 성공 후 자동 갱신한다.
- `.env`는 600, token 디렉터리는 700, token 파일은 비루트 cloudflared가 읽을 수 있도록 444를 사용한다.
  NAS ACL도 확인한다. [Cloudflare 설정 절차](deployment-cloudflare.md)를 따른다.
- NAS에 없는 PostgreSQL·cloudflared 이미지는 Compose가 가져오므로 NAS의 이미지 레지스트리 연결이 필요하다.
  인터넷 연결이 없으면 ARM64 인프라 이미지를 별도로 가져온다. 앱 이미지는 항상 SSH로 전송한다.

SSH 설정 예시 (`~/.ssh/config`):

```sshconfig
Host oms-nas
    HostName 192.168.0.20
    User deploy
    Port 22
    IdentityFile ~/.ssh/id_ed25519
```

## 실행 옵션

연결·빌드·파일 생성 없이 배포 계획 확인:

```sh
python3 scripts/deploy_nas.py --host oms-nas --dry-run
```

경로, SSH 포트, 키 파일을 직접 지정:

```sh
python3 scripts/deploy_nas.py --host deploy@192.168.0.20 \
  --path /volume1/oms/ --project oms \
  --port 2222 --identity ~/.ssh/id_ed25519
```

이미 빌드·검증한 로컬 ARM64 이미지 사용:

```sh
python3 scripts/deploy_nas.py --host oms-nas \
  --skip-build --image oms:2026.10.27-1
```

기본 이미지 태그는 UTC 시각과 무작위 suffix로 생성한다. `--image`로 지정할 때도 새 태그를 사용하며
운영 중이거나 이전에 배포한 태그를 덮어쓰지 않는다. `latest`는 허용하지 않는다.
처음 Teams worker를 활성화하려면 NAS `.env`의 Teams 설정과 `TEAMS_ENABLED=1`을 준비한 뒤 `--teams`를 추가한다.
이미 실행 중인 worker는 일반 업데이트에서도 함께 중지·교체·재시작한다.

## 수행 순서와 산출물

1. SSH, NAS 경로·`.env`, Docker 권한을 사전 확인한다.
2. 현재 작업 트리로 ARM64 앱 이미지를 빌드한다. 커밋하지 않은 코드도 포함된다.
3. 이미지를 저장하고 Compose, 원격 스크립트, 이미지 ID와 SHA-256 체크섬을 묶어 SSH로 전송한다.
4. NAS 배포 잠금을 잡고 체크섬·이미지 ID·아키텍처·Compose·터널 token 파일 존재·Django 운영 설정을 검사한다.
5. DB health와 마이그레이션 누락을 확인하고, app과 worker를 중지한 뒤 DB·media를 백업한다.
6. 마이그레이션을 적용하고 app·worker·터널을 교체한다. app·터널 health와 connector readiness를 확인한다.
7. 성공한 이미지 태그를 NAS `.env`에 기록하고 운영 Compose 파일을 교체한다.

로컬 산출물은 `dist/nas/<릴리스>/`, NAS 전송 파일은 `<배포경로>/releases/<릴리스>/`,
백업은 `<배포경로>/backups/<릴리스>/`에 남긴다. Git과 Docker 빌드에서 `dist/`를 제외했다.
로컬 `.env`와 비밀 파일은 전송하지 않는다. NAS `.env`는 이미지 태그 외의 값을 유지한다.
백업에는 이전 `.env`가 포함되므로 접근 권한을 제한하고 별도 저장소에도 보관한다.
최초 배포에 기존 app 컨테이너가 없다면 백업할 기존 media 마운트가 없으므로 media 백업은 생략한다.
기존 app이 있으면 실제 컨테이너의 media 볼륨을 읽기 전용으로 연결해 백업한다.

배포 중에는 서비스가 중단된다. 전체 기능 테스트와 실제 외부 SSO 로그인은 이 스크립트가 대신하지 않는다.
중요 배포 전 기존 검증 절차를 실행하고, 배포 후 외부 네트워크에서 로그인·권한·로그아웃을 확인한다.
오래된 이미지·전송 파일·백업은 자동 삭제하지 않는다.

## 실패 시

사전 검사 실패는 기존 app과 worker를 중지하기 전에 종료한다. 백업·마이그레이션·기동 단계에서 실패하면
단계와 백업 경로를 출력하며, DB를 자동으로 되돌리거나 이전 앱을 자동 재시작하지 않는다.
마이그레이션이 일부 적용되었을 수 있으므로 DB 상태와 이전 이미지의 호환성을 확인하고 복구한다.
실패 시 NAS `.env`의 이미지 태그는 갱신하지 않지만 새 컨테이너가 이미 실행 중일 수 있다.
상태 확인에 실패한 앱을 외부에서 분리하려면 해당 프로젝트의 cloudflared를 중지한다.
이전 Compose와 `.env` 사본, `database.dump`, `media.tar.gz`는 해당 백업 폴더에서 확인한다.

동시 실행은 `<배포경로>/.deploy-lock`으로 차단한다. 강제 종료로 잠금이 남았다면 실행 중인 배포가 없는지
확인한 후 빈 디렉터리만 `rmdir`로 제거한다. `down -v`는 사용하지 않는다.

로컬 배포 로직 검사 (Docker·SSH 연결 없이 실행):

```sh
python3 -m unittest discover -s scripts -p test_deploy_nas.py -v
sh -n scripts/deploy_nas_remote.sh
```
