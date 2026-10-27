#!/bin/sh
# Called by deploy_nas.py; requires POSIX sh, Compose v2, tar and sha256sum.
set -eu
umask 077
export PATH="$PATH:/usr/local/bin:/var/packages/ContainerManager/target/usr/bin:/var/packages/Docker/target/usr/bin"
docker_bin=$(command -v docker) || { echo 'Docker executable not found on NAS.' >&2; exit 1; }
target=$1
release=$2
project=$3
APP_IMAGE=$4
docker_mode=$5
teams=$6
export APP_IMAGE
# The SSH caller allocates a TTY. Prompt once here, then preserve Docker stdin/stdout.
if [ "$docker_mode" = sudo ]; then sudo -v; fi
cd "$target"
# Canonicalize even when this script is called directly with a trailing slash.
target=$(pwd -P)
test -f .env
mkdir "$target/.deploy-lock" 2>/dev/null || {
    echo 'Another deployment holds .deploy-lock. Check it before removing a stale lock.' >&2
    exit 1
}
phase=preflight
backup="$target/backups/$(basename "$release")"
finish() {
    result=$?
    trap - EXIT
    rmdir "$target/.deploy-lock"
    if [ "$result" -ne 0 ]; then
        echo "Deployment failed during: $phase. Backup location: $backup" >&2
        echo 'No database rollback was attempted. Check migration state before restarting writers.' >&2
    fi
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
d() {
    if [ "$docker_mode" = sudo ]; then
        # Refresh/re-prompt after long builds or migrations; never consume piped Docker input.
        sudo -v && sudo -n "$docker_bin" "$@"
    else
        "$docker_bin" "$@"
    fi
}
c() {
    d compose --project-directory "$target" --env-file "$target/.env" \
        -p "$project" -f "$release/compose.cloudflare.yaml" --profile teams "$@"
}
cd "$release"
sha256sum -c SHA256SUMS
d load -i app-image.tar
test "$(d image inspect "$APP_IMAGE" --format '{{.Id}}')" = "$(cat image-id)"
test "$(d image inspect "$APP_IMAGE" --format '{{.Os}}/{{.Architecture}}')" = linux/arm64
cd "$target"
c config --quiet
# Resolve Compose's secret path without sourcing .env as shell code or printing values.
token=$(c config --format json | d run --rm -i --network none --entrypoint python "$APP_IMAGE" \
    -c 'import json,sys; print(json.load(sys.stdin)["secrets"]["cloudflared_token"]["file"])')
test -s "$token" || { echo 'Cloudflare token file is missing or empty.' >&2; exit 1; }
# Check configuration and credentials format before interrupting the current app.
c run --rm --no-deps app python manage.py check --deploy --fail-level ERROR
# Preserve an already running worker on routine updates.
worker=$(c ps --status running -q teams-worker)
app=$(c ps -a -q app)
if [ -n "$worker" ]; then teams=1; fi
if [ "$teams" = 1 ]; then
    c run --rm --no-deps app python -c 'import os; os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings"); from django.conf import settings; assert settings.TEAMS_ENABLED, "Set TEAMS_ENABLED=1 on NAS before --teams"'
fi
c up -d --wait --wait-timeout 180 db
c run --rm --no-deps app python manage.py makemigrations --check --dry-run
mkdir -p "$backup"
cp .env "$backup/env.before"
if [ -f compose.cloudflare.yaml ]; then cp compose.cloudflare.yaml "$backup/compose.before.yaml"; fi
phase=backup
c stop app teams-worker
c exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup/database.dump.tmp"
test -s "$backup/database.dump.tmp"
mv "$backup/database.dump.tmp" "$backup/database.dump"
if [ -n "$app" ]; then
    d run --rm --network none --volumes-from "$app:ro" --entrypoint tar "$APP_IMAGE" \
        -C /app/media -czf - . > "$backup/media.tar.gz.tmp"
    test -s "$backup/media.tar.gz.tmp"
    mv "$backup/media.tar.gz.tmp" "$backup/media.tar.gz"
fi
phase=migration
c run --rm --no-deps app python manage.py migrate --noinput
phase=startup
c up -d --wait --wait-timeout 180 app
if [ "$teams" = 1 ]; then c up -d teams-worker; fi
c up -d --wait --wait-timeout 180 cloudflared
c exec -T cloudflared cloudflared tunnel --metrics 127.0.0.1:2000 ready
phase=save-configuration
# Keep the NAS secrets and only replace the release image tag. Publish after health checks.
awk -v image="$APP_IMAGE" '
    BEGIN { found=0 }
    /^[[:space:]]*APP_IMAGE[[:space:]]*=/ { if (!found) print "APP_IMAGE=" image; found=1; next }
    { print }
    END { if (!found) print "APP_IMAGE=" image }
' .env > .env.deploy-next
chmod 600 .env.deploy-next
mv .env.deploy-next .env
cp "$release/compose.cloudflare.yaml" compose.cloudflare.yaml.deploy-next
mv compose.cloudflare.yaml.deploy-next compose.cloudflare.yaml
c ps
echo "Deployment complete: $APP_IMAGE"
echo "Backups: $backup (contains secrets; keep access restricted)"
