"""The one exception of a render that cannot go on."""


class ReportError(Exception):
    """Why a report cannot be rendered, for the person who asked, and the tag the export
    routes turn into an HTTP status (app/api/routes/tasks.py): `unprocessable` (422, the
    definition, the data or the parameters), `not_found` (404, a source that is gone),
    `unavailable` (503, the chart service), `timeout` (504), `bad_request` (400)."""

    def __init__(self, message: str, tag: str = "unprocessable"):
        super().__init__(message)
        self.message = message
        self.tag = tag
