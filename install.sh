#!/usr/bin/env bash
# hey senlyt pi daemon — 라즈베리파이 1줄 설치·구동 (다운로드부터 자동).
#
# 사용 (Pi에서 한 줄) — 스크립트는 **항상 main 의 사본**을 받고, 바꾸는 건 서버 URL 하나뿐:
#   curl -fsSL https://raw.githubusercontent.com/joomidang-tech/heysenlyt_pi/main/install.sh \
#     | sudo bash -s -- https://senlyt.com                 # prod  → 소스 main
#     | sudo bash -s -- https://dev-env.senlyt.com         # dev   → 소스 dev
#     | sudo bash -s -- https://v1-3-0.env.senlyt.com      # 프리뷰 → 소스 v1.3.0
#
# 손잡이 3개(2026-09-11 설계 — 이 파일은 브랜치와 무관한 한 벌이라 어느 브랜치의 사본이든 같다):
#   ① 어느 install.sh 를 받나   = curl URL 경로(평소 main)
#   ② 어느 소스를 설치하나      = SENLYT_INSTALL_REF (브랜치·태그·**40자리 전체 커밋 SHA**)
#                                 없으면 서버 URL 에서 유추(senlyt.com→main · dev-env→dev · vX-Y-Z.env→vX.Y.Z)
#                                 (구 이름 SENLYT_INSTALL_BRANCH 도 같은 뜻의 별칭으로 받는다)
#   ③ 어느 서버를 보나          = 첫 인자, 또는 SENLYT_SERVER_BASE_URL (인자가 우선)
#   예) curl -fsSL .../main/install.sh | sudo env SENLYT_INSTALL_REF=test bash -s -- https://senlyt.com
#
# 사람이 넣는 건 **서버 URL 하나**뿐. 나머지는 켜진 뒤 자동:
#   - deviceId  = HW 시리얼 자동수집(RPi4=cpuinfo·RPi5=device-tree)
#   - mode      = admin에서 승인할 때 배정 → 서버가 기기에 내려줌
#   - 펌프 모델 = **admin 센소리움 선언**(부팅 스냅샷 수신 — 미배정이면 전 모션 거부로 안전 대기.
#     ⇒ Tecan 실물은 admin 에서 +tecan 배정 후 설치/재시작) · valve = GPIO 자동감지
#   - 펌프 주소 = 부팅 버스 스캔 자동인식(+ 인식 실패 시 펌프 응답 감지 → 자동 재기동 재스캔)
# 등록은 키 없이 신청(TOFU) → admin에서 "승인"해야 online.  (재실행 안전·멱등)
set -euo pipefail
# 로케일 고정(R8) — env 보존 글롭 `[A-Z_]*` 가 UTF-8 collation 에선 소문자까지 매치해
# 로케일마다 보존 결과가 갈린다. C 고정 = ASCII 대문자 키만 보존(결정론·systemd 관습 정합).
export LC_ALL=C

SERVER_URL="${1:-${SENLYT_SERVER_BASE_URL:-}}"
REPO="https://github.com/joomidang-tech/heysenlyt_pi.git"
# 소스 ref — 브랜치·태그·전체 SHA 모두 `git fetch origin <ref>` 한 경로로 받는다(clone --branch 는 이름만 받아 폐기).
#   비면 아래 0절에서 서버 URL 로 유추한다(브랜치=환경 규칙: web branch-env.sh · pi server_target.branch_to_env 와 대칭).
REF="${SENLYT_INSTALL_REF:-${SENLYT_INSTALL_BRANCH:-}}"
APP_DIR="/opt/senlyt/heysenlyt-pi"
ENV_DIR="/etc/senlyt"
ENV_FILE="$ENV_DIR/device.env"
LOG_DIR="/var/log/senlyt"
STATE_DIR="/var/lib/senlyt"
SERVICE="/etc/systemd/system/senlytd.service"

# ── 0. 인자·권한 체크 ──────────────────────────────────────────────────────
if [ -z "$SERVER_URL" ]; then
	echo "❌ 서버 URL이 필요합니다." >&2
	echo "   예: curl -fsSL .../main/install.sh | sudo bash -s -- https://senlyt.com" >&2
	exit 1
fi
case "$SERVER_URL" in
	http://*|https://*) : ;;
	*) echo "❌ 서버 URL은 http(s):// 로 시작해야 합니다 (받은 값: $SERVER_URL)" >&2; exit 1 ;;
