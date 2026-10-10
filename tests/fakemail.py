"""In-process fake SMTP and IMAP servers, plus the mailbox they share.

Exists to enforce one rule: a test never touches a real network or a real inbox. The
approval agent drives `smtplib.SMTP` and `imaplib.IMAP4` for real against these loopback
doubles, so the client code under test is exercised byte-for-byte on the wire while no
credential, relay or mailbox outside this process is ever involved.

Stdlib only: `smtpd` was removed in 3.12 and `aiosmtpd` is not a dependency, so both
protocols are spoken directly over `socketserver` sockets -- just the subset of ESMTP and
IMAP4rev1 that the stdlib clients actually emit.

Deliberate fidelities and simplifications, because a double that lies costs more than it
saves:
  * A non-PEEK body fetch (`RFC822`, `BODY[]`) sets `\\Seen`, as a real server does;
    `BODY.PEEK[]` does not. Agent code that polls with `RFC822` will see its own reads.
  * SMTP accepts into `FakeMailbox.sent`; it does not loop mail back into the IMAP view.
    Arrivals are staged explicitly with `append()` / `reply_to()`.
  * UIDVALIDITY is always 1 and UIDs never restart: one mailbox lives one test.
  * SEARCH understands ALL / UNSEEN / SEEN / HEADER / SUBJECT / BODY / TEXT only; anything
    else is answered BAD rather than silently matching everything.
"""
from __future__ import annotations

import base64
import email
import re
import socketserver
import threading
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formatdate, make_msgid
from typing import Any, Callable

HOST = "127.0.0.1"
_EOL = re.compile(rb"\r\n|\r|\n")
_LITERAL = re.compile(rb"\{(\d+)\+?\}$")
_SEEN = r"\Seen"
_DELETED = r"\Deleted"


def _wire(raw: bytes) -> bytes:
    """Normalise to CRLF and guarantee a terminator: IMAP literal sizes must be exact."""
    out = _EOL.sub(b"\r\n", raw)
    return out if out.endswith(b"\r\n") else out + b"\r\n"


def _match_set(spec: str, value: int, highest: int) -> bool:
    """Evaluate an IMAP sequence/uid set (`2`, `1,4`, `2:7`, `3:*`, `*`) against one value."""
    for part in spec.split(","):
        lo, _, hi = part.partition(":")
        hi = hi or lo
        try:
            a = highest if lo == "*" else int(lo)
            b = highest if hi == "*" else int(hi)
        except ValueError:
            return False
        if min(a, b) <= value <= max(a, b):
            return True
    return False


def _split_imap(line: bytes) -> tuple[list[bytes], int | None]:
    """Tokenise one IMAP command line into (tokens, pending literal size).

    Parens are emitted as standalone tokens so `(RFC822)` and `RFC822` parse identically,
    while a bracketed section stays glued to its atom so `BODY[HEADER.FIELDS (DATE)]`
    survives as one token. A trailing `{n}` means the caller must prompt for a literal.
    """
    out: list[bytes] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i : i + 1]
        if c in (b" ", b"\t"):
            i += 1
        elif c in (b"(", b")"):
            out.append(c)
            i += 1
        elif c == b'"':
            buf, i = bytearray(), i + 1
            while i < n and line[i : i + 1] != b'"':
                if line[i : i + 1] == b"\\":
                    i += 1
                buf += line[i : i + 1]
                i += 1
            out.append(bytes(buf))
            i += 1
        elif c == b"{" and (m := _LITERAL.match(line, i)):
            return out, int(m.group(1))
        else:
            buf, depth = bytearray(), 0
            while i < n:
                ch = line[i : i + 1]
                if ch == b"[":
                    depth += 1
                elif ch == b"]":
                    depth -= 1
                elif depth == 0 and ch in (b" ", b"\t", b"(", b")"):
                    break
                buf += ch
                i += 1
            out.append(bytes(buf))
    return out, None


