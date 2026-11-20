# 연구 나침반 (rcompass)

로컬에서 혼자 연구하는 사람을 위한 연구 기록 웹앱입니다. 원래 연구 질문, 하위 문제, 실험과 판정(채택·기각·보류·철회), 기준선 변화, 자료의 출처를 한 흐름으로 기록합니다. 그리고 그 상태를 Claude Code 새 세션에 넘겨줄 수 있게 합니다. 핵심 기능은 외부 LLM API 키 없이 동작합니다.

> 현재 상태: 개발 초기(v0.0.1). 실행 골격만 있고, 연구 기록 기능은 단계적으로 추가되고 있습니다.

## 빠른 시작

```bash
git clone <이 저장소 URL> rcompass && cd rcompass
uv sync
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

## 개발

```bash
uv run pytest
```

## 라이선스

[Apache-2.0](LICENSE)
