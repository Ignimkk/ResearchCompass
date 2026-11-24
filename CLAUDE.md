# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트

연구 나침반(`rcompass`)은 로컬에서 혼자 연구하는 사람을 위한 연구 기록 웹앱입니다. 다루는 흐름은 연구 질문, 문제 트리, 실험과 실행(Run), 결정(채택·기각·보류·철회), 기준선, 자료 계보, Claude Code 인계입니다. 설계와 단계별 구현 순서는 [docs/implementation-plan.md](docs/implementation-plan.md)에 있고, 작업할 때는 이 계획서의 단계 번호(6절)를 기준으로 진행합니다. 현재 8단계(Claude Code 인계와 결과 블록)까지 끝났습니다.

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
  - 프로젝트 안의 모든 화면은 [context.py](src/rcompass/web/context.py)의 `project_context(...)` 결과를 템플릿에 펼쳐 넣고, `_context_bar.html`을 include합니다. 이 바에 원래 질문, 경로, 돌아갈 곳, 현재 기준선이 나옵니다.
    - `path`는 연구 질문 아래부터 해당 문제까지의 경로입니다(`problems.problem_path`).
    - 문제 아래의 하위 화면(예: 새 하위 문제, 실험)은 `tail=[Crumb(...)]`을 주면, 돌아갈 곳이 그 문제가 됩니다.
  - 문제는 URL에서 짧은 ID로 찾습니다(`/projects/{id}/problems/P3`). 실험도 같은 방식을 따릅니다.
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
  - `test_migration_matches_models`가 모델과 마이그레이션의 차이를 잡습니다. 이전 리비전에서 올라가는 경로는 `tests/test_migrations.py`에서 검증합니다.
  - 다른 표가 외래 키로 참조하는 표(예: `runs`)에 제약을 더할 때는 `batch_alter_table`(표 재생성)을 피하고 `op.create_index(..., unique=True)`를 씁니다. 외래 키 검사가 켜진 상태에서는 표를 다시 만들다 실패할 수 있습니다(0002 참고).
- **추가만 가능한 표**(`models.APPEND_ONLY_TABLES`: 결정, 기준선, 기준선 실행 스냅샷, 기준선 변경, 실행 제외):
  - SQLite 트리거가 UPDATE와 DELETE를 막습니다.
  - 배치 마이그레이션은 표를 다시 만들며 트리거를 지우므로, `upgrade_to_head`가 끝날 때 `ensure_append_only_triggers`가 되살립니다. 추가만 가능한 표를 새로 만들면 `APPEND_ONLY_TABLES`에 넣으세요.
- **결과 기록**([services/runs.py](src/rcompass/services/runs.py)):
  - 결과는 "실행 × 지표" 표(첫 줄 머리글, 탭·쉼표·공백 구분)로 받습니다. 실행은 실험 안에서 이름으로 찾습니다(이름은 고유 인덱스).
  - 기준선 스냅샷(`BaselineRun`)에 든 실행은 값이든 메타데이터든 다시 쓰지 않습니다.
  - 기준선 측정 실험(`ExperimentKind.BASELINE_MEASUREMENT`)에 새 실행을 기록하면, 그 실험이 재는 기준선(`measured_baseline`)이 아직 현재 기준선일 때만 `snapshot_update`로 근거를 넓힙니다.
    - E0은 B0을 재고, 지표에 제한이 없습니다.
    - 나중에 만든 측정 실험(`create_measurement`)은 만들 때의 현재 기준선을 재고, 그 기준선이 아직 재지 않은 지표만 기록할 수 있습니다. 채택한 기준선이 이미 가진 수치는 바뀌지 않습니다.
- **첨부 파일**([services/files.py](src/rcompass/services/files.py)):
  - 내용은 `<data_dir>/files/sha256/<앞 2자리>/<sha256>`에 저장합니다. 사용자가 준 파일 이름은 경로에 쓰지 않고, DB의 `rel_path`는 `blob_path`가 검증한 뒤에만 엽니다.
  - 같은 내용은 디스크에 한 번만 저장되고, `FileObject` 기록은 첨부할 때마다 새로 만듭니다.
  - 업로드 라우트는 **DB를 건드리기 전에** `store_stream`으로 저장합니다. 복사와 해시 계산 동안 `BEGIN IMMEDIATE` 쓰기 잠금을 잡지 않기 위해서입니다. 실패하거나 404가 나면 `discard_if_unreferenced`로 고아 내용을 지웁니다.
  - 외부 참조의 지문 계산도 같은 이유로 "대상 확인 → `session.rollback()` → 해시 계산 → 짧은 트랜잭션으로 기록" 순서를 지킵니다(`verification_target`, `compute_fingerprint`, `apply_fingerprint`). 오래 걸리는 파일 입출력은 트랜잭션 밖에서 합니다.
  - 파일을 내려줄 때 PNG·JPEG·GIF·WebP와 텍스트만 inline으로 보내고, 나머지(SVG·HTML·PDF 등)는 `attachment`로 보냅니다. 모든 응답에 `nosniff`와 `CSP sandbox`를 붙입니다.
  - 요청 크기는 `BodySizeLimitMiddleware`(순수 ASGI)가 받는 바이트를 세어 넘으면 `HTTPException(413)`을 던집니다. chunked 요청도 이렇게 막습니다.
  - 지문 계산은 로컬·NAS 절대 경로와 `file://`(UNC 포함)만 대상으로 합니다. 상대 경로는 서버의 현재 폴더(소스 저장소일 수 있음)를 기준으로 엉뚱한 파일을 읽을 수 있어 계산하지 않습니다. 원본은 읽기만 합니다.
