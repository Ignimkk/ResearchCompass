"""도메인 서비스. 웹 라우트와 CLI가 같은 함수를 씁니다."""


class ValidationError(Exception):
    """입력 검증 실패. ``errors``는 필드 이름 → 한국어 메시지."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{field}: {message}" for field, message in errors.items()))
        self.errors = errors
