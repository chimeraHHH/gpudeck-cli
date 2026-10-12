class DeckError(Exception):
    def __init__(self, code, message, exit_code=1, *, status=None, details=None):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.status = status
        self.details = details

    def as_dict(self):
        result = {"code": self.code, "message": str(self)}
        if self.status is not None:
            result["status"] = self.status
        if self.details is not None:
            result["details"] = self.details
        return result
