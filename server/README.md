# Union server

The control plane and relay for Union. People sign in here (passkey or
password) to create unions, hand out join keys, watch nodes check in, and
evict them. Nodes talk to `/api/v1` with signed requests and end-to-end
encrypted messages the server cannot read.

* Web UI: `/unions`
* API docs: `/api/docs` (Swagger), `/api/redoc`, `/api/openapi.json`
* Client guide for building a node in any language: `/api/guide` (also `/api/guide.md`)

## Setup

From the repository root:

```bash
python -m venv .venv
.venv\Scripts\activate                # Windows
pip install -e ./protocol -e ./server
```

Create `server/union.toml` from `server/union.example.toml`. The two settings
that matter:

```toml
[server]
public_url    = "https://union.webroo.xyz"   # passkeys are bound to this exact host
reverse_proxy = true                         # TLS is terminated by the proxy in front

[session]
secret_key = "<python -c \"import secrets; print(secrets.token_hex(32))\">"
```

## Run

```bash
cd server
python union_web.py                     # http://127.0.0.1:8484
python union_web.py --host 0.0.0.0      # reachable from other machines
```

Or `start.bat` on Windows. Run exactly one process: the relay keeps live
event streams in memory.

Then open the URL, **Register**, add a passkey on the account page, and
create a union.

Registration is gated two ways, both under `[security]`:

* `registration_code`: when set, the register form requires it. Same idea
  as NaturalRoo. Wrong codes are limited per IP by `bad_code_rate_limit`.
* `registration_enabled = false` closes registration entirely. The first
  account can always register so a fresh install is never locked out.

Password login attempts are limited per IP and username together by
`login_rate_limit`; passkey sign-in has a separate, looser limit so a
passkey always works. Behind a reverse proxy the client IP comes from
`X-Forwarded-For`, trusted only when `reverse_proxy = true`.

Note for Nginx Proxy Manager in Docker Desktop on Windows: Docker NATs
inbound connections, so the proxy sees every visitor as the bridge gateway
(172.18.0.1 or similar) and so does Union. The per-IP limits then apply to
everyone together. The registration-code and per-username limits still
hold; only the per-IP ceilings lose their meaning.

## Behind Nginx Proxy Manager or Caddy

Same as DittoRoo. Point a proxy host at `http://<this-machine>:8484` with
TLS, keep `reverse_proxy = true`, and make sure `public_url` matches the
public hostname exactly or passkeys and redirects break. Websockets are not
needed, but the proxy must not buffer responses: Union pushes events over
long-lived `text/event-stream` responses. Union sends `X-Accel-Buffering: no`;
for Nginx also set `proxy_buffering off;` and a long `proxy_read_timeout`
(the stream sends a ping every 20 s, so anything above 60 s is fine).

## Data

Everything lives in `server/data/` (configurable with `[data] dir`):

* `union.db`: users, passkeys, unions, join keys, nodes, presence, activity.
  No message bodies, ever.
* `hub.key`: the server's Ed25519 signing key. Rosters are signed with it.
  Back it up with the database; nodes pin its public half.
* `blobs/`: encrypted attachments in transit, deleted on delivery. Cleared
  on start.

Join keys are stored as-is so the page can show them; they are the one
secret in the database. Cycle a key from the union page if it leaks.

## Tests

```bash
pytest protocol/tests server/tests
```

The server tests start a real uvicorn on a free port and drive it with a
reference node (`server/tests/refclient.py`) built from `union_protocol`
and `httpx`. That file is a working client in about 150 lines.