esac
if [ "$(id -u)" -ne 0 ]; then
	echo "❌ root 권한이 필요합니다(systemd·GPIO·시리얼). sudo 로 실행하세요." >&2
	exit 1
fi
# 소스 ref 유추(지정이 없을 때) — 서버 환경과 같은 브랜치. 모르는 host(localhost 등)는 main.
if [ -z "$REF" ]; then
	_host=$(printf '%s' "$SERVER_URL" | sed -E 's#^https?://##; s#[/:].*$##' | tr 'A-Z' 'a-z')
	case "$_host" in
		senlyt.com) REF=main ;;
		dev-env.senlyt.com) REF=dev ;;
		v*-*-*.env.senlyt.com) REF=$(printf '%s' "$_host" | sed -E 's/^v([0-9]+)-([0-9]+)-([0-9]+)\.env\.senlyt\.com$/v\1.\2.\3/') ;;
		*) REF=main ;;
	esac
fi

echo "▶ hey senlyt pi 설치 — server=$SERVER_URL  (ref=$REF)"

# ── 1. 시스템 의존성 (Raspberry Pi OS Bookworm 기준 · Python 3.11+) ─────────
#   하드웨어 라이브러리는 **apt 프리빌트**로 설치한다(pip 소스컴파일 = swig/컴파일러 필요 → 실패).
#   lgpio = Pi4·Pi5 **공통 현대 표준**(Pi5 는 RP1 칩이라 옛 RPi.GPIO 불가 — lgpio 라야 GPIO 동작).
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip
# 하드웨어 라이브러리(밸브 gpiozero+lgpio · 펌프 시리얼 pyserial) — 프리빌트 deb, 컴파일 없음.
#   없는 패키지는 건너뛴다(구 OS 폴백 — 각각 개별 설치라 하나 없어도 나머지는 깔림).
for pkg in python3-gpiozero python3-lgpio python3-serial; do
	apt-get install -y -qq "$pkg" 2>/dev/null && echo "  ✓ $pkg" || echo "  (skip $pkg — 이 OS엔 미제공)"
done

# ── 2. 소스 받기 — 신규·갱신 한 경로(멱등) ────────────────────────────────
#   `git fetch --depth 1 origin <ref>` 는 ref 가 브랜치·태그·전체 SHA 어느 것이든 그 커밋 하나를
#   FETCH_HEAD 로 세운다(clone --branch 는 이름만 받아 SHA 불가 → 폐기). 기존 설치가 다른 브랜치의
#   단일-브랜치 shallow clone 이어도 FETCH_HEAD 경로는 refspec 과 무관해 브랜치 전환 재설치가 된다.
#   작업트리는 detached 로 둔다 — 데몬은 브랜치명을 읽지 않고, "무엇이 깔렸나"는 env 의 두 기록 키로 남긴다.
mkdir -p "$(dirname "$APP_DIR")"
if [ -d "$APP_DIR/.git" ]; then
	echo "  ↻ 기존 설치 갱신"
	git -C "$APP_DIR" remote set-url origin "$REPO"
else
	echo "  ⬇ 저장소 준비"
	git init -q "$APP_DIR"
	git -C "$APP_DIR" remote add origin "$REPO"
fi
if ! git -C "$APP_DIR" fetch -q --depth 1 origin "$REF"; then
	echo "❌ 소스 '$REF' 를 원격에서 받지 못했습니다 — 브랜치/태그 이름이거나 40자리 전체 커밋 SHA 여야 합니다(축약 SHA 불가)." >&2
	exit 1
fi
git -C "$APP_DIR" checkout -q --detach FETCH_HEAD
git -C "$APP_DIR" reset -q --hard FETCH_HEAD
INSTALLED_SHA=$(git -C "$APP_DIR" rev-parse HEAD)
echo "  ✓ 소스 $REF @ ${INSTALLED_SHA:0:12}"

# ── 3. venv(--system-site-packages) + 데몬 설치 ────────────────────────────
#   데몬은 런타임 의존성 0(stdlib) — pip 은 데몬 패키지 등록만. --system-site-packages 로 위에서 apt
#   설치한 하드웨어 라이브러리(gpiozero/lgpio/serial)를 venv 가 그대로 본다(pip 컴파일 없음).
#   재실행 시 실행 중 데몬이 옛 venv 를 물고 있으면 재생성이 위험 → 먼저 멈추고, --system-site-packages
#   flag 반영을 위해 venv 를 새로 만든다(마지막에 restart 로 새 코드 기동).
systemctl stop senlytd 2>/dev/null || true
rm -rf "$APP_DIR/.venv"
python3 -m venv --system-site-packages "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"

