"""CLI entry points for all four commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from . import __version__


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments common to all commands."""
    parser.add_argument("--jid", help="Sender XMPP JID")
    parser.add_argument("--recipient", help="Recipient XMPP JID")
    parser.add_argument("--credentials", help="Path to credentials file")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")


def _setup_logging(verbose: bool = False, quiet: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_json_arg(raw: str) -> dict[str, object]:
    """Parse a JSON string argument, printing error and exiting on failure."""
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        print("Error: invalid JSON", file=sys.stderr)
        sys.exit(1)


# --- bridge ---


def bridge_main() -> None:
    """Entry point for claude-xmpp-bridge daemon."""
    parser = argparse.ArgumentParser(description="XMPP bridge daemon for Claude Code")
    _add_common_args(parser)
    parser.add_argument("--socket-path", help="Unix socket path")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--messages", help="Path to messages TOML file")
    parser.add_argument("--mcp-port", type=int, help="MCP HTTP server port (default 7878, 0 = disabled)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only warnings/errors")
    args = parser.parse_args()

    _setup_logging(args.verbose, args.quiet)

    from .config import load_config, validate_config

    config = load_config(
        cli_jid=args.jid,
        cli_recipient=args.recipient,
        cli_credentials=args.credentials,
        cli_socket_path=args.socket_path,
        cli_db_path=args.db_path,
        cli_messages=args.messages,
        cli_mcp_port=args.mcp_port,
    )
    validate_config(config)

    from .bridge import XMPPBridge

    bridge = XMPPBridge(config)
    asyncio.run(bridge.run())


# --- client ---


def client_main() -> None:
    """Entry point for claude-xmpp-client."""
    parser = argparse.ArgumentParser(description="Client for claude-xmpp-bridge daemon")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--socket-path", help="Unix socket path")
    sub = parser.add_subparsers(dest="command")

    # send
    p_send = sub.add_parser("send", help="Send XMPP message")
    p_send.add_argument("message", nargs="*", help="Message text (reads stdin if omitted)")

    # register
    p_reg = sub.add_parser(
        "register",
        help="Register a session",
        epilog='JSON: {"session_id":"…","sty":"…","window":"…","project":"…","backend":"screen|tmux|none"}',
    )
    p_reg.add_argument("json_data", help="Session JSON data")

    # unregister
    p_unreg = sub.add_parser("unregister", help="Unregister a session")
    p_unreg.add_argument("session_id", help="Session ID")

    # notify
    p_notify = sub.add_parser(
        "notify",
        help="Send a session-tagged XMPP notification (bridge adds icon + window ID)",
        epilog='JSON: {"session_id":"…","message":"…"}',
    )
    p_notify.add_argument("json_data", help="Notification JSON data")

    # response
    p_resp = sub.add_parser("response", help="Send a response notification")
    p_resp.add_argument("json_data", help="Response JSON data")

    # query
    p_query = sub.add_parser("query", help="Query registered project for a session")
    p_query.add_argument("session_id", help="Session ID")

    # ping
    sub.add_parser("ping", help="Check if bridge daemon is running (exit 0 = running)")

    # relay
    p_relay = sub.add_parser(
        "relay",
        help="Send a message from one agent to another via the bridge",
        epilog="Specify target with --to SESSION_ID or --to-index N",
    )
    p_relay.add_argument("message", nargs="*", help="Message text (reads stdin if omitted)")
    p_relay.add_argument("--to", metavar="SESSION_ID", help="Target session ID")
    p_relay.add_argument("--to-index", type=int, metavar="N", help="Target session index (from /list)")
    p_relay.add_argument("--session-id", default=None, help="Sender session ID (for labelling)")

    # broadcast
    p_broadcast = sub.add_parser(
        "broadcast",
        help="Send a message to all other registered sessions",
    )
    p_broadcast.add_argument("message", nargs="*", help="Message text (reads stdin if omitted)")
    p_broadcast.add_argument("--session-id", default=None, help="Sender session ID (excluded from delivery)")

    args = parser.parse_args()

    from .client import fallback_notify, send_to_bridge
    from .config import DEFAULT_SOCKET_PATH

    socket_path = Path(args.socket_path) if args.socket_path else DEFAULT_SOCKET_PATH

    if args.command == "send":
        message_parts: list[str] = args.message or []
        if message_parts:
            message = " ".join(message_parts)
        elif not sys.stdin.isatty():
            message = sys.stdin.read().strip()
        else:
            print("Error: no message provided", file=sys.stderr)
            print("Usage: claude-xmpp-client send MESSAGE", file=sys.stderr)
            print("       echo MESSAGE | claude-xmpp-client send", file=sys.stderr)
            sys.exit(1)

        if not message:
            sys.exit(0)

        result = send_to_bridge({"cmd": "send", "message": message}, socket_path)
        if result is None:
            fallback_notify(message)
        elif "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "register":
        data = _parse_json_arg(args.json_data)
        data["cmd"] = "register"
        result = send_to_bridge(data, socket_path)
        if result is not None and "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "unregister":
        result = send_to_bridge({"cmd": "unregister", "session_id": args.session_id}, socket_path)
        if result is not None and "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "notify":
        data = _parse_json_arg(args.json_data)
        data["cmd"] = "notify"
        result = send_to_bridge(data, socket_path)
        if result is None:
            message = str(data.get("message", ""))
            if message:
                fallback_notify(message)
        elif "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "response":
        data = _parse_json_arg(args.json_data)
        data["cmd"] = "response"
        result = send_to_bridge(data, socket_path)
        if result is None:
            message = str(data.get("message", ""))
            if message:
                fallback_notify(message)
        elif "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "query":
        result = send_to_bridge({"cmd": "query", "session_id": args.session_id}, socket_path)
        if result and result.get("ok"):
            print(result["project"])
        else:
            sys.exit(1)

    elif args.command == "ping":
        result = send_to_bridge({"cmd": "ping"}, socket_path)
        if result and result.get("ok"):
            print("bridge: running")
        else:
            print("bridge: not running", file=sys.stderr)
            sys.exit(1)

    elif args.command == "relay":
        message_parts = args.message or []
        if message_parts:
            message = " ".join(message_parts)
        elif not sys.stdin.isatty():
            message = sys.stdin.read().strip()
        else:
            print("Error: no message provided", file=sys.stderr)
            sys.exit(1)
        if not message:
            sys.exit(0)
        if not args.to and args.to_index is None:
            print("Error: specify --to SESSION_ID or --to-index N", file=sys.stderr)
            sys.exit(1)
        req: dict[str, object] = {"cmd": "relay", "message": message}
        if args.to:
            req["to"] = args.to
        if args.to_index is not None:
            req["to_index"] = args.to_index
        if args.session_id:
            req["session_id"] = args.session_id
        result = send_to_bridge(req, socket_path)
        if result is None:
            print("Error: bridge not running", file=sys.stderr)
            sys.exit(1)
        elif not result.get("ok"):
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "broadcast":
        message_parts = args.message or []
        if message_parts:
            message = " ".join(message_parts)
        elif not sys.stdin.isatty():
            message = sys.stdin.read().strip()
        else:
            print("Error: no message provided", file=sys.stderr)
            sys.exit(1)
        if not message:
            sys.exit(0)
        req = {"cmd": "broadcast", "message": message}
        if args.session_id:
            req["session_id"] = args.session_id
        result = send_to_bridge(req, socket_path)
        if result is None:
            print("Error: bridge not running", file=sys.stderr)
            sys.exit(1)
        elif not result.get("ok"):
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)
        else:
            delivered = result.get("delivered", 0)
            print(f"broadcast delivered to {delivered} session(s)")

    else:
        parser.print_help()
        sys.exit(1)


