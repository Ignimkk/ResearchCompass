"""실험 안에서 실행 이름을 고유하게

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-20

결과 표는 실행 이름으로 기존 실행을 찾아 값을 고치므로, 같은 실험에 같은 이름의 실행이 둘이면
엉뚱한 실행을 고치게 됩니다. 표를 다시 만들지 않도록(외래 키와 트리거 보존) 고유 인덱스로 추가합니다.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_runs_experiment_id_label", "runs", ["experiment_id", "label"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_runs_experiment_id_label", table_name="runs")