- **짧은 ID**([ids.py](src/rcompass/ids.py)): 모두 1부터 발급합니다. `E0`(기준선 측정 실험)과 `B0`은 프로젝트를 만들 때 `BASELINE_MEASUREMENT_ID`와 `BASELINE_ZERO_ID`로 직접 붙입니다. 실행 ID는 `scope=f"R:{experiment_id}"`로 실험마다 R1부터 시작합니다.
- **결정과 기준선**([services/decisions.py](src/rcompass/services/decisions.py)):
  - `Decision`과 `BaselineChange`는 추가만 가능하고, 수정하거나 삭제하는 API를 만들지 않습니다. 판단을 뒤집을 때는 철회(새 결정)를 추가합니다.
  - 채택하면 새 기준선을 만들고, 그때 집계에 든 실행(제외하지 않았고 수치가 있는 실행)을 `BaselineRun` 스냅샷으로 고정합니다. 채택 뒤 실행을 추가하거나 제외해도 기준선 수치는 바뀌지 않습니다.
  - `Project.current_baseline_id`는 캐시입니다. 기준선을 옮길 때는 같은 트랜잭션에서 `BaselineChange`를 추가해, 마지막 변경의 `to_baseline_id`와 항상 같게 둡니다.
  - 내릴 수 있는 결정은 `allowed_decisions`가 정합니다. 결정 전과 보류 상태에서는 채택·기각(보류는 한 번), 채택 상태에서는 철회만 됩니다. 기각과 철회는 마지막 판단입니다.
  - 채택은 `adoption_gate`(데이터 버전, 코드 버전, 설정, 측정한 기준마다 `min_runs`)를 통과해야 합니다. 기각과 보류는 빠진 기록을 경고만 합니다.
  - 철회의 기본 복귀 대상은 철회되지 않은 가장 가까운 조상입니다. 철회하는 기준선이 현재 기준선이 아니면 복귀 대상을 직접 고르게 합니다.
  - 판정한 실험은 가설과 판정 기준을, 채택했던 실험은 입력을 고칠 수 없습니다(`PLAN_LOCKED`, `INPUTS_LOCKED`).
  - 판정 폼에는 맨 앞에 비활성 제출 버튼을 둡니다. Enter 키로 되돌릴 수 없는 판정이 제출되는 것을 막기 위해서입니다.
- **Claude Code 연동**([services/handoff.py](src/rcompass/services/handoff.py), [services/result_block.py](src/rcompass/services/result_block.py), [integrations/claude-code/](integrations/claude-code/)):
  - 인계 Markdown은 계획서 5절 예시 구조를 따르고 `tests/test_handoff.py`의 스냅샷으로 고정합니다. 구조를 바꾸면 스냅샷과 `rcompass-result` 스킬을 함께 고칩니다.
  - 결과 블록은 PyYAML `compose`로 노드만 읽고 값은 글자 그대로 다룹니다(형 변환 없음, 오류마다 붙여넣은 텍스트 기준 줄 번호). 별칭(&, *)은 거부합니다.
  - 미리보기 라우트는 DB에 쓰지 않습니다. 기록은 "확인하고 기록"(`confirm_results`)에서만 하고 provenance는 `claude_confirmed`입니다.
  - 템플릿에 넣는 값은 `yaml_value`로 필요할 때 따옴표로 감쌉니다. 그대로 넣을 수 없는 값(여러 줄, 너무 긴 값)은 `<...>` 안내 문구로 두고, 안내 문구는 빈 값으로 읽습니다. 값을 줄여 넣으면 다른 값으로 기록되기 때문입니다.
- **프로젝트 홈**([services/dashboard.py](src/rcompass/services/dashboard.py)):
  - 목표 달성(현재 기준선 스냅샷의 판정)과 작업 진행(판정을 마친 실험 수, 해결한 문제 수)은 따로 계산하고 화면에서도 나눠 보여줍니다.
  - "최근 시도"는 지표를 마지막으로 기록한 시도 실험(E0 제외)입니다. 값을 다시 기록하면 `record_results`가 결과 행을 지우고 새로 만들어, `MetricResult.id` 순서가 기록 순서가 되게 합니다.
  - 다음 행동은 한 줄(`one_line`)이고 `MAX_NEXT_ACTION`자까지입니다. 프로젝트 생성과 홈 수정에 같은 규칙을 씁니다.
- 이후 단계에서 지킬 도메인 규칙(계획서 4절):
  - 성공 기준 상태는 `미측정`, `근거 부족`(`min_runs` 미달), `미달`, `달성` 네 가지이고, 작업 진행률과 목표 달성은 따로 계산합니다.
  - 앱은 연구 저장소의 Git이나 원본 파일을 절대 쓰지 않습니다. 원본을 읽는 것은 사용자가 누른 "지문 계산"(읽기 전용 해시)뿐입니다. 커밋 SHA는 텍스트로만 다룹니다.
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
