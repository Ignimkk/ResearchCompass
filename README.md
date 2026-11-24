# 연구 나침반 (rcompass)

로컬에서 혼자 연구하는 사람을 위한 연구 기록 웹앱입니다. 원래 연구 질문, 하위 문제, 실험과 판정(채택·기각·보류·철회), 기준선 변화, 자료의 출처를 한 흐름으로 기록합니다. 그리고 그 상태를 Claude Code 새 세션에 넘겨줄 수 있게 합니다. 핵심 기능은 외부 LLM API 키 없이 동작합니다.

> 현재 상태: 개발 초기(v0.0.1). 실행 골격만 있고, 연구 기록 기능은 단계적으로 추가되고 있습니다.

## 빠른 시작

```bash
git clone <이 저장소 URL> rcompass && cd rcompass
uv sync
uv run rcompass init --example   # 예제 프로젝트 "QA 평가 오답 줄이기" (생략 가능)
uv run rcompass serve
```

브라우저에서 <http://127.0.0.1:8765>을 엽니다. 서버는 내 컴퓨터(127.0.0.1)에서만 접속할 수 있습니다.

`uv`가 없으면 `python -m pip install --user uv`로 설치하세요. pip만 쓰려면 이렇게 합니다.

```bash
python -m venv .venv && .venv/Scripts/activate   # macOS/Linux: source .venv/bin/activate
pip install -e . pytest httpx
rcompass serve
```

## 데이터는 어디에 저장되나요?

DB와 첨부 파일은 **저장소 밖**에 저장되므로 Git에 올라가지 않습니다.

| 우선순위 | 지정 방법 | 예 |
|---|---|---|
| 1 | `--data-dir` 옵션 | `uv run rcompass serve --data-dir D:/research-data` |
| 2 | `RCOMPASS_DATA_DIR` 환경변수 | |
| 3 | 기본 사용자 데이터 경로 | Windows `%LOCALAPPDATA%\rcompass`, macOS `~/Library/Application Support/rcompass`, Linux `~/.local/share/rcompass` |

## 백업, 복구, 업데이트

```bash
uv run rcompass backup                         # <데이터 디렉터리>/backups/rcompass-backup-YYYYMMDD-HHMM.zip
uv run rcompass restore <zip> --data-dir <빈 폴더>   # 서버를 끈 상태에서
git pull && uv sync && uv run rcompass migrate # 업데이트: migrate가 먼저 자동 백업합니다
```

- 백업 zip에는 DB와 첨부 파일이 들어갑니다. 웹 화면의 "백업"에서도 만들고 내려받을 수 있습니다.
- 대용량 외부 데이터(로컬·NAS 경로, URL)는 위치와 지문만 기록되고 백업에 들어가지 않습니다. 따로 보관하세요. 복구가 끝나면 외부 참조마다 "접근 가능", "없음", "지문 불일치"를 알려줍니다. NAS 같은 네트워크 경로(UNC)는 직접 만든 백업일 때만 `--check-network`를 붙여 확인하세요.
- 복구는 레코드 수와 모든 파일의 sha256을 확인한 뒤에만 데이터를 바꿉니다. 이미 기록이 있는 폴더는 거부하고, `--force`를 붙이면 기존 데이터를 먼저 백업한 뒤 바꿉니다.

## 구조도 (Archify)

Claude Code에서 [Archify](https://github.com/tt-a1i/archify) 스킬로 만든 구조도 HTML을 프로젝트의 "구조도" 탭에 올리면, 근거 구분(계획 단계 예상 / 코드 근거), 커밋, 관련 파일, 한국어 설명과 함께 볼 수 있습니다.

- 코드 근거 구조도는 **원격에 푸시된 커밋**에서 만들고 전체 커밋 SHA와 근거 파일을 적습니다. JSON IR을 같이 올리면 이 값들을 IR에서 채웁니다.
- 구조도는 앱과 분리된 격리 영역(sandbox iframe)에서 열립니다. 그림 안의 스크립트는 앱의 기록을 읽거나 바꿀 수 없습니다.
- 연구 저장소 구조도의 커밋이 현재 기준선의 코드 커밋과 다르면 "기준선과 어긋남" 배지가 붙습니다.
- 이 앱 자체의 구조도는 [docs/architecture/](docs/architecture/README.md)에 있습니다.

## 개발

```bash
uv run pytest
```

## 라이선스

[Apache-2.0](LICENSE)
