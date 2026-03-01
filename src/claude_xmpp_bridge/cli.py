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


# --- bridge ---


def bridge_main() -> None:
    """Entry point for claude-xmpp-bridge daemon."""
    parser = argparse.ArgumentParser(
        description="XMPP bridge daemon for Claude Code"
    )
    _add_common_args(parser)
    parser.add_argument("--socket-path", help="Unix socket path")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--messages", help="Path to messages TOML file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only warnings/errors")
    args = parser.parse_args()

    _setup_logging(args.verbose, args.quiet)

    from .config import load_config

    config = load_config(
        cli_jid=args.jid,
        cli_recipient=args.recipient,
        cli_credentials=args.credentials,
        cli_socket_path=args.socket_path,
        cli_db_path=args.db_path,
        cli_messages=args.messages,
    )

    from .bridge import XMPPBridge

    bridge = XMPPBridge(config)
    asyncio.run(bridge.run())


# --- client ---


def client_main() -> None:
    """Entry point for claude-xmpp-client."""
    parser = argparse.ArgumentParser(
        description="Client for claude-xmpp-bridge daemon"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--socket-path", help="Unix socket path")
    sub = parser.add_subparsers(dest="command")

    # send
    p_send = sub.add_parser("send", help="Send XMPP message")
    p_send.add_argument("message", nargs="*", help="Message text (reads stdin if omitted)")

    # register
    p_reg = sub.add_parser("register", help="Register a session")
    p_reg.add_argument("json_data", help="Session JSON data")

    # unregister
    p_unreg = sub.add_parser("unregister", help="Unregister a session")
    p_unreg.add_argument("session_id", help="Session ID")

    # response
    p_resp = sub.add_parser("response", help="Send a response notification")
    p_resp.add_argument("json_data", help="Response JSON data")

    # query
    p_query = sub.add_parser("query", help="Query registered project for a session")
    p_query.add_argument("session_id", help="Session ID")

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
            print("Error: no message", file=sys.stderr)
            sys.exit(1)

        if not message:
            sys.exit(0)

        result = send_to_bridge({"cmd": "send", "message": message}, socket_path)
        if result is None:
            fallback_notify(message)

    elif args.command == "register":
        try:
            data = json.loads(args.json_data)
        except json.JSONDecodeError:
            print("Error: invalid JSON", file=sys.stderr)
            sys.exit(1)
        data["cmd"] = "register"
        send_to_bridge(data, socket_path)

    elif args.command == "unregister":
        send_to_bridge(
            {"cmd": "unregister", "session_id": args.session_id}, socket_path
        )

    elif args.command == "response":
        try:
            data = json.loads(args.json_data)
        except json.JSONDecodeError:
            print("Error: invalid JSON", file=sys.stderr)
            sys.exit(1)
        data["cmd"] = "response"
        result = send_to_bridge(data, socket_path)
        if result is None:
            message = data.get("message", "")
            if message:
                fallback_notify(str(message))

    elif args.command == "query":
        result = send_to_bridge(
            {"cmd": "query", "session_id": args.session_id}, socket_path
        )
        if result and result.get("ok"):
            print(result["project"])
        else:
            sys.exit(1)

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
