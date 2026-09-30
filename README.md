# heysenlyt-pi

hey_senlyt **v1.4.0** 디스펜서 데몬(`senlytd`). 라즈베리파이에서 돌며, 서버에서 제조 명령을 받아 펌프·밸브를 움직이고 결과를 서버에 보고합니다.

---

## 1. 설치 (라즈베리파이에서 한 줄)

```bash
REF=v1.4.0; curl -fsSL "https://raw.githubusercontent.com/joomidang-tech/heysenlyt_pi/$REF/install.sh" \
  | sudo env SENLYT_INSTALL_REF=$REF bash -s -- https://senlyt.com
```

정하는 것은 두 가지뿐입니다.

| 무엇 | 어디 | 예 |
|---|---|---|
| 설치할 pi 코드 버전 | `REF=` | `v1.4.0` · `dev` · `main` · 40자리 커밋 SHA |
| 붙을 서버 | 맨 끝 URL | prod `https://senlyt.com` · dev `https://dev-env.senlyt.com` · 프리뷰 `https://v1-4-0.env.senlyt.com` |

> ⚠️ `REF` 는 **URL 경로와 `SENLYT_INSTALL_REF` 두 곳에 같은 값**이 들어가야 합니다(그래서 변수 하나로 씁니다). 설치 스크립트도 버전마다 바뀌어, `main` 의 스크립트로 새 버전을 깔면 제대로 뜨지 않습니다.

설치가 끝나면:
1. 관리 화면(`<서버URL>/admin`)에 기기가 **"승인 대기"** 로 뜹니다.
2. 운영자가 **승인 + 모드 배정**을 하면 online 이 됩니다.
3. 기기 ID·펌프 기종(Runze SY-01B / Tecan XCalibur)·밸브는 **자동 인식**됩니다. 사람이 넣을 값은 없습니다.

- 설치 로그 첫 줄 `▶ hey senlyt pi 설치 — server=… (ref=…)` 로 서버·버전을 확인합니다.
- 설치된 버전은 `/etc/senlyt/device.env` 의 `SENLYT_INSTALL_REF`·`SENLYT_INSTALL_COMMIT` 에 남습니다.
- **업데이트 = 같은 한 줄을 다시 실행**합니다(멱등 — 받아서 재기동까지).

---

## 2. 운영 명령

```bash
sudo systemctl status senlytd --no-pager   # 상태
sudo systemctl stop senlytd                # 데몬 멈춤 (기기는 켜져 있음)
sudo systemctl start senlytd               # 다시 시작
sudo systemctl disable --now senlytd       # 멈춤 + 부팅 자동시작 해제 (되돌리기: enable --now)

journalctl -u senlytd -f                   # 실시간 로그 ("하드웨어 자가진단" 줄 = 펌프·밸브 인식 결과)
journalctl -u senlytd -b > ~/senlytd-$(date +%F-%H%M).log   # 이번 부팅 로그 저장

sudo shutdown -h now                       # 전원 끄기 (초록 LED 가 멎은 뒤 코드를 뽑는다)
sudo reboot                                # 재부팅
```

> ⛔ **제조·정비 중에는 전원을 뽑지 않습니다.** 펌프가 위치를 잃습니다. `systemctl stop senlytd` → 모션이 끝난 뒤 `shutdown` 순서로 내립니다.
>
> ⚠️ **전원을 급히 뽑으면 그 부팅의 로그가 사라집니다**(로그가 메모리에만 저장되는 기본 설정). 이상이 보이면 전원을 내리기 **전에** 위의 "이번 부팅 로그 저장" 명령으로 먼저 받아 두세요.

---

## 3. 개발

배포되는 것은 **Python 데몬**(`src/senlyt_pi/`)입니다. `lib/`(Dart)는 계약 대조용 기준 구현이고 배포하지 않습니다.

```bash
pip install -e .   # senlytd 설치
pytest             # Python 테스트
dart test          # Dart 기준 구현 대조 테스트
```

```
src/senlyt_pi/
├── core/         # 계약(와이어 모델·주문 상태·펌프 가드) — 서버와 바이트 동일
├── ports/        # 인터페이스
├── adapters/     # 서버 통신·펌프 엔진(SY-01B·Tecan)·설정 감시·기기 등록
├── pipeline/     # 레시피 해석·펌프 시퀀서·상태 보고·오프라인 큐
├── persistence/  # 멱등 원장·하드웨어 설정 캐시
└── app/          # 조립·진입점(senlytd)
```

- 계약 정본: `developer/hey_senlyt/v1.4.0/04_erd/hey_senlyt_erd.md`
- 펌프 수치 정본: `heysenlyt-web` `server/domain/pumpGuard.ts`·`settingsClamp.ts`

### 지켜야 할 것

- pi 는 **DB에 직접 붙지 않습니다** — 서버 스트림을 받고, 결과는 API로만 보고합니다.
- 주문 상태를 앞으로 옮기는 것은 **pi 만** 합니다.
- 다시 제조할 땐 **attempt 를 올립니다**(`{orderId}:{attempt}` 로 중복 방지).
- 개인정보(이름·연락처 등)는 pi 로 **내려오지 않습니다**.
- 토큰 서명·검증은 **서버 몫**입니다. pi 는 받은 토큰을 그대로 씁니다.
