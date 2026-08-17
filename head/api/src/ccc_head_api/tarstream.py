"""One way to hand a directory to a client: a gzip tar, yielded in chunks, never held whole.

Every tar the Head serves comes from here — a **Spawn**'s declared **Output**s, its **Recovery
Window** copy, a **Workspace**'s tree. They started as three separate answers to the same question
and two of them were wrong: built with `io.BytesIO` and returned as one `bytes`, which on a 3GB
result meant the Head spent ~4 minutes compressing before the client saw byte one, and the client's
read timeout fired long before that. A timeout on a transfer that was working perfectly reads to a
User as "cannot reach the Head", which is the wrong thing to go looking for.

So the shape is fixed here rather than per-route, and two properties come with it.

**Nothing is sized by the tree.** `tarfile` writes through `Sink.write` as it copies each file, so
the bounded queue — not the 3GB — is what this costs the Head, the one box that must stay up.

**An abandoned download stops the work.** A client that disconnects closes the generator, which sets
`stop` and lets the writer thread unwind instead of blocking on a queue nobody drains. The buffered
version had no such point: a client that gave up left the Head compressing to completion, so each
retry stacked another full-tree job on it.

`LEVEL` is 1, not gzip's default 9. Measured on a real 3GB sweep result: level 9 compressed at
14MB/s while the mesh moved 94MB/s, so compression, not the network, was 93% of the wall clock.
Level 1 gives that back and costs little — these trees are checkpoints, logs and yaml, which still
come out around 2:1. The point of the ratio was never the ratio; it was not sending 3GB.
"""

import queue
import tarfile
import threading
from pathlib import Path

LEVEL = 1


def chunks(target: Path, arcbase: str):
    """A gzip-tar of `target`, yielded chunk by chunk, with `arcbase` as the top-level name inside."""
    out: queue.Queue = queue.Queue(maxsize=8)
    done = object()
    stop = threading.Event()

    class Sink:
        def write(self, data):
            while not stop.is_set():
                try:
                    out.put(bytes(data), timeout=0.5)
                    return len(data)
                except queue.Full:
                    continue
            raise BrokenPipeError("download aborted")

    def build():
        try:
            with tarfile.open(fileobj=Sink(), mode="w|gz", compresslevel=LEVEL) as tf:
                tf.add(target, arcname=arcbase)
        except BaseException as e:      # noqa: BLE001 — re-raised on the consuming side
            out.put(e)
        finally:
            out.put(done)

    threading.Thread(target=build, daemon=True).start()
    try:
        while True:
            item = out.get()
            if item is done:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()


def has_files(target: Path) -> bool:
    """Whether there is anything in there worth a tar at all.

    Emptiness, not existence, is the test everywhere this is used: `submit` pre-creates a Spawn's
    `outputs/` before the job runs, so the directory being there says nothing. And it has to be asked
    BEFORE the stream opens — once the first chunk is out the 200 is committed, and the careful 404
    telling "wrote nothing" from "held it, now evicted" can no longer be sent.
    """
    return target.is_dir() and any(p.is_file() for p in target.rglob("*"))
