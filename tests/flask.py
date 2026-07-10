from mock.mocks import MockCls


class Response:
    def __init__(self, response=None, status=200, content_type=None, mimetype=None):
        self.response = response
        self.status_code = status
        self.content_type = content_type or mimetype


request = MockCls()
request.host_url = "http://localhost/"
