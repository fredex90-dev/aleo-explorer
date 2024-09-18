import asyncio
import logging
import multiprocessing
import os
from typing import Any

import aiohttp
import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.routing import Route

from db import Database
from middleware.asgi_logger import AccessLoggerMiddleware
from middleware.server_timing import ServerTimingMiddleware
from util.set_proc_title import set_proc_title
from .address_routes import address_route
from .chain_routes import blocks_route, get_summary, recent_blocks_route, index_update_route, block_route, \
    transaction_route, \
    validators_route
from .error_routes import bad_request, not_found, internal_error
from .utils import public_cache_seconds, out_of_sync_check, CJSONResponse

load_dotenv()

class UvicornServer(multiprocessing.Process):

    def __init__(self, config: uvicorn.Config):
        super().__init__()
        self.server = uvicorn.Server(config=config)
        self.config = config

    def stop(self):
        self.terminate()

    def run(self, *args: Any, **kwargs: Any):
        self.server.run()

async def index_route(request: Request):
    return CJSONResponse({"hello": "world"})

@public_cache_seconds(10)
async def sync_info_route(request: Request):
    db: Database = request.app.state.db
    sync_info = await out_of_sync_check(request.app.state.session, db)
    return CJSONResponse(sync_info)

@public_cache_seconds(5)
async def summary_route(request: Request):
    db: Database = request.app.state.db
    return CJSONResponse(await get_summary(db))

routes = [
    Route("/", index_route),
    Route("/sync", sync_info_route),
    Route("/summary", summary_route),

    Route("/block/recent", recent_blocks_route),
    Route("/block/index_update", index_update_route),

    Route("/blocks", blocks_route),
    Route("/block/{height}", block_route),
    Route("/validators", validators_route),
    Route("/transaction/{id}", transaction_route),

    Route("/address/{address}", address_route),
]

exc_handlers = {
    400: bad_request,
    404: not_found,
    550: internal_error,
}

async def startup():
    async def noop(_: Any): pass

    # different thread so need to get a new database instance
    db = Database(server=os.environ["DB_HOST"], user=os.environ["DB_USER"], password=os.environ["DB_PASS"],
                  database=os.environ["DB_DATABASE"], schema=os.environ["DB_SCHEMA"],
                  redis_server=os.environ["REDIS_HOST"], redis_port=int(os.environ["REDIS_PORT"]),
                  redis_db=int(os.environ["REDIS_DB"]), redis_user=os.environ.get("REDIS_USER"),
                  redis_password=os.environ.get("REDIS_PASS"),
                  message_callback=noop)
    await db.connect()
    # noinspection PyUnresolvedReferences
    app.state.db = db
    # noinspection PyUnresolvedReferences
    # app.state.lns.connect(os.environ.get("P2P_NODE_HOST", "127.0.0.1"), int(os.environ.get("P2P_NODE_PORT", "4130")), None)
    app.state.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1))
    set_proc_title("aleo-explorer: webapi")

log_format = '\033[92mWEB\033[0m: \033[94m%(client_addr)s\033[0m - - %(t)s \033[96m"%(request_line)s"\033[0m \033[93m%(s)s\033[0m %(B)s "%(f)s" "%(a)s" %(L)s'
# noinspection PyTypeChecker
app = Starlette(
    debug=True if os.environ.get("DEBUG") else False,
    routes=routes,
    on_startup=[startup],
    exception_handlers=exc_handlers,
    middleware=[
        Middleware(AccessLoggerMiddleware, format=log_format),
        Middleware(ServerTimingMiddleware),
        Middleware(AuthMiddleware, token=os.environ.get("WEBAPI_TOKEN", "")),
    ]
)

async def run():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("WEBAPI_PORT", 8002))
    config = uvicorn.Config(
        "webapi:app", reload=True, log_level="info", host=host, port=port,
        forwarded_allow_ips=["127.0.0.1", "::1", "10.0.4.1", "10.0.5.1"]
    )
    logging.getLogger("uvicorn.access").handlers = []
    server = UvicornServer(config=config)
    # noinspection PyUnresolvedReferences
    # app.state.lns = LightNodeState()

    server.start()
    while True:
        await asyncio.sleep(3600)

async def run_profile():
    config = uvicorn.Config("webapi:app", reload=True, log_level="info", port=8888)
    await uvicorn.Server(config).serve()
