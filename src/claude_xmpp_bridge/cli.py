"""CLI entry points for all four commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
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


def _print_bridge_error(result: dict[str, object] | None) -> None:
    """Print a uniform bridge/socket error message and exit non-zero."""
    message = str(result.get("error", "bridge not running")) if result else "bridge not running"
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _default_bridge_session_id(explicit: str | None) -> str | None:
    """Return explicit sender session_id or fallback to BRIDGE_SESSION_ID env."""
    if explicit:
        return explicit
    value = os.environ.get("BRIDGE_SESSION_ID", "").strip()
    return value or None


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

    # list
    sub.add_parser(
        "list",
        help="List all registered sessions as JSON",
    )

    # get-context
    p_ctx = sub.add_parser("get-context", help="Get coordination context for one session as JSON")
    p_ctx.add_argument("session_id", help="Session ID")

    # todos
    p_list_todos = sub.add_parser("list-todos", help="List stored todos for a session as JSON")
    p_list_todos.add_argument("session_id", help="Session ID")

    p_replace_todos = sub.add_parser("replace-todos", help="Replace stored todos for a session from JSON")
    p_replace_todos.add_argument("json_data", help='JSON: {"session_id":"...","todos":[...],"expected_version":N?}')

    p_add_todo = sub.add_parser("add-todo", help="Add one todo item to a session")
    p_add_todo.add_argument("session_id", help="Session ID")
    p_add_todo.add_argument("content", help="Todo text")
    p_add_todo.add_argument("--status", default="pending")
    p_add_todo.add_argument("--priority", default="medium")
    p_add_todo.add_argument("--expected-version", type=int)

    p_update_todo = sub.add_parser("update-todo", help="Update one todo item by id")
    p_update_todo.add_argument("session_id", help="Session ID")
    p_update_todo.add_argument("todo_id", help="Todo ID")
    p_update_todo.add_argument("--content")
    p_update_todo.add_argument("--status")
    p_update_todo.add_argument("--priority")
    p_update_todo.add_argument("--expected-version", type=int)

    p_remove_todo = sub.add_parser("remove-todo", help="Remove one todo item by id")
    p_remove_todo.add_argument("session_id", help="Session ID")
    p_remove_todo.add_argument("todo_id", help="Todo ID")
    p_remove_todo.add_argument("--expected-version", type=int)

    p_reply_last = sub.add_parser("reply-last", help="Reply to the last remembered agent sender")
    p_reply_last.add_argument("session_id", help="Your session ID")
    p_reply_last.add_argument("message", nargs="*", help="Reply text (reads stdin if omitted)")
    p_reply_last.add_argument("--screen", action="store_true", help="Use direct screen relay instead of nudge")

    # locks
    p_list_locks = sub.add_parser("list-locks", help="List file locks as JSON")
    p_list_locks.add_argument("--project", default="")
    p_list_locks.add_argument("--hide-stale", action="store_true")

    p_acquire_lock = sub.add_parser("acquire-lock", help="Acquire a bridge-native file lock")
    p_acquire_lock.add_argument("session_id", help="Session ID")
    p_acquire_lock.add_argument("filepath", help="File path")
    p_acquire_lock.add_argument("--project", default="")
    p_acquire_lock.add_argument("--reason", default="")

    p_release_lock = sub.add_parser("release-lock", help="Release a bridge-native file lock")
    p_release_lock.add_argument("session_id", help="Session ID")
    p_release_lock.add_argument("filepath", help="File path")
    p_release_lock.add_argument("--force", action="store_true")

    p_cleanup_locks = sub.add_parser("cleanup-locks", help="Remove stale file locks")
    p_cleanup_locks.add_argument("--project", default="")

    # task delegation
    p_delegate = sub.add_parser(
        "delegate",
        help="Delegate a task to another agent session",
    )
    p_delegate.add_argument("description", nargs="*", help="Task description (reads stdin if omitted)")
    p_delegate.add_argument("--to", metavar="SESSION_ID", required=True, help="Target session ID")
    p_delegate.add_argument("--context", default="", help="Additional context for the task")
    p_delegate.add_argument("--session-id", default=None, help="Sender (delegator) session ID")
    p_delegate.add_argument("--no-nudge", action="store_true", help="Don't nudge the target")

    p_task_result = sub.add_parser(
        "task-result",
        help="Report the result of a delegated task",
    )
    p_task_result.add_argument("task_id", help="Task ID")
    p_task_result.add_argument("status", choices=["accepted", "completed", "failed", "cancelled"])
    p_task_result.add_argument("result", nargs="*", help="Result text (reads stdin if omitted)")
    p_task_result.add_argument("--session-id", default=None, help="Sender (assignee) session ID")
    p_task_result.add_argument("--no-nudge", action="store_true", help="Don't nudge the delegator")

    p_list_tasks = sub.add_parser("list-tasks", help="List delegated tasks as JSON")
    p_list_tasks.add_argument("--session-id", default=None, help="Filter by session ID")
    p_list_tasks.add_argument("--role", choices=["from", "to", "both"], default="both")
    p_list_tasks.add_argument("--status", default="")

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

    # state
    p_state = sub.add_parser(
        "state",
        help="Update agent state for a registered session",
        epilog='JSON: {"session_id":"…","state":"idle|running"}',
    )
    p_state.add_argument("json_data", help="State JSON data")

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

    elif args.command == "list":
        result = send_to_bridge({"cmd": "list"}, socket_path)
        if result and result.get("ok"):
            print(json.dumps(result["sessions"]))
        else:
            print("Error: bridge not running or list failed", file=sys.stderr)
            sys.exit(1)

    elif args.command == "get-context":
        result = send_to_bridge({"cmd": "get_context", "session_id": args.session_id}, socket_path)
        if result and result.get("ok"):
            print(json.dumps(result))
        else:
            _print_bridge_error(result)

    elif args.command == "list-todos":
        result = send_to_bridge({"cmd": "list_todos", "session_id": args.session_id}, socket_path)
        if result and result.get("ok"):
            print(json.dumps(result["todos"]))
        else:
            _print_bridge_error(result)

    elif args.command == "replace-todos":
        data = _parse_json_arg(args.json_data)
        data["cmd"] = "replace_todos"
        result = send_to_bridge(data, socket_path)
        if result and result.get("ok"):
            print(json.dumps(result))
        else:
            _print_bridge_error(result)

    elif args.command == "add-todo":
        add_req = {
            "cmd": "add_todo",
            "session_id": args.session_id,
            "content": args.content,
            "status": args.status,
            "priority": args.priority,
        }
        if args.expected_version is not None:
            add_req["expected_version"] = args.expected_version
        result = send_to_bridge(add_req, socket_path)
        if result and result.get("ok"):
            print(json.dumps(result))
        else:
            _print_bridge_error(result)

    elif args.command == "update-todo":
        update_req: dict[str, object] = {
            "cmd": "update_todo",
            "session_id": args.session_id,
            "todo_id": args.todo_id,
        }
        if args.content is not None:
            update_req["content"] = args.content
        if args.status is not None:
            update_req["status"] = args.status
        if args.priority is not None:
            update_req["priority"] = args.priority
        if args.expected_version is not None:
            update_req["expected_version"] = args.expected_version
        result = send_to_bridge(update_req, socket_path)
        if result and result.get("ok"):
            print(json.dumps(result))
        else:
            _print_bridge_error(result)

    elif args.command == "remove-todo":
        remove_req: dict[str, object] = {
            "cmd": "remove_todo",
            "session_id": args.session_id,
            "todo_id": args.todo_id,
        }
        if args.expected_version is not None:
            remove_req["expected_version"] = args.expected_version
        result = send_to_bridge(remove_req, socket_path)
        if result and result.get("ok"):
            print(json.dumps(result))
        else:
            _print_bridge_error(result)

    elif args.command == "reply-last":
        reply_parts: list[str] = args.message or []
        if reply_parts:
            message = " ".join(reply_parts)
        elif not sys.stdin.isatty():
            message = sys.stdin.read().strip()
        else:
            print("Error: no message provided", file=sys.stderr)
            print("Usage: claude-xmpp-client reply-last SESSION_ID MESSAGE", file=sys.stderr)
            print("       echo MESSAGE | claude-xmpp-client reply-last SESSION_ID", file=sys.stderr)
            sys.exit(1)
        if not message:
            sys.exit(0)
        result = send_to_bridge(
            {
                "cmd": "reply_to_last_sender",
                "session_id": args.session_id,
                "message": message,
                "nudge": not args.screen,
            },
            socket_path,
        )
        if result and result.get("ok"):
            print(json.dumps(result))
        else:
            _print_bridge_error(result)

    elif args.command == "list-locks":
        result = send_to_bridge(
            {"cmd": "list_file_locks", "project": args.project, "include_stale": not args.hide_stale}, socket_path
        )
        if result and result.get("ok"):
            print(json.dumps(result["locks"]))
        else:
            _print_bridge_error(result)

    elif args.command == "acquire-lock":
        acquire_req = {
            "cmd": "acquire_file_lock",
            "session_id": args.session_id,
            "filepath": args.filepath,
            "project": args.project,
            "reason": args.reason,
        }
        result = send_to_bridge(acquire_req, socket_path)
        if result and "ok" in result:
            print(json.dumps(result))
            if not result.get("ok"):
                sys.exit(1)
        else:
            _print_bridge_error(result)

    elif args.command == "release-lock":
        release_req = {
            "cmd": "release_file_lock",
            "session_id": args.session_id,
            "filepath": args.filepath,
            "force": args.force,
        }
        result = send_to_bridge(release_req, socket_path)
        if result and result.get("ok"):
            print(json.dumps(result))
        else:
            _print_bridge_error(result)

    elif args.command == "cleanup-locks":
        result = send_to_bridge({"cmd": "cleanup_stale_locks", "project": args.project}, socket_path)
        if result and result.get("ok"):
            print(json.dumps(result))
        else:
            _print_bridge_error(result)

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
        relay_req: dict[str, object] = {"cmd": "relay", "message": message}
        if args.to:
            relay_req["to"] = args.to
        if args.to_index is not None:
            relay_req["to_index"] = args.to_index
        sender_session_id = _default_bridge_session_id(args.session_id)
        if sender_session_id:
            relay_req["session_id"] = sender_session_id
        result = send_to_bridge(relay_req, socket_path)
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
        broadcast_req: dict[str, object] = {"cmd": "broadcast", "message": message}
        sender_session_id = _default_bridge_session_id(args.session_id)
        if sender_session_id:
            broadcast_req["session_id"] = sender_session_id
        result = send_to_bridge(broadcast_req, socket_path)
        if result is None:
            print("Error: bridge not running", file=sys.stderr)
            sys.exit(1)
        elif not result.get("ok"):
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)
        else:
            delivered = result.get("delivered", 0)
            print(f"broadcast delivered to {delivered} session(s)")

    elif args.command == "state":
        data = _parse_json_arg(args.json_data)
        data["cmd"] = "state"
        result = send_to_bridge(data, socket_path)
        if result is None:
            print("Error: bridge not running", file=sys.stderr)
            sys.exit(1)
        elif "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "delegate":
        desc_parts: list[str] = args.description or []
        if desc_parts:
            description = " ".join(desc_parts)
        elif not sys.stdin.isatty():
            description = sys.stdin.read().strip()
        else:
            print("Error: no description provided", file=sys.stderr)
            sys.exit(1)
        if not description:
            sys.exit(0)
        delegate_req: dict[str, object] = {
            "cmd": "delegate",
            "to": args.to,
            "description": description,
            "nudge": not args.no_nudge,
        }
        if args.context:
            delegate_req["context"] = args.context
        sender_session_id = _default_bridge_session_id(args.session_id)
        if sender_session_id:
            delegate_req["session_id"] = sender_session_id
        result = send_to_bridge(delegate_req, socket_path)
        if result is None:
            print("Error: bridge not running", file=sys.stderr)
            sys.exit(1)
        elif not result.get("ok"):
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)
        else:
            print(json.dumps(result))

    elif args.command == "task-result":
        result_parts: list[str] = args.result or []
        if result_parts:
            result_text = " ".join(result_parts)
        elif not sys.stdin.isatty():
            result_text = sys.stdin.read().strip()
        else:
            result_text = ""
        task_result_req: dict[str, object] = {
            "cmd": "task_result",
            "task_id": args.task_id,
            "status": args.status,
            "nudge": not args.no_nudge,
        }
        if result_text:
            task_result_req["result"] = result_text
        sender_session_id = _default_bridge_session_id(args.session_id)
        if sender_session_id:
            task_result_req["session_id"] = sender_session_id
        result = send_to_bridge(task_result_req, socket_path)
        if result is None:
            print("Error: bridge not running", file=sys.stderr)
            sys.exit(1)
        elif not result.get("ok"):
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)
        else:
            print(json.dumps(result))

    elif args.command == "list-tasks":
        list_tasks_req: dict[str, object] = {
            "cmd": "list_tasks",
            "role": args.role,
        }
        if args.session_id:
            list_tasks_req["session_id"] = args.session_id
        if args.status:
            list_tasks_req["status"] = args.status
        result = send_to_bridge(list_tasks_req, socket_path)
        if result and result.get("ok"):
            print(json.dumps(result.get("tasks", []), indent=2))
        else:
            _print_bridge_error(result)

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
