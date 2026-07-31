"""A raw TCP forwarder, one per live Tide Pool (docs/TODO.md) — `python -m ccc_head_api.relay
<listen_port> <target_host> <target_port>`. Started and killed by ccc_head_api.tidepool, never run
standalone.

This is the whole answer to "Users are not Mesh members and cannot reach an Arm's mesh address": it
listens on the Head (which every client already reaches, for the Dashboard/API) and forwards bytes,
unmodified, to one Arm:port for the life of one Spawn. Nothing here understands SSH — that is the
point. The real ssh client and the Pod's real sshd negotiate everything (host keys, ciphers, auth)
straight through this pipe; it would work identically forwarding any other TCP protocol.

Its own process, its own asyncio loop — not a task inside the Head API (an ASGI app whose event loop is
serving HTTP) or the karma tick (a 30s poll loop with no event loop at all). A relay carrying live
interactive bytes must never share a loop with either.
"""

import asyncio
import sys

CHUNK = 65536


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


async def _handle(local_r, local_w, target_host: str, target_port: int) -> None:
    try:
        remote_r, remote_w = await asyncio.open_connection(target_host, target_port)
    except OSError:
        local_w.close()
        return
    # Both directions run concurrently and independently — an ssh session is full-duplex, and either
    # side closing (a clean `exit`, or Slurm killing the job the instant --time is up) must tear down
    # the other leg rather than leave a half-open socket the OS only reclaims on a timeout.
    await asyncio.gather(_pump(local_r, remote_w), _pump(remote_r, local_w))


async def _serve(listen_port: int, target_host: str, target_port: int) -> None:
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, target_host, target_port), "0.0.0.0", listen_port)
    async with server:
        await server.serve_forever()


def main() -> None:
    listen_port, target_host, target_port = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    asyncio.run(_serve(listen_port, target_host, target_port))


if __name__ == "__main__":
    main()
