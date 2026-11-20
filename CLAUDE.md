# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트

연구 나침반(`rcompass`)은 로컬에서 혼자 연구하는 사람을 위한 연구 기록 웹앱입니다. 다루는 흐름은 연구 질문, 문제 트리, 실험과 실행(Run), 결정(채택·기각·보류·철회), 기준선, 자료 계보, Claude Code 인계입니다. 설계와 단계별 구현 순서는 [docs/implementation-plan.md](docs/implementation-plan.md)에 있고, 작업할 때는 이 계획서의 단계 번호(6절)를 기준으로 진행합니다. 현재 2단계(프로젝트, 성공 기준, 기준선 0 만들기 화면)까지 끝났습니다.

UI 문구, 문서, 사용자에게 보이는 메시지는 한국어로 씁니다.

## 명령

```bash
uv sync                                   # 의존성 설치 (.venv 생성)
uv run rcompass serve --data-dir <폴더>    # http://127.0.0.1:8765 (빈 DB는 시작 시 자동 생성)
uv run rcompass migrate --data-dir <폴더>  # 기존 DB를 최신 스키마로
uv run pytest                             # 전체 테스트
uv run pytest tests/test_cli.py::test_invalid_port_is_rejected   # 단일 테스트
```

이 개발 PC에서는 `uv`가 PATH에 없으므로 `python -m uv ...`로 실행합니다. 수동으로 확인할 때는 `--data-dir`을 임시 폴더로 지정해 실제 사용자 데이터 경로를 건드리지 않습니다.

## 구조와 불변 규칙

- **데이터는 저장소 밖에 둡니다.** [config.py](src/rcompass/config.py)의 `resolve_data_dir`가 데이터 경로를 정하고, 우선순위는 `--data-dir`, `RCOMPASS_DATA_DIR`, platformdirs 기본값(`%LOCALAPPDATA%\rcompass`) 순서입니다. 데이터 경로가 소스 체크아웃 안에 있으면 CLI와 홈 화면 모두 경고합니다. 테스트에서는 `env=` 인자나 `tmp_path`를 써서 실제 환경변수와 사용자 경로에 의존하지 않습니다.
- **앱 팩토리** [web/app.py](src/rcompass/web/app.py)의 `create_app(settings)`는 엔진과 DB 파일을 lifespan 시작 시점에만 만듭니다(`app.state.sessions`도 이때 생김). 그래서 페이지 테스트는 항상 `with TestClient(app, base_url="http://127.0.0.1:8765")`로 씁니다. Host 검사 때문에 `base_url`이 필요합니다.
- **웹 계층 규칙**:
  - 라우트는 [web/routes/](src/rcompass/web/routes/)에 두고, 도메인 로직은 [services/](src/rcompass/services/)에 둡니다. 서비스는 `ValidationError(errors={필드: 한국어 메시지})`를 던지고, 커밋은 호출한 쪽이 합니다.
  - `async def` 라우트에서는 DB를 직접 쓰지 말고 `run_in_threadpool`로 넘깁니다. 모든 트랜잭션이 `BEGIN IMMEDIATE`라서, 이벤트 루프에서 잠금을 기다리면 서버 전체가 멈춥니다. 동기 `def` 라우트는 `get_session` 의존성을 씁니다.
  - [security.py](src/rcompass/web/security.py)의 `LocalOnlyMiddleware`가 Host(127.0.0.1/localhost만)와 상태 변경 요청의 Origin/Referer를 검사합니다. CSRF와 DNS 리바인딩을 막기 위한 것입니다.
  - 오류 페이지는 `error.html`로 한국어로 보여줍니다.
- **CLI 진입점**은 `rcompass.cli:run`입니다. Click이 도움말을 출력하기 전에 `ensure_utf8_output`으로 stdout과 stderr를 UTF-8로 바꿉니다. 이것이 없으면 Windows에서 출력을 리디렉션할 때 한국어 출력에서 `UnicodeEncodeError`가 납니다. `--data-dir`는 빈 문자열이 `Path(".")`로 바뀌지 않도록 `str`로 받습니다.
- 서버는 `127.0.0.1`에만 바인딩합니다. 인증이 없는 단일 사용자 앱이기 때문입니다.
- **DB와 트랜잭션** ([db.py](src/rcompass/db.py)):
  - 엔진은 드라이버의 자동 BEGIN을 끄고, 모든 트랜잭션을 `BEGIN IMMEDIATE`로 시작합니다.
  - 이렇게 해야 마이그레이션이 한 트랜잭션으로 묶이고(중간에 실패해도 반쯤 만든 표가 남지 않음), 동시 쓰기에서 SQLITE_BUSY가 나지 않습니다. 이 설정을 되돌리지 마세요.
  - `open_database`는 빈 DB만 자동으로 만들고, 기록이 있는 이전 버전 DB는 `SchemaError`로 거부합니다. 업그레이드는 `rcompass migrate`로만 합니다.