# ── 4. 환경파일 — 넣는 값은 서버 URL 하나뿐 ────────────────────────────────
mkdir -p "$ENV_DIR" "$LOG_DIR" "$STATE_DIR/queue"
umask 077
# ⛔ 운영자 보존 값 수확(검증 P0-2 봉합·2026-09-01) — 종전엔 재실행마다 파일을 통째 재생성해
#   운영자가 손으로 넣은 키(SENLYT_VALVE_PINS 등)가 증발했다. 방식: 설치기가 관리하는 키만
#   갱신하고, 그 외 `KEY=값` 행은 전부 "운영자 보존 값" 섹션으로 이월한다. "존재 시 미갱신"은
#   기각 — 서버 URL 교체·템플릿 개선이 죽는다. 멱등: 재실행마다 동일 결과.
# ⚠️ env 다이어트(2026-09-02 — "기기가 알아야 하는 건 어느 서버에 붙는가뿐"): 템플릿이 찍는
#   설정은 이제 **SENLYT_SERVER_BASE_URL 하나**다. 나머지는 결정권이 딴 데 있거나 파생값이라
#   env 파일에서 걷어낸다(관리 목록 = strip 대상 — 재설치가 옛 각인을 제거):
#     - SENLYT_ENGINE            → 폐기. 펌프 모델 = admin 센소리움 선언(부팅 스냅샷) 단일 채널.
#     - PUMP_ADDRESSES           → 부팅 버스 스캔 자동인식(실펌프 응답 = 존재 SoT, 모드는 서버
#                                  배정 우선)이 원래 SoT — 각인은 그 자동화를 가리는 과잉이었다.
#     - SENLYT_RUN·LOG_DIR·SENLYT_STATE_DIR·SENLYT_LEDGER_PATH
#                                → 설정이 아니라 서비스 배선(설치 표준 경로·소비루프 스위치).
#                                  systemd 유닛 Environment= 로 이동(아래 5절) — 운영자 device.env
#                                  의 같은 키가 유닛 값을 덮는다(EnvironmentFile 이 나중 적용).
#   순서 권고: Tecan 실물 기기는 **admin 에서 +tecan 센소리움 배정 후** 설치/재시작하는 게
#   매끄럽다(배정 전 부팅은 UndeclaredEngineAdapter = 전 모션 거부라 **안전**하지만, 선언 수신
#   → 자동 재기동 1사이클을 더 돈다). 종전 "배정 전 부팅 = sy01b 조립 → NVM 기록 위험" 서술은
#   수정 전 거동이다 — 미선언 폴백 sy01b 는 코드에서 금지됐다(bootstrap 조립 테스트가 강제).
_MANAGED_KEYS="SENLYT_SERVER_BASE_URL SENLYT_INSTALL_REF SENLYT_INSTALL_COMMIT SENLYT_RUN LOG_DIR SENLYT_LEDGER_PATH SENLYT_STATE_DIR PUMP_ADDRESSES SENLYT_ENGINE"
_PRESERVED=""
if [ -f "$ENV_FILE" ]; then
  # `|| [ -n "$line" ]` — 끝 개행 없는 파일의 마지막 줄 보존(검증 P2-E: read 가 EOF 에서 false 를
  #   돌려 마지막 키가 조용히 증발하던 실결함). 선행 공백도 벗긴다(systemd EnvironmentFile 은
  #   들여쓴 `  KEY=v` 를 허용 — 안 벗기면 그 줄이 손실된다).
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"   # 선행 공백 제거(POSIX 파라미터 확장).
    case "$line" in
      [A-Z_]*=*)
        key="${line%%=*}"
        managed=0
        for mk in $_MANAGED_KEYS; do [ "$key" = "$mk" ] && managed=1; done
        [ "$managed" = 0 ] && _PRESERVED="${_PRESERVED}${line}
" ;;
    esac
  done < "$ENV_FILE"
  # 키 기준 dedupe(마지막 값 승리·최초 등장 순서 유지) — 행 전체 sort -u 는 같은 키의 옛 값을
  #   남겨 systemd EnvironmentFile(마지막 값 채택)과 어긋날 수 있다(검증 P2-2).
  _PRESERVED=$(printf '%s' "$_PRESERVED" | awk -F= 'NF{v[$1]=$0; if(!seen[$1]++){order[++n]=$1}} END{for(i=1;i<=n;i++) print v[order[i]]}')
