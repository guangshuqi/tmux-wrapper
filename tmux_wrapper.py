#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "click>=8.1.0",
# ]
# ///

"""
Tmux wrapper CLI that auto-detects when command output is complete.
"""

import click
import subprocess
import time
import sys
import re
from typing import Optional, Tuple

# Pre-compile regex for stripping ANSI escape sequences produced by colorized prompts
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B[@-_][0-?]*[ -/]*[@-~]")


def capture_tmux_output(session_name: str, lines: int = 100) -> str:
    """Capture output from a tmux session."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", f"-{lines}"],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.stdout
    except subprocess.TimeoutExpired:
        return ""
    except subprocess.CalledProcessError:
        return ""


def wait_for_output_completion(
    session_name: str,
    command_sent: Optional[str] = None,
    max_wait_sec: int = 20,
    pre_command_output: Optional[str] = None
) -> Tuple[str, bool, bool]:
    """
    Wait until tmux output shows a prompt indicating command completion.

    Looks for common prompt patterns:
    - Shell prompts ending with '$', '#', or '>'
    - Rails console: 'pry(main)>'
    - Bash prompt with '#'

    Returns:
        (final_output, timed_out, session_exited)
    """
    start_time = time.time()
    poll_interval = 0.1  # 100ms polling

    # Prompt patterns that indicate command is done
    prompt_patterns = [
        "pry(main)>",  # Rails console
        "]$",          # Bash prompt like [user@host dir]$
        "]#",          # Root bash prompt like [root@host dir]#
        "#",           # Shell prompt with #
        "$",           # Shell prompt with $
    ]

    # Capture initial output to avoid matching stale prompts
    initial_output = pre_command_output if pre_command_output is not None else capture_tmux_output(session_name)
    last_output = initial_output
    output_changed = pre_command_output is None
    command_seen = False

    if command_sent and command_sent in initial_output:
        command_seen = True

    poll_count = 0

    while True:
        poll_count += 1
        elapsed = time.time() - start_time

        # Check timeout first
        if elapsed > max_wait_sec:
            current_output = capture_tmux_output(session_name)
            return _trim_output_to_command(current_output, command_sent), True, False

        # Check if session still exists
        if not session_exists(session_name):
            current_output = capture_tmux_output(session_name)
            return _trim_output_to_command(current_output, command_sent), False, True

        current_output = capture_tmux_output(session_name)

        if command_sent and command_sent in current_output and not command_seen:
            command_seen = True

        # Track if output has changed from initial state
        if current_output != last_output:
            output_changed = True
            last_output = current_output

        # Only check for prompts after output has changed or the command is visible
        trimmed_output = _trim_output_to_command(current_output, command_sent)
        check_prompts = (output_changed or command_seen) and trimmed_output

        if check_prompts:
            last_line = trimmed_output.strip().split('\n')[-1].strip() if trimmed_output.strip() else ""
            clean_last_line = strip_ansi_escape_sequences(last_line)

            # Check if line ends with any prompt pattern
            for pattern in prompt_patterns:
                if clean_last_line.endswith(pattern):
                    return _trim_output_to_command(current_output, command_sent), False, False

        time.sleep(poll_interval)


def _trim_output_to_command(output: str, command: Optional[str]) -> str:
    """
    Trim output to only show from the command onwards.
    If command is not found, return the full output.
    """
    if not command or not output:
        return output

    # Find the command in the output
    lines = output.split('\n')
    for idx in range(len(lines) - 1, -1, -1):
        if command in lines[idx]:
            # Return everything from this line onwards
            return '\n'.join(lines[idx:])

    # If command not found, return full output
    return output


def strip_ansi_escape_sequences(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return ANSI_ESCAPE_PATTERN.sub("", text)


def session_exists(session_name: str) -> bool:
    """Check if a tmux session exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
        text=True
    )
    return result.returncode == 0


