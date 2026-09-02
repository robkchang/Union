"""Web process entry point.

    python union_web.py                    # union.toml next to this file, or defaults
    python union_web.py --config path.toml
    python union_web.py --host 0.0.0.0 --port 8484

Run one worker: the relay keeps live streams in memory.
"""
import argparse
import logging

import uvicorn

from union_server import config, create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("union")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Union server")
    parser.add_argument("--config", default=None, help="Path to union.toml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    settings = config.load(args.config)
    if settings.secret_generated:
        log.warning("No [session] secret_key in union.toml; logins will not survive a restart. "
                    "Set it to: python -c \"import secrets; print(secrets.token_hex(32))\"")
    app = create_app(settings)
    host = args.host or settings.server.host
    port = args.port or settings.server.port
    log.info("Union listening on http://%s:%s  public_url=%s", host, port, settings.server.public_url)
    uvicorn.run(
        app, host=host, port=port,
        proxy_headers=settings.server.reverse_proxy,
        forwarded_allow_ips="*" if settings.server.reverse_proxy else "127.0.0.1",
        log_level="info",
    )