# --- notify ---


def notify_main() -> None:
    """Entry point for claude-xmpp-notify."""
    parser = argparse.ArgumentParser(description="Send XMPP notification")
    _add_common_args(parser)
    parser.add_argument("message", nargs="?", default=None, help="Message (reads stdin if omitted)")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose, args.quiet)

    if args.message:
        message = args.message
    elif not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    else:
        print("Error: no message provided (pass as argument or pipe to stdin)", file=sys.stderr)
        sys.exit(1)

    if not message:
        sys.exit(0)

    from .config import load_notify_config
    from .notify import send_notification

    config = load_notify_config(
        cli_jid=args.jid,
        cli_recipient=args.recipient,
        cli_credentials=args.credentials,
    )
    try:
        asyncio.run(send_notification(config, message))
    except ConnectionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# --- ask ---


def ask_main() -> None:
    """Entry point for claude-xmpp-ask."""
    parser = argparse.ArgumentParser(description="Send XMPP message and wait for reply")
    _add_common_args(parser)
    parser.add_argument("message", nargs="?", default=None, help="Message (reads stdin if omitted)")
    parser.add_argument("--timeout", type=int, default=300, help="Reply timeout in seconds (default: 300)")
    parser.add_argument("--session-id", default=None, help="Session ID for tagging the ask message")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose, args.quiet)

    if args.message:
        message = args.message
    elif not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    else:
        print("Error: no message provided", file=sys.stderr)
        sys.exit(1)

    if not message:
        sys.exit(0)

    # Try bridge socket first (avoids opening a separate XMPP connection).
    # Only treat the result as authoritative when "ok" is present — that means
    # the bridge understood the "ask" command.  A bare {"error": …} (e.g.
    # "unknown command") means the bridge is too old → fall through to XMPP.
    from .client import send_to_bridge

    ask_req: dict[str, object] = {"cmd": "ask", "message": message, "timeout": args.timeout}
    if args.session_id:
        ask_req["session_id"] = args.session_id
    result = send_to_bridge(
        ask_req,
        socket_timeout=args.timeout + 10,
    )
    if result is not None and "ok" in result:
        if result["ok"] and result.get("reply"):
            print(result["reply"])
            sys.exit(0)
        else:
            error = result.get("error", "no reply")
            print(f"No reply ({error})", file=sys.stderr)
            sys.exit(1)

    # Fallback: direct XMPP connection (bridge not running)
    from .ask import send_and_wait
    from .config import load_notify_config

    config = load_notify_config(
        cli_jid=args.jid,
        cli_recipient=args.recipient,
        cli_credentials=args.credentials,
    )
    try:
        reply = asyncio.run(send_and_wait(config, message, timeout=args.timeout))
    except ConnectionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if reply is not None:
        print(reply)
    else:
        print(f"No reply received (waited {args.timeout}s)", file=sys.stderr)
        sys.exit(1)