@click.group()
def cli():
    """Tmux wrapper with auto-detection of output completion."""
    pass


@cli.command(name="new-tmux-session")
@click.option("-s", "--session-name", required=True, help="Session name")
@click.option("-c", "--start-directory", help="Working directory")
@click.argument("command", required=True)
def new_tmux_session(
    session_name: str,
    start_directory: Optional[str],
    command: str
):
    """
    Create a new detached tmux session with fallback shell and wait for output completion.

    Example:
        tmux_wrapper.py new-tmux-session -s mysession -c ~/mydir "echo hello && sleep 1 && echo world"
    """
    # Check if session already exists
    if session_exists(session_name):
        click.echo(f"Error: Session '{session_name}' already exists", err=True)
        sys.exit(1)

    # Add fallback shell for debugging if command fails
    fallback_cmd = f'echo "ERROR: Command failed. Opening shell for debugging." && exec bash'
    wrapped_command = f'{command} || ({fallback_cmd})'

    # Build tmux command (always detached)
    tmux_cmd = ["tmux", "new-session", "-d"]
    tmux_cmd.extend(["-s", session_name])
    if start_directory:
        tmux_cmd.extend(["-c", start_directory])
    tmux_cmd.append(wrapped_command)

    # Create session
    try:
        result = subprocess.run(tmux_cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            click.echo(f"Error creating session: {result.stderr}", err=True)
            sys.exit(1)
    except subprocess.TimeoutExpired:
        click.echo("Error: tmux new-session command timed out", err=True)
        sys.exit(1)

    # Give session a moment to initialize
    time.sleep(0.1)

    # Check if session still exists (command might have failed immediately)
    if not session_exists(session_name):
        click.echo(f"Error: Session '{session_name}' exited immediately", err=True)
        sys.exit(1)

    # Wait for prompt to appear
    output, timed_out, session_exited = wait_for_output_completion(session_name, command)

    # Print output
    click.echo(output, nl=False)

    if session_exited:
        click.echo(f"\n[Session exited - command completed]", err=True)
        sys.exit(0)
    elif timed_out:
        click.echo(f"\n[Timeout after 20s - session still running]", err=True)
        sys.exit(2)


@cli.command(name="send-keys")
@click.option("-t", "--target-session", required=True, help="Target session name")
@click.option("--no-enter", is_flag=True, help="Don't send Enter key after command")
@click.argument("keys", required=True)
def send_keys(
    target_session: str,
    no_enter: bool,
    keys: str
):
    """
    Send keys to a tmux session and wait for output completion.

    Example:
        tmux_wrapper.py send-keys -t mysession "echo hello && sleep 1 && echo world"
    """
    # Check if session exists
    if not session_exists(target_session):
        click.echo(f"Error: Session '{target_session}' does not exist", err=True)
        sys.exit(1)

    # Capture the current pane contents before sending the command
    pre_command_output = capture_tmux_output(target_session)

    # Build tmux send-keys command
    tmux_cmd = ["tmux", "send-keys", "-t", target_session, keys]
    if not no_enter:
        tmux_cmd.append("C-m")  # Send Enter

    # Send keys
    try:
        result = subprocess.run(tmux_cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            click.echo(f"Error sending keys: {result.stderr}", err=True)
            sys.exit(1)
    except subprocess.TimeoutExpired:
        click.echo("Error: tmux send-keys command timed out", err=True)
        sys.exit(1)

    # Wait for prompt to appear
    output, timed_out, session_exited = wait_for_output_completion(
        target_session,
        keys,
        pre_command_output=pre_command_output
    )

    # Print output
    click.echo(output, nl=False)

    if session_exited:
        click.echo(f"\n[Session exited]", err=True)
        sys.exit(1)
    elif timed_out:
        click.echo(f"\n[Timeout after 20s - session still running]", err=True)
        sys.exit(2)


if __name__ == "__main__":
    cli()
