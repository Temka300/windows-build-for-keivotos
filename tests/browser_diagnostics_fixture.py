"""Disposable diagnostic failures; never installed by production composition."""
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute


def install():
    from server import app
    from routers.backups import get_backup_configuration
    state={"fail":False}
    async def mode(request:Request):
        state["fail"]=(await request.json()).get("enabled") is True
        return {"enabled":state["fail"]}
    def expire():
        import time
        from services import diagnostic_policy
        diagnostic_policy._debug_deadline=time.time()+2
        diagnostic_policy._debug_started=time.monotonic()+2
        return {"status":"shortened disposable test deadline"}
    app.router.routes.insert(0,APIRoute("/api/fixture/diagnostic-debug-expire",expire,methods=["POST"]))
    def backups():
        if state["fail"]:
            raise HTTPException(503,"private diagnostic fixture body")
        return get_backup_configuration()
    app.router.routes.insert(0,APIRoute("/api/fixture/diagnostic-mode",mode,methods=["POST"]))
    app.router.routes.insert(0,APIRoute("/api/backups",backups,methods=["GET"]))
