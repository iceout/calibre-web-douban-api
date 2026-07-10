class MockCls:
    def __init__(self):
        self.args = {}

    @staticmethod
    def route(*args, **kwargs):
        def inner(function):
            return function
        return inner
