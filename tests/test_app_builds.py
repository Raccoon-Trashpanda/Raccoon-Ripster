"""The FastAPI app object must build (all routers import + register) without
running the server. app.py guards uvicorn behind `if __name__ == '__main__'`,
so importing it is side-effect-free."""


def test_app_object_builds():
    import app
    assert hasattr(app, "app"), "app.py must expose an `app` object"


def test_app_has_routes():
    import app
    routes = getattr(app.app, "routes", [])
    assert len(routes) > 0, "FastAPI app registered no routes"