fi
cat > "$ENV_FILE" <<EOF
# hey senlyt pi — 설치가 각인한 값. 설정은 **서버 URL 하나뿐**이다(2026-09-02 env 다이어트).
#   SENLYT_INSTALL_REF / SENLYT_INSTALL_COMMIT 두 줄은 "무엇이 깔렸나" 기록(2026-09-11) — 데몬은 읽지 않는다.
#   deviceId=HW시리얼 자동 · mode=admin 승인 시 배정 · 펌프모델/포트=admin 센소리움 선언 ·
#   펌프주소=부팅 버스 스캔 자동인식 · 경로/런스위치=systemd 유닛(Environment=).
#   여기에 KEY=값 을 추가하면 유닛 기본값을 덮는다(운영자 override).
#   ⚠️ 보존 범위: 설치기 관리 키(_MANAGED_KEYS — SENLYT_RUN·LOG_DIR·SENLYT_STATE_DIR·
#   SENLYT_LEDGER_PATH·PUMP_ADDRESSES·SENLYT_ENGINE)는 **재설치가 걷어낸다** — 그 키의
#   override 는 다음 재설치 전까지만 유효하다. 그 외 키(SENLYT_VALVE_* 등)만 재설치에도 보존.
SENLYT_SERVER_BASE_URL=$SERVER_URL
SENLYT_INSTALL_REF=$REF
SENLYT_INSTALL_COMMIT=$INSTALLED_SHA
EOF
if [ -n "$_PRESERVED" ]; then
  {
    echo "# ── 운영자 보존 값(재설치 유지 — 설치기가 관리하지 않는 키는 여기로 이월) ──"
    printf '%s\n' "$_PRESERVED"
  } >> "$ENV_FILE"
fi

# ── 5. systemd 유닛 — 부팅 자동시작 + 무인 복구(Restart=always) ────────────
cat > "$SERVICE" <<EOF
[Unit]
Description=hey senlyt pi daemon (senlytd)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# 서비스 배선 값(설정 아님 — env 다이어트 2026-09-02). 소비루프 스위치 + 설치 표준 경로.
#   정체성은 서버(환경)별 분리 저장(\$SENLYT_STATE_DIR/identities/{서버host}.json — 데몬이 서버
#   URL 로 파일명 파생·2026-07-23): 서버를 바꿔 재설치해도 각 서버의 등록·승인이 보존된다.
# EnvironmentFile 은 **선언 위치와 무관하게** Environment= 를 덮는다(systemd.exec(5):
#   "Settings from these files override settings made with Environment=" — 순서 규칙은
#   EnvironmentFile **끼리**에만 적용). 그래서 device.env 의 같은 키가 항상 이긴다 —
#   아래 배치 순서는 가독성이지 우선순위 장치가 아니다.
Environment=SENLYT_RUN=1
Environment=LOG_DIR=$LOG_DIR
Environment=SENLYT_STATE_DIR=$STATE_DIR
Environment=SENLYT_LEDGER_PATH=$STATE_DIR/queue/idempotency-ledger.log
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/senlytd
Restart=always
RestartSec=5
# GPIO/시리얼 접근을 위해 root 실행(단일 목적 기기). 로그는 journald.

[Install]
WantedBy=multi-user.target
EOF

# ── 6. 기동 ────────────────────────────────────────────────────────────────
#   restart 사용(enable --now 아님) — 재실행 시 **이미 켜진 서비스는 --now 로 재시작되지 않아** 옛 코드가
#   계속 돈다. restart 는 꺼져 있으면 시작·켜져 있으면 새 코드로 재시작(멱등 재실행 정확성).
systemctl daemon-reload
systemctl enable -q senlytd
systemctl restart senlytd

ADMIN_URL="${SERVER_URL%/}/admin"
echo ""
echo "✅ 설치·기동 완료 — senlytd 가 부팅 자동시작으로 돕니다."
echo "   상태:  systemctl status senlytd --no-pager"
echo "   로그:  journalctl -u senlytd -f     (\"하드웨어 자가진단\" 줄로 engine/valve 확인)"
echo ""
echo "👉 다음(마지막 한 걸음): 이 기기가 서버 admin에 \"승인 대기\"로 나타납니다."
echo "   $ADMIN_URL 에서 이 기기를 \"승인 + 모드 배정\" 하면 online 됩니다."
echo "   (승인 전에는 \"승인 대기\"로 폴링만 합니다 = 정상)"