class FakeMailbox:
    """Shared store between the two servers. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.messages: list[dict] = []
        self.sent: list[dict] = []
        self._next_uid = 1

    # ------------------------------------------------------------------ arrivals
    def append(self, raw: bytes) -> int:
        with self._lock:
            uid = self._next_uid
            self._next_uid += 1
            self.messages.append({"uid": uid, "raw": _wire(raw), "flags": set()})
            return uid

    def reply_to(self, sent_index: int, body: str, *, subject: str | None = None,
                 from_addr: str = "admin@example.invalid") -> int:
        """Stage a realistic human reply to `self.sent[sent_index]` and return its uid."""
        with self._lock:
            original = self.sent[sent_index]
            parent: email.message.Message = original["msg"]
        parent_id = str(parent.get("Message-ID", "") or "")
        reply = EmailMessage(policy=SMTP_POLICY)
        reply["Message-ID"] = make_msgid(domain="example.invalid")
        reply["Date"] = formatdate(localtime=False)
        reply["From"] = from_addr
        reply["To"] = str(parent.get("From", "") or original["mailfrom"])
        reply["Subject"] = subject if subject is not None else "Re: " + str(parent.get("Subject", "") or "")
        if parent_id:
            # Threading headers are the only link the agent has back to its own request.
            reply["In-Reply-To"] = parent_id
            reply["References"] = parent_id
        reply.set_content(body)
        return self.append(reply.as_bytes())

    # ------------------------------------------------------------------ outgoing
    def record_sent(self, mailfrom: str, rcpttos: list[str], raw: bytes) -> int:
        """Called by FakeSMTPServer once DATA completes; index is the agent's handle on it."""
        with self._lock:
            self.sent.append({"mailfrom": mailfrom, "rcpttos": list(rcpttos), "raw": raw,
                              "msg": email.message_from_bytes(raw)})
            return len(self.sent) - 1

    # ------------------------------------------------------------------ views
    def snapshot(self) -> list[dict]:
        """Positional copy; sequence numbers are 1-based indexes into this list."""
        with self._lock:
            return list(self.messages)

    def set_flags(self, uid: int, flags: set[str]) -> None:
        with self._lock:
            for m in self.messages:
                if m["uid"] == uid:
                    m["flags"] = set(flags)
                    return

    def expunge(self) -> list[int]:
        """Drop \\Deleted messages, returning the sequence numbers as the client sees them."""
        with self._lock:
            gone, seq = [], 0
            for m in list(self.messages):
                seq += 1
                if _DELETED in m["flags"]:
                    self.messages.remove(m)
                    gone.append(seq)
                    seq -= 1  # every removal shifts the numbering of what follows
            return gone