- **마이그레이션**:
  - `src/rcompass/migrations/`에 있습니다. 설치된 패키지에서도 `migrate`가 동작해야 하므로 패키지 안에 둡니다.
  - 새 마이그레이션은 `alembic.ini` 상단 주석의 명령으로 임시 DB에 autogenerate한 뒤 직접 검토합니다.
  - `alembic.ini`는 ASCII만 씁니다. Alembic이 로케일 인코딩으로 읽어 Windows에서 한글이 깨집니다.
  - `test_migration_matches_models`가 모델과 마이그레이션의 차이를 잡습니다.
- **추가만 가능한 표**(`models.APPEND_ONLY_TABLES`: 결정, 기준선, 기준선 실행 스냅샷, 기준선 변경, 실행 제외):
  - SQLite 트리거가 UPDATE와 DELETE를 막습니다.
  - 배치 마이그레이션은 표를 다시 만들며 트리거를 지우므로, `upgrade_to_head`가 끝날 때 `ensure_append_only_triggers`가 되살립니다. 추가만 가능한 표를 새로 만들면 `APPEND_ONLY_TABLES`에 넣으세요.
- **짧은 ID**([ids.py](src/rcompass/ids.py)): 모두 1부터 발급합니다. `E0`(기준선 측정 실험)과 `B0`은 프로젝트를 만들 때 `BASELINE_MEASUREMENT_ID`와 `BASELINE_ZERO_ID`로 직접 붙입니다. 실행 ID는 `scope=f"R:{experiment_id}"`로 실험마다 R1부터 시작합니다.
- 이후 단계에서 지킬 도메인 규칙(계획서 4절):
  - `Decision`과 `BaselineChange`는 추가만 가능하고, 수정하거나 삭제하는 API를 만들지 않습니다.
  - 채택하면 `Baseline.run_ids`에 실행 스냅샷을 고정합니다. 채택 뒤 실행을 추가하거나 제외해도 기준선 수치는 바뀌지 않습니다.
  - 성공 기준 상태는 `미측정`, `근거 부족`(`min_runs` 미달), `미달`, `달성` 네 가지이고, 작업 진행률과 목표 달성은 따로 계산합니다.
  - 앱은 연구 저장소의 Git이나 원본 파일을 절대 읽거나 쓰지 않습니다. 커밋 SHA는 텍스트로만 다룹니다.
- 외부 LLM API 없이 동작해야 합니다. LLM 연동은 선택 기능으로만 추가합니다.

## 단계 완료 기준

계획서 각 단계의 "완료 기준"을 채운 뒤 다음을 거쳐야 완료로 봅니다.

- `run` 스킬로 실제 서버를 띄워 해당 화면이나 엔드포인트를 확인합니다.
- `code-review` 스킬로 변경분을 검토합니다.
- 5단계(파일 업로드), 9단계(백업 zip 복구), 10단계(구조도 HTML iframe)는 `security-review`도 거칩니다.

## Archify 구조도

- 위치: `docs/architecture/`. 원본 JSON과 생성된 HTML을 커밋하고, 그림 설명은 [docs/architecture/README.md](docs/architecture/README.md)에 한국어로 씁니다.
- 스킬 패키지(`.claude/skills/archify/`, 약 18MB)는 gitignore되어 있습니다. 없으면 다음 명령으로 프로젝트 로컬에 설치합니다.

  ```bash
  npx -y skills@latest add https://archify.si --skill archify --agent claude-code --copy --yes
  node .claude/skills/archify/bin/archify.mjs doctor
  ```

- 생성할 때는 스킬의 `SKILL.md` 절차를 따릅니다. 저장소 루트에서 다음을 실행합니다.

  ```bash
  node .claude/skills/archify/bin/archify.mjs finalize architecture docs/architecture/<이름>.json docs/architecture/<이름>.html --quality showcase --json
  ```

  `finalize`가 남기는 `*.finalize*.json`, `*.browser-check.json`, `*.delivery.json` 영수증에는 로컬 절대 경로가 들어 있으므로 커밋하지 않습니다(gitignore됨).
- 노드에는 설명 필드가 없습니다. 쉬운 설명은 `cards`와 README에 넣습니다. "계획 단계 예상" 그림은 제목과 카드에 그 사실을 밝힙니다.
- 코드 근거 그림에는 `meta.repository`(원격 URL과 40자 커밋 SHA)와 노드별 `sources`(1~3개)를 넣고, `--repo-root .`로 finalize합니다. 원격에 **푸시된 커밋**에서만 만듭니다.
