# 구조도

이 폴더에는 [Archify](https://github.com/tt-a1i/archify)로 만든 연구 나침반 구조도가 들어 있습니다. 각 그림은 원본 JSON(`*.json`)과 브라우저로 바로 여는 HTML(`*.html`)로 이루어집니다.

## 그림 목록

| 그림 | 근거 | 기준 | 상태 |
|---|---|---|---|
| [코드 구조 (9단계, 코드 근거)](code-v0.1.html) | 코드 근거. 노드마다 SRC 링크 | 커밋 `b23aadc` (9단계 완료, 2026-11-24) | 현재 유효 |
| [계획 → 코드 비교](compare-planned-to-code-v0.1.html) | `archify compare` 결과 | `planned-v0.1.json` → `code-v0.1.json` | 참고 |
| [v0.1 구조 (계획 단계 예상)](planned-v0.1.html) | 계획서만. 코드 근거 없음 | [구현 계획서](../implementation-plan.md) 6절, 2026-11-20 | 코드 근거 구조도로 대체 |

### 코드 구조 (9단계, 코드 근거)

**이 그림에서 볼 것**: 연구자의 브라우저 요청은 먼저 **LocalOnly 미들웨어**를 지나야 합니다. 이 미들웨어는 Host가 127.0.0.1이나 localhost인지, 상태를 바꾸는 요청의 Origin이 같은 서버인지 확인합니다. 통과한 요청은 **웹 라우트**가 받아 네 서비스로 나눠 보냅니다. **기록 서비스**는 실험, 실행, 결정, 기준선을 SQLite에 쓰고, **첨부·지문**은 파일을 sha256 주소로 저장하거나 외부 데이터를 읽기 전용으로 해시합니다. **인계·결과 블록**은 인계 Markdown을 만들고, 확인한 결과 블록만 기록 서비스로 넘깁니다. **백업·복구**는 DB 스냅샷과 기록이 가리키는 첨부만 zip에 담습니다. 터미널의 **rcompass CLI**는 서버를 띄우고, 백업·복구와 인계를 같은 서비스로 처리합니다.

**읽는 법**: 노드를 누르면 근거 파일과 줄 범위(SRC)가 나오고, GitHub의 `b23aadc` 커밋으로 이동합니다. CLI에서 인계로 가는 선과 웹에서 백업으로 가는 선이 한 번 교차합니다. CLI가 내부 서비스 세 곳을 부르기 때문에 생기는 교차이고, 연결 관계는 정확합니다.

**빠진 것**: 10단계 구조도 등록(sandbox iframe으로 보는 화면)은 이 커밋 뒤의 변경이라 그림에 없습니다. 11단계를 마친 뒤 다시 만듭니다.

### 계획 → 코드 비교

[비교 그림](compare-planned-to-code-v0.1.html)은 계획 단계 예상 구조와 실제 코드 구조의 차이를 보여줍니다. 계획에서는 "서비스 계층" 하나였던 상자가 실제로는 기록, 첨부·지문, 인계·결과 블록, 백업·복구 네 서비스로 나뉘었습니다. 웹 앞단에는 Host·Origin을 검사하는 미들웨어가 생겼고, 백업 zip 저장소가 데이터 디렉터리에 더해졌습니다. "Archify 스킬 → 웹 화면" 연결은 10단계에서 구현하므로 비교 그림에서는 빠진 것으로 나옵니다.

### v0.1 구조 (계획 단계 예상)

**이 그림에서 볼 것**: 연구자는 브라우저로 **웹 화면**에 접속합니다. 화면의 모든 동작은 **서비스 계층**을 거칩니다. 서비스 계층은 결정, 기준선, 실행 집계, 계보, 인계, 백업을 맡습니다. 기록은 **저장소 밖의 데이터 디렉터리**에 남는데, 구조화된 기록은 SQLite에, 그래프와 설정 같은 첨부 파일은 sha256 주소의 파일 저장소에 저장됩니다. 대용량 외부 데이터는 복사하지 않고 위치와 지문만 적어 둡니다. 터미널의 **rcompass CLI**도 같은 서비스 계층을 쓰므로, 웹 화면과 CLI의 결과가 달라지지 않습니다. Claude Code와는 직접 연결되지 않습니다. 연구자가 인계 Markdown을 붙여넣고, 돌아온 결과 블록을 웹 화면에서 확인한 뒤 기록합니다.

**주의**: 대부분 아직 구현 전입니다. 현재 구현된 것은 0단계 골격(웹 빈 화면, `rcompass serve`, SQLite 연결)뿐입니다. 구현 단계가 끝나면 푸시된 커밋에 고정한 코드 근거 구조도를 추가하고, 이 그림과의 차이를 `compare`로 남깁니다.

## Archify 확인 기록 (v3.2.0, 2026-11-20)

계획서 1절에서 "확인하지 못한 것"으로 남겨 둔 항목을 설치한 스킬의 스키마로 확인했습니다.

- **노드 필드**: Architecture 컴포넌트는 `id`, `type`, `label`, `sublabel`, `tag`, `icon`, `brand`, `sources`, `row`/`col`/`pos`/`size`만 받습니다(`additionalProperties: false`). **설명문 필드는 없습니다.** 그래서 쉬운 한국어 설명은 그림의 `cards`와 이 README에 둡니다.
- **경계 종류**: `region`, `security-group` 두 가지뿐입니다. "계획 단계" 표시는 경계가 아니라 제목과 카드로 합니다.
- **코드 근거**: `meta.repository`는 `url`과 40자 `revision`이 필수이고, `provider`(github, gitee, gitlab)와 `link_mode`(`web`, `local-only`)를 선택할 수 있습니다. `sources`는 노드당 1~3개이며 각각 `path`, `line`, `end_line`, `label`을 가집니다.
- **한국어**: `meta.locale: "ko"`가 화면 UI(범례, 버튼)를 한국어로 바꿉니다. 그림 내용은 작성한 언어 그대로 표시됩니다.

## 스킬 설치와 재생성

스킬 패키지는 저장소에 포함하지 않습니다(약 18MB). 설치 출처와 해시는 루트의 `skills-lock.json`에 기록됩니다.

```bash
npx -y skills@latest add https://archify.si --skill archify --agent claude-code --copy --yes
node .claude/skills/archify/bin/archify.mjs doctor
node .claude/skills/archify/bin/archify.mjs finalize architecture docs/architecture/planned-v0.1.json docs/architecture/planned-v0.1.html --quality showcase --json
node .claude/skills/archify/bin/archify.mjs finalize architecture docs/architecture/code-v0.1.json docs/architecture/code-v0.1.html --repo-root . --quality showcase --json
node .claude/skills/archify/bin/archify.mjs compare architecture docs/architecture/planned-v0.1.json docs/architecture/code-v0.1.json docs/architecture/compare-planned-to-code-v0.1.html --repo-root . --json
```