# ---------------------------------------------------------------------------- plumbing
class _Threaded(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True  # stop() must not block on a client that never says goodbye
    block_on_close = False

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that hangs up mid-command is normal here, not a test failure."""


class _Base:
    """start/stop/context-manager scaffolding shared by both fakes."""

    _handler: type[socketserver.BaseRequestHandler]
    _modes: frozenset[str]

    def __init__(self, mailbox: FakeMailbox, *, user: str, password: str, fail_with: str | None) -> None:
        if fail_with is not None and fail_with not in self._modes:
            raise ValueError(f"unknown fail_with {fail_with!r}; expected one of {sorted(self._modes)}")
        self.mailbox = mailbox
        self.user = user
        self.password = password
        self.fail_with = fail_with
        self.host = HOST
        self.port = 0
        self._srv: _Threaded | None = None
        self._thread: threading.Thread | None = None

    def start(self):  # noqa: ANN201 - concrete subclasses narrow the return type
        if self._srv is not None:
            return self
        self._srv = _Threaded((HOST, 0), self._handler)
        self._srv.cfg = self  # type: ignore[attr-defined]
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.02},
                                        name=f"{type(self).__name__}:{self.port}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        srv, thread = self._srv, self._thread
        self._srv = self._thread = None
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self):  # noqa: ANN204
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


class _Conn(socketserver.StreamRequestHandler):
    """Line-oriented connection handler; `timeout` keeps a wedged test from leaking a thread."""

    timeout = 30

    @property
    def cfg(self) -> Any:
        return self.server.cfg  # type: ignore[attr-defined]

    def _send(self, data: bytes) -> None:
        self.wfile.write(data)

    def handle(self) -> None:
        if self.cfg.fail_with == "refuse":
            return  # accept the TCP connection, then vanish: an unreachable relay
        try:
            self.converse()
        except (OSError, TimeoutError, ValueError):
            return  # client hung up or spoke nonsense; the test, not the double, reports it

    def converse(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------- SMTP
class _SMTPConn(_Conn):
    def converse(self) -> None:
        cfg = self.cfg
        self._send(b"220 %s Faultline fake ESMTP ready\r\n" % HOST.encode())
        authed = not cfg.require_auth
        mailfrom, rcpttos = "", []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.rstrip(b"\r\n").decode("utf-8", "replace")
            verb, _, rest = text.partition(" ")
            verb, rest = verb.upper(), rest.strip()
            if verb in ("EHLO", "HELO"):
                if verb == "HELO":
                    self._send(b"250 %s Hello\r\n" % HOST.encode())
                else:
                    # No STARTTLS line: the double is plaintext by design, and advertising
                    # it would make smtplib try to wrap a socket that cannot do TLS.
                    self._send(b"250-%s Hello\r\n250-PIPELINING\r\n250-8BITMIME\r\n"
                               b"250-SIZE 10485760\r\n250-AUTH PLAIN LOGIN\r\n250 HELP\r\n"
                               % HOST.encode())
            elif verb == "AUTH":
                if authed and cfg.require_auth:
                    self._send(b"503 5.5.1 Already authenticated\r\n")
                    continue
                authed = self._auth(rest) or (authed and not cfg.require_auth)
            elif verb == "MAIL":
                if not authed:
                    self._send(b"530 5.7.0 Authentication required\r\n")
                    continue
                mailfrom, rcpttos = _addr(rest), []
                self._send(b"250 2.1.0 Ok\r\n")
            elif verb == "RCPT":
                if not mailfrom:
                    self._send(b"503 5.5.1 Need MAIL before RCPT\r\n")
                    continue
                rcpttos.append(_addr(rest))
                self._send(b"250 2.1.5 Ok\r\n")
            elif verb == "DATA":
                if cfg.fail_with == "data":
                    self._send(b"554 5.0.0 Transaction failed\r\n")
                    continue
                if not rcpttos:
                    self._send(b"503 5.5.1 Need RCPT before DATA\r\n")
                    continue
                self._send(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                raw = self._read_data()
                if raw is None:
                    return
                idx = cfg.mailbox.record_sent(mailfrom, rcpttos, raw)
                mailfrom, rcpttos = "", []
                self._send(b"250 2.0.0 Ok: queued as %d\r\n" % idx)
            elif verb == "RSET":
                mailfrom, rcpttos = "", []
                self._send(b"250 2.0.0 Ok\r\n")
            elif verb == "NOOP":
                self._send(b"250 2.0.0 Ok\r\n")
            elif verb == "VRFY":
                self._send(b"252 2.5.2 Cannot VRFY user\r\n")
            elif verb == "QUIT":
                self._send(b"221 2.0.0 Bye\r\n")
                return
            else:
                self._send(b"500 5.5.2 Command unrecognized\r\n")

    def _auth(self, rest: str) -> bool:
        """Handle AUTH PLAIN (initial-response or prompted) and both AUTH LOGIN shapes."""
        cfg = self.cfg
        mech, _, arg = rest.partition(" ")
        mech, arg = mech.upper(), arg.strip()
        if mech == "PLAIN":
            if not arg:
                self._send(b"334 \r\n")
                arg = self._read_token()
            try:
                _, user, password = _b64(arg).split("\0", 2)
            except ValueError:
                self._send(b"501 5.5.2 Malformed AUTH PLAIN\r\n")
                return False
        elif mech == "LOGIN":
            if not arg:
                self._send(b"334 VXNlcm5hbWU6\r\n")  # "Username:"
                arg = self._read_token()
            user = _b64(arg)
            self._send(b"334 UGFzc3dvcmQ6\r\n")  # "Password:"
            password = _b64(self._read_token())
        else:
            self._send(b"504 5.5.4 Unrecognized authentication type\r\n")
            return False
        if cfg.fail_with == "auth" or user != cfg.user or password != cfg.password:
            self._send(b"535 5.7.8 Authentication credentials invalid\r\n")
            return False
        self._send(b"235 2.7.0 Authentication successful\r\n")
        return True

    def _read_token(self) -> str:
        line = self.rfile.readline()
        if not line:
            raise OSError("client closed during AUTH")
        return line.rstrip(b"\r\n").decode("ascii", "replace")

    def _read_data(self) -> bytes | None:
        out = bytearray()
        while True:
            line = self.rfile.readline()
            if not line:
                return None
            if line in (b".\r\n", b".\n", b"."):
                return bytes(out)
            if line.startswith(b".."):
                line = line[1:]  # undo smtplib's dot-stuffing
            out += line


def _addr(rest: str) -> str:
    """Pull the address out of `FROM:<a@b>` / `TO:<a@b> SIZE=42`."""
    _, _, tail = rest.partition(":")
    tail = tail.strip()
    if tail.startswith("<"):
        return tail[1 : tail.index(">")] if ">" in tail else tail[1:]
    return tail.split(" ")[0]


def _b64(text: str) -> str:
    return base64.b64decode(text.encode("ascii") + b"===").decode("utf-8", "replace")


class FakeSMTPServer(_Base):
    """Speaks enough ESMTP for smtplib.SMTP: greeting, EHLO (no STARTTLS, AUTH PLAIN/LOGIN
    advertised), AUTH, MAIL FROM, RCPT TO, DATA, QUIT, NOOP, RSET."""

    _handler = _SMTPConn
    _modes = frozenset({"auth", "data", "refuse"})

    def __init__(self, mailbox: FakeMailbox, *, require_auth: bool = True,
                 user: str = "faultline", password: str = "secret",
                 fail_with: str | None = None) -> None:
        super().__init__(mailbox, user=user, password=password, fail_with=fail_with)
        self.require_auth = require_auth

    def start(self) -> "FakeSMTPServer":
        return super().start()


# ---------------------------------------------------------------------------- IMAP
class _IMAPConn(_Conn):
    def converse(self) -> None:
        cfg = self.cfg
        self._send(b"* OK Faultline fake IMAP4rev1 ready\r\n")
        self.state = "NONAUTH"
        while True:
            tokens = self._read_command()
            if not tokens:
                return
            tag = tokens[0]
            cmd = tokens[1].upper() if len(tokens) > 1 else b""
            args = tokens[2:]
            uid_mode = cmd == b"UID"
            if uid_mode:
                if not args:
                    self._send(b"%s BAD UID needs a command\r\n" % tag)
                    continue
                cmd, args = args[0].upper(), args[1:]
            if cmd == b"CAPABILITY":
                self._send(b"* CAPABILITY IMAP4REV1 LITERAL+ UIDPLUS AUTH=PLAIN\r\n")
                done = b"OK CAPABILITY completed"
            elif cmd == b"NOOP":
                done = b"OK NOOP completed"
            elif cmd == b"LOGOUT":
                self._send(b"* BYE Faultline fake IMAP signing off\r\n")
                self._send(b"%s OK LOGOUT completed\r\n" % tag)
                return
            elif cmd == b"LOGIN":
                done = self._login(args)
            elif self.state == "NONAUTH":
                done = b"NO Authenticate first"
            elif cmd in (b"SELECT", b"EXAMINE"):
                done = self._select(readonly=cmd == b"EXAMINE")
            elif self.state != "SELECTED":
                done = b"BAD Select a mailbox first"
            elif cmd == b"SEARCH":
                done = self._search(args, uid_mode)
            elif cmd == b"FETCH":
                done = self._fetch(args, uid_mode)
            elif cmd == b"STORE":
                done = self._store(args, uid_mode)
            elif cmd == b"EXPUNGE":
                for seq in self.cfg.mailbox.expunge():
                    self._send(b"* %d EXPUNGE\r\n" % seq)
                done = b"OK EXPUNGE completed"
            elif cmd == b"CLOSE":
                cfg.mailbox.expunge()  # CLOSE expunges silently, then drops the selection
                self.state = "AUTH"
                done = b"OK CLOSE completed"
            else:
                done = b"BAD Command unrecognized"
            self._send(b"%s %s\r\n" % (tag, done))

    # -------------------------------------------------------------- framing
    def _read_command(self) -> list[bytes]:
        """Read one command, honouring the `{n}` literal form that imaplib may use."""
        line = self.rfile.readline()
        if not line:
            return []
        tokens: list[bytes] = []
        while True:
            more, size = _split_imap(line.rstrip(b"\r\n"))
            tokens += more
            if size is None:
                return tokens
            self._send(b"+ Ready for literal\r\n")
            tokens.append(self.rfile.read(size))
            line = self.rfile.readline()
            if not line:
                return tokens

    # -------------------------------------------------------------- commands
    def _login(self, args: list[bytes]) -> bytes:
        cfg = self.cfg
        creds = [a for a in args if a not in (b"(", b")")]
        if len(creds) < 2:
            return b"BAD LOGIN needs a user and a password"
        user, password = creds[0].decode(), creds[1].decode()
        if cfg.fail_with == "login" or user != cfg.user or password != cfg.password:
            return b"NO [AUTHENTICATIONFAILED] Invalid credentials"
        self.state = "AUTH"
        return b"OK LOGIN completed"

    def _select(self, *, readonly: bool) -> bytes:
        msgs = self.cfg.mailbox.snapshot()
        unseen = [i for i, m in enumerate(msgs, 1) if _SEEN not in m["flags"]]
        self._send(b"* FLAGS (\\Seen \\Deleted)\r\n")
        self._send(b"* %d EXISTS\r\n" % len(msgs))
        self._send(b"* %d RECENT\r\n" % len(unseen))
        self._send(b"* OK [UIDVALIDITY 1] UIDs valid\r\n")
        self._send(b"* OK [UIDNEXT %d] Predicted next UID\r\n" % (self.cfg.mailbox._next_uid))
        if unseen:
            self._send(b"* OK [UNSEEN %d] First unseen\r\n" % unseen[0])
        self.state = "SELECTED"
        mode = b"READ-ONLY" if readonly else b"READ-WRITE"
        return b"OK [%s] %s completed" % (mode, b"EXAMINE" if readonly else b"SELECT")

    def _search(self, args: list[bytes], uid_mode: bool) -> bytes:
        crit = [a for a in args if a not in (b"(", b")")]
        msgs = self.cfg.mailbox.snapshot()
        try:
            pred = _search_predicate(crit)
        except ValueError as exc:
            return b"BAD " + str(exc).encode()
        hits = [(m["uid"] if uid_mode else seq) for seq, m in enumerate(msgs, 1) if pred(m)]
        self._send(b"* SEARCH" + b"".join(b" %d" % h for h in hits) + b"\r\n")
        return b"OK SEARCH completed"

    def _fetch(self, args: list[bytes], uid_mode: bool) -> bytes:
        if not args:
            return b"BAD FETCH needs a set and items"
        spec, items = args[0].decode(), [a for a in args[1:] if a not in (b"(", b")")]
        msgs = self.cfg.mailbox.snapshot()
        highest = max([m["uid"] for m in msgs], default=0) if uid_mode else len(msgs)
        for seq, m in enumerate(msgs, 1):
            if not _match_set(spec, m["uid"] if uid_mode else seq, highest):
                continue
            try:
                parts, implicit_seen = _fetch_parts(m, items, force_uid=uid_mode)
            except ValueError as exc:
                return b"BAD " + str(exc).encode()
            if implicit_seen and _SEEN not in m["flags"]:
                self.cfg.mailbox.set_flags(m["uid"], m["flags"] | {_SEEN})
            self._emit_fetch(seq, parts)
        return b"OK FETCH completed"

    def _store(self, args: list[bytes], uid_mode: bool) -> bytes:
        plain = [a for a in args if a not in (b"(", b")")]
        if len(plain) < 2:
            return b"BAD STORE needs a set, an item and flags"
        spec, action = plain[0].decode(), plain[1].upper().decode()
        flags = {f.decode() for f in plain[2:]}
        silent = action.endswith(".SILENT")
        action = action.removesuffix(".SILENT")
        if action not in ("FLAGS", "+FLAGS", "-FLAGS"):
            return b"BAD STORE item must be FLAGS, +FLAGS or -FLAGS"
        msgs = self.cfg.mailbox.snapshot()
        highest = max([m["uid"] for m in msgs], default=0) if uid_mode else len(msgs)
        for seq, m in enumerate(msgs, 1):
            if not _match_set(spec, m["uid"] if uid_mode else seq, highest):
                continue
            current = set(m["flags"])
            new = flags if action == "FLAGS" else (current | flags if action[0] == "+" else current - flags)
            self.cfg.mailbox.set_flags(m["uid"], new)
            if not silent:
                parts: list[Any] = [b"FLAGS (%s)" % " ".join(sorted(new)).encode()]
                if uid_mode:
                    parts.insert(0, b"UID %d" % m["uid"])
                self._emit_fetch(seq, parts)
        return b"OK STORE completed"

    def _emit_fetch(self, seq: int, parts: list[Any]) -> None:
        """Write `* n FETCH (...)`, splicing literals in place.

        imaplib reads a literal straight off the socket and then reads the rest of the
        line, so nothing may sit between `{n}\\r\\n` and the n payload bytes, and no CRLF
        may follow them before the next item or the closing paren.
        """
        out = bytearray(b"* %d FETCH (" % seq)
        for i, part in enumerate(parts):
            if i:
                out += b" "
            if isinstance(part, tuple):
                name, payload = part
                out += b"%s {%d}\r\n" % (name, len(payload))
                out += payload
            else:
                out += part
        self._send(bytes(out) + b")\r\n")


def _headers(raw: bytes) -> email.message.Message:
    return email.message_from_bytes(raw)


def _search_predicate(crit: list[bytes]) -> Callable[[dict], bool]:
    """Compile SEARCH criteria into an AND-ed predicate; unknown keys raise ValueError."""
    tests: list[Callable[[dict], bool]] = []
    i = 0
    while i < len(crit):
        key = crit[i].upper().decode()
        i += 1
        if key == "ALL":
            continue
        if key == "CHARSET":
            i += 1  # the client's charset hint is irrelevant to an in-memory store
        elif key == "UNSEEN":
            tests.append(lambda m: _SEEN not in m["flags"])
        elif key == "SEEN":
            tests.append(lambda m: _SEEN in m["flags"])
        elif key == "DELETED":
            tests.append(lambda m: _DELETED in m["flags"])
        elif key == "HEADER":
            if i + 1 >= len(crit):
                raise ValueError("HEADER needs a field and a value")
            field, want = crit[i].decode(), crit[i + 1].decode().lower()
            i += 2
            tests.append(lambda m, f=field, w=want: w in str(_headers(m["raw"]).get(f, "")).lower())
        elif key in ("SUBJECT", "FROM", "TO", "CC"):
            if i >= len(crit):
                raise ValueError(f"{key} needs a value")
            want = crit[i].decode().lower()
            i += 1
            tests.append(lambda m, f=key, w=want: w in str(_headers(m["raw"]).get(f, "")).lower())
        elif key in ("BODY", "TEXT"):
            if i >= len(crit):
                raise ValueError(f"{key} needs a value")
            want = crit[i].decode().lower()
            i += 1
            tests.append(lambda m, w=want: w in m["raw"].decode("utf-8", "replace").lower())
        else:
            raise ValueError(f"Unsupported SEARCH criterion {key}")
    return lambda m: all(t(m) for t in tests)


def _fetch_parts(msg: dict, items: list[bytes], *, force_uid: bool) -> tuple[list[Any], bool]:
    """Build the parenthesised FETCH items; literals are (name, payload) tuples."""
    raw, flags = msg["raw"], msg["flags"]
    names = [i.upper() for i in items] or [b"RFC822"]
    parts: list[Any] = []
    if force_uid and b"UID" not in names:
        parts.append(b"UID %d" % msg["uid"])  # RFC 3501: UID FETCH always reports the UID
    implicit_seen = False
    for name in names:
        if name == b"UID":
            parts.append(b"UID %d" % msg["uid"])
        elif name == b"FLAGS":
            parts.append(b"FLAGS (%s)" % " ".join(sorted(flags)).encode())
        elif name == b"RFC822.SIZE":
            parts.append(b"RFC822.SIZE %d" % len(raw))
        elif name in (b"RFC822", b"BODY[]", b"BODY.PEEK[]"):
            parts.append((b"RFC822" if name == b"RFC822" else b"BODY[]", raw))
            implicit_seen |= b"PEEK" not in name
        elif name in (b"RFC822.HEADER", b"BODY[HEADER]", b"BODY.PEEK[HEADER]"):
            head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            parts.append((b"RFC822.HEADER" if name.startswith(b"RFC822") else b"BODY[HEADER]", head))
            implicit_seen |= name == b"RFC822.HEADER"
        else:
            raise ValueError(f"Unsupported FETCH item {name.decode()}")
    return parts, implicit_seen


class FakeIMAPServer(_Base):
    """Speaks enough IMAP4rev1 for imaplib.IMAP4: untagged greeting, CAPABILITY, LOGIN,
    SELECT, SEARCH, FETCH (RFC822 / BODY.PEEK[] / (RFC822)), STORE (+FLAGS \\Seen /
    \\Deleted), UID variants of SEARCH/FETCH/STORE, EXPUNGE, CLOSE, NOOP, LOGOUT."""

    _handler = _IMAPConn
    _modes = frozenset({"login", "refuse"})

    def __init__(self, mailbox: FakeMailbox, *, user: str = "faultline", password: str = "secret",
                 fail_with: str | None = None) -> None:
        super().__init__(mailbox, user=user, password=password, fail_with=fail_with)

    def start(self) -> "FakeIMAPServer":
        return super().start()
