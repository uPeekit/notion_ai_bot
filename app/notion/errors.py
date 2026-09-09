class NotionError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class NotionUnavailable(NotionError):
    def __init__(self, message: str = "Notion unreachable") -> None:
        super().__init__(0, "unavailable", message)
