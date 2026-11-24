"""7단계: 프로젝트 홈 대시보드 (목표 달성과 작업 진행을 따로 계산)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rcompass.config import Settings
from rcompass.models import DecisionType, Project, ProblemStatus
from rcompass.services import ValidationError
from rcompass.services.dashboard import build_dashboard, latest_attempt, update_next_action, work_progress
from rcompass.services.datasets import add_dataset_version
from rcompass.services.decisions import decide, revoke_adoption
from rcompass.services.experiments import create_experiment, update_inputs
from rcompass.services.problems import add_problem, change_status
from rcompass.services.progress import CriterionState
from rcompass.services.runs import exclude_run, get_run, parse_results_table, record_results
from rcompass.web.app import create_app


@pytest.fixture
def lab(session: Session, project: Project):
    add_problem(session, project, title="장문 지문 오답", why="18개 중 7개가 장문")
    version = add_dataset_version(session, project, dataset_name="eval-100", version_label="v1.0",
                                  external_uri="D:/data/eval100_v1.jsonl").version
    session.commit()
    return version


def _trial(session: Session, project: Project, version, title: str, table: str):
    experiment = create_experiment(session, project, problem_short_id="P1", title=title, hypothesis="h",
                                   decision_rule="r")
    update_inputs(session, experiment, dataset_version_ids=[version.id], code_ref="a1b2c3d", config_text="c.yaml")
    record_results(session, project, experiment, parse_results_table(table), dataset_version_id=version.id)
    return experiment


def test_fresh_project_dashboard(session: Session, project: Project) -> None:
    board = build_dashboard(session, project)
    assert board.met == 0 and board.goal[CriterionState.NOT_MET] == 1 and board.goal[CriterionState.UNMEASURED] == 1
    assert [row.attempt for row in board.rows] == [None, None]
    assert (board.work.experiments_total, board.work.experiments_decided, board.work.problems_total) == (0, 0, 0)
    assert board.last_decision is None and board.on_baseline_zero
    assert [row.label for row in board.history] == ["시작"] and board.history[0].to.short_id == "B0"


def test_rejection_moves_work_but_not_goal(session: Session, project: Project, lab) -> None:
    """시나리오 3: 기각 뒤 작업 진행은 '실험 1/1 완료'인데 목표 달성은 0/2, 최근 시도는 'E1 19.0 기각'."""
    e1 = _trial(session, project, lab, "few-shot", "label\twrong_count\ns1\t18\ns2\t19\ns3\t20")
    decide(session, project, e1, DecisionType.REJECT, reason="평균 오답 +1")
    board = build_dashboard(session, project)
    c1 = board.rows[0]
    assert c1.evaluation.state is CriterionState.NOT_MET and c1.evaluation.display_value == "18.0 ± 1.0 (n=3)"
    assert c1.attempt.experiment.short_id == "E1" and c1.attempt_display == "19.0 ± 1.0 (n=3)"
    assert (board.work.experiments_decided, board.work.experiments_total) == (1, 1)
    assert board.met == 0
    assert board.last_decision.label == "기각" and board.last_decision.moved_to is None


def test_latest_attempt_is_most_recently_recorded(session: Session, project: Project, lab) -> None:
    e1 = _trial(session, project, lab, "a", "label\twrong_count\ns1\t18")
    e2 = _trial(session, project, lab, "b", "label\twrong_count\ns1\t11")
    criterion = build_dashboard(session, project).rows[0].criterion
    assert latest_attempt(session, project, criterion).experiment.id == e2.id
    record_results(session, project, e1, parse_results_table("label\twrong_count\ns2\t17"), dataset_version_id=lab.id)
    attempt = latest_attempt(session, project, criterion)
    assert attempt.experiment.id == e1.id and attempt.aggregated.values == (18.0, 17.0)


def test_latest_attempt_ignores_excluded_runs_and_e0(session: Session, project: Project, lab) -> None:
    e1 = _trial(session, project, lab, "a", "label\twrong_count\ns1\t18\ns2\t30")
    exclude_run(session, get_run(session, e1, "R2"), "로그 손상")
    criterion = build_dashboard(session, project).rows[0].criterion
    assert latest_attempt(session, project, criterion).aggregated.values == (18.0,)
    exclude_run(session, get_run(session, e1, "R1"), "다시 잼")
    assert latest_attempt(session, project, criterion) is None  # E0(기준선 측정)은 시도가 아닙니다


def test_work_progress_counts(session: Session, project: Project, lab) -> None:
    add_problem(session, project, title="모델 교체", why="한계 도달 시", status=ProblemStatus.PARKED,
                revisit_condition="나중")
    resolved = add_problem(session, project, title="해결된 문제", why="w")
    dropped = add_problem(session, project, title="버린 문제", why="w")
    change_status(session, project, resolved, ProblemStatus.RESOLVED)
    change_status(session, project, dropped, ProblemStatus.DROPPED)
    held = _trial(session, project, lab, "보류할 것", "label\twrong_count\ns1\t16")
    decide(session, project, held, DecisionType.HOLD, reason="재측정")
    waiting = _trial(session, project, lab, "판정 대기", "label\twrong_count\ns1\t15")
    create_experiment(session, project, problem_short_id="P1", title="계획만", hypothesis="h", decision_rule="r")
    work = work_progress(session, project)
    assert (work.experiments_total, work.experiments_decided, work.experiments_held) == (3, 0, 1)
    assert [e.id for e in work.awaiting] == [waiting.id]
    assert (work.problems_total, work.problems_resolved, work.problems_dropped) == (3, 1, 1)


def test_adopt_and_revoke_show_in_history_and_last_decision(session: Session, project: Project, lab) -> None:
    e2 = _trial(session, project, lab, "요약", "label\twrong_count\ns1\t11\ns2\t12\ns3\t10")
    decide(session, project, e2, DecisionType.ADOPT, reason="목표 달성", baseline_label="문단 요약 v1")
    board = build_dashboard(session, project)
    assert board.met == 1 and not board.on_baseline_zero
    assert (board.last_decision.moved_from.short_id, board.last_decision.moved_to.short_id) == ("B0", "B1")
    revoke_adoption(session, project, e2, reason="누수")
    board = build_dashboard(session, project)
    assert [row.label for row in board.history] == ["철회로 복귀", "채택", "시작"]
    assert board.last_decision.label == "채택 철회" and board.last_decision.moved_to.short_id == "B0"
    assert (board.work.experiments_decided, board.met) == (1, 0)


def test_home_warns_about_excluded_snapshot_run(session: Session, project: Project, lab) -> None:
    e2 = _trial(session, project, lab, "요약", "label\twrong_count\ns1\t11\ns2\t12\ns3\t10")
    decide(session, project, e2, DecisionType.ADOPT, reason="ok")
    exclude_run(session, get_run(session, e2, "R2"), "로그 손상")
    warnings = build_dashboard(session, project).warnings
    assert len(warnings) == 1 and "E1/R2" in warnings[0]


@pytest.mark.parametrize(("text", "stored"), [("  E2 응답 시간\n측정  ", "E2 응답 시간 측정"), ("   ", None)])
def test_next_action_is_one_line(session: Session, project: Project, text: str, stored: str | None) -> None:
    update_next_action(session, project, text)
    assert project.next_action == stored


def test_next_action_length_limit(session: Session, project: Project) -> None:
    with pytest.raises(ValidationError):
        update_next_action(session, project, "가" * 301)


# ---------------------------------------------------------------- 화면


BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(data_dir=tmp_path / "data")), base_url=BASE_URL) as client:
        client.post("/projects", data={
            "title": "QA", "question": "오답을 12개 이하로?", "baseline_label": "현행 프롬프트 v1",
            "criteria-0-name": "오답 수", "criteria-0-metric_key": "wrong_count", "criteria-0-direction": "lower",
            "criteria-0-target": "12", "criteria-0-min_runs": "3", "criteria-0-baseline_values": "17, 18, 19",
            "criteria-1-name": "평균 응답 시간", "criteria-1-metric_key": "avg_latency_s",
            "criteria-1-direction": "lower", "criteria-1-target": "2.0",
        })
        client.post("/projects/1/problems", data={"title": "장문 지문 오답", "why": "18개 중 7개가 장문"})
        client.post("/projects/1/datasets", data={"dataset_name": "eval-100", "version_label": "v1.0",
                                                  "external_uri": "D:/data/eval100_v1.jsonl"})
        yield client


def _page_trial(client: TestClient, title: str, values: list[int]) -> str:
    location = client.post("/projects/1/experiments", data={
        "problem": "P1", "title": title, "hypothesis": "h", "decision_rule": "r"}, follow_redirects=False
    ).headers["location"]
    client.post(f"{location}/inputs", data={"dataset_version": "1", "code_ref": "a1b2c3d", "config_text": "c"})
    rows = "\n".join(f"s{i}\t{v}" for i, v in enumerate(values, 1))
    client.post(f"{location}/results", data={"results": f"label\twrong_count\n{rows}", "dataset_version": "1"})
    return location


def test_home_shows_two_separate_progress_measures(client: TestClient) -> None:
    location = _page_trial(client, "few-shot", [18, 19, 20])
    client.post(f"{location}/decisions", data={"type": "reject", "reason": "평균 오답 +1"})
    page = client.get("/projects/1").text
    goal = page.split('id="goal-title"')[1].split("</section>")[0]
    work = page.split('id="work-title"')[1].split("</section>")[0]
    assert "성공 기준 2개 중 달성 0" in goal and "미측정 1" in goal
    assert "실험 완료 1/1" in work and "작업 진행은 목표 달성을 뜻하지 않습니다" in work
    table = page.split("성공 기준별 최신 근거")[1].split("</table>")[0]
    assert '<a href="/projects/1/experiments/E1">E1</a> 19.0 ± 1.0 (n=3)' in table and "✘ 기각" in table
    last = page.split("마지막 결정")[1].split("</section>")[0]
    assert "<strong>기각</strong>" in last and "평균 오답 +1" in last


def test_unmeasured_criterion_offers_measurement(client: TestClient) -> None:
    page = client.get("/projects/1").text
    assert 'href="/projects/1/experiments/E0#results">측정값 기록</a>' in page
    location = _page_trial(client, "요약", [11, 12, 10])
    client.post(f"{location}/decisions", data={"type": "adopt", "reason": "ok"})
    page = client.get("/projects/1").text
    assert "E0#results" not in page and ">측정 실험 만들기</a>" in page
    client.post("/projects/1/focus", data={"problem": "P1"})
    page = client.get("/projects/1").text
    assert 'href="/projects/1/measurements/new?problem=P1">측정 실험 만들기</a>' in page
    assert 'href="/projects/1/problems/new?parent=P1">새 하위 문제</a>' in page


def test_insufficient_and_awaiting(client: TestClient) -> None:
    _page_trial(client, "아직 판정 전", [11])
    page = client.get("/projects/1").text
    assert '판정 대기:' in page and 'href="/projects/1/experiments/E1#decision"' in page
    client.post("/projects", data={
        "title": "근거 부족", "question": "q", "baseline_label": "b",
        "criteria-0-name": "오답 수", "criteria-0-metric_key": "wrong_count", "criteria-0-direction": "lower",
        "criteria-0-target": "12", "criteria-0-min_runs": "3", "criteria-0-baseline_values": "17, 18",
    })
    page = client.get("/projects/2").text
    assert "근거 부족</span>" in page and "실행 2/3" in page


def test_next_action_inline_edit(client: TestClient) -> None:
    response = client.post("/projects/1/next-action", data={"next_action": "E2 설정으로 응답 시간 측정"},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("#next-action")
    page = client.get("/projects/1?saved=next_action").text
    assert 'value="E2 설정으로 응답 시간 측정"' in page and "다음 행동을 저장했습니다" in page
    too_long = client.post("/projects/1/next-action", data={"next_action": "가" * 301})
    assert too_long.status_code == 422 and "300자 이내" in too_long.text
    assert client.post("/projects/9/next-action", data={"next_action": "x"}).status_code == 404


def test_home_baseline_history(client: TestClient) -> None:
    location = _page_trial(client, "요약", [11, 12, 10])
    client.post(f"{location}/decisions", data={"type": "adopt", "reason": "ok", "baseline_label": "문단 요약 v1"})
    page = client.get("/projects/1").text
    history = page.split("기준선 이력")[1].split("</section>")[0]
    assert history.index("채택 → <span class=\"id\">B1</span> 문단 요약 v1") < history.index("시작 → <span class=\"id\">B0</span>")


def test_correcting_values_makes_that_experiment_the_latest(session: Session, project: Project, lab) -> None:
    """같은 실행의 값을 고쳐 다시 기록해도 '최근 시도'는 마지막으로 기록한 실험을 가리킵니다."""
    e1 = _trial(session, project, lab, "a", "label\twrong_count\ns1\t18")
    _trial(session, project, lab, "b", "label\twrong_count\ns1\t11")
    record_results(session, project, e1, parse_results_table("label\twrong_count\ns1\t17"), dataset_version_id=lab.id)
    attempt = latest_attempt(session, project, build_dashboard(session, project).rows[0].criterion)
    assert attempt.experiment.id == e1.id and attempt.aggregated.values == (17.0,)


def test_project_creation_applies_next_action_rules(client: TestClient) -> None:
    base = {"title": "t", "question": "q", "baseline_label": "b", "criteria-0-name": "오답 수",
            "criteria-0-metric_key": "wrong_count", "criteria-0-direction": "lower", "criteria-0-target": "12"}
    too_long = client.post("/projects", data={**base, "next_action": "가" * 301})
    assert too_long.status_code == 422 and "300자 이내" in too_long.text
    client.post("/projects", data={**base, "next_action": "첫 줄\n둘째 줄"})
    assert 'value="첫 줄 둘째 줄"' in client.get("/projects/2").text
