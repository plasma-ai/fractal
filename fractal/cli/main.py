"""Command-line interface for ``fractal``."""

from __future__ import annotations

import sys
from typing import Any

import typer

from . import cmd

__all__ = ['cli']

# the failure line's bold-red styling, applied only on a tty -- piped
# consumers get the bare text (the marker is the word, not the color)
_FAIL = '\033[1;31m'
_RESET = '\033[0m'


def cli(**kwargs: Any) -> None:
    """Run the ``fractal`` CLI."""
    # construct app
    kwargs.setdefault('pretty_exceptions_enable', False)
    app = typer.Typer(name='fractal', **kwargs)
    # version callback
    cmd.version(app)
    # fractal commands (public)
    cmd.install(app)
    cmd.init(app)
    cmd.track(app)
    cmd.untrack(app)
    cmd.commit(app)
    cmd.open(app)
    cmd.pause(app)
    cmd.resume(app)
    cmd.reset(app)
    cmd.destroy(app)
    # node sub-apps (public)
    node_app = typer.Typer(
        name='node',
        help='Node lifecycle and management.',
        **kwargs,
    )
    cost_app = typer.Typer(
        name='cost',
        help='Track API costs.',
        **kwargs,
    )
    time_app = typer.Typer(
        name='time',
        help='Track iteration time.',
        **kwargs,
    )
    cmd.node_init(node_app)
    cmd.node_start(node_app)
    cmd.node_finish(node_app)
    cmd.node_stop(node_app)
    cmd.node_pause(node_app)
    cmd.node_resume(node_app)
    cmd.node_kill(node_app)
    cmd.node_merge(node_app)
    cmd.node_delete(node_app)
    cmd.node_reconcile(node_app)
    cmd.node_retire(node_app)
    cmd.node_unretire(node_app)
    cmd.node_attach(node_app)
    cmd.node_status(node_app)
    cmd.node_list(node_app)
    cmd.node_activity(node_app)
    cmd.node_approve(node_app)
    cmd.node_pending(node_app)
    cmd.node_chat(node_app)
    cmd.node_update(node_app)
    cmd.node_loop(node_app)
    cmd.node_seed(node_app)
    cmd.time_remaining(time_app)
    cmd.cost_remaining(cost_app)
    cmd.cost_spent(cost_app)
    cmd.cost_breakdown(cost_app)
    node_app.add_typer(time_app)
    node_app.add_typer(cost_app)
    # node config sub-app (public)
    node_config_app = typer.Typer(
        name='config',
        help='Get or set a node config value.',
        **kwargs,
    )
    cmd.node_config_get(node_config_app)
    cmd.node_config_set(node_config_app)
    node_app.add_typer(node_config_app)
    app.add_typer(node_app)
    # radio sub-app (public)
    radio_app = typer.Typer(
        name='radio',
        help='Communicate between nodes.',
        **kwargs,
    )
    cmd.radio_send(radio_app)
    cmd.radio_post(radio_app)
    cmd.radio_unsend(radio_app)
    cmd.radio_save(radio_app)
    cmd.radio_unsave(radio_app)
    cmd.radio_messages(radio_app)
    cmd.radio_sent(radio_app)
    cmd.radio_relays(radio_app)
    cmd.radio_feed(radio_app)
    cmd.radio_read(radio_app)
    cmd.radio_thread(radio_app)
    cmd.radio_reply(radio_app)
    cmd.radio_react(radio_app)
    cmd.radio_sub(radio_app)
    cmd.radio_unsub(radio_app)
    cmd.radio_subs(radio_app)
    # radio channel sub-app (public)
    radio_channel_app = typer.Typer(
        name='channel',
        help='Manage radio channels.',
        **kwargs,
    )
    cmd.radio_channel_create(radio_channel_app)
    cmd.radio_channel_delete(radio_channel_app)
    cmd.radio_channel_list(radio_channel_app)
    radio_app.add_typer(radio_channel_app)
    app.add_typer(radio_app)
    # plan sub-app (public)
    plan_app = typer.Typer(
        name='plan',
        help='Create and list iteration plans.',
        **kwargs,
    )
    cmd.plan_init(plan_app)
    cmd.plan_list(plan_app)
    app.add_typer(plan_app)
    # db sub-app (private)
    db_app = typer.Typer(
        name='db',
        help='Manage the central database.',
        **kwargs,
    )
    cmd.db_query(db_app)
    app.add_typer(db_app, hidden=True)
    # config sub-app (private)
    config_app = typer.Typer(
        name='config',
        help='Manage node configuration.',
        **kwargs,
    )
    cmd.config_get(config_app)
    cmd.config_set(config_app)
    app.add_typer(config_app, hidden=True)
    # event sub-app (private)
    event_app = typer.Typer(
        name='event',
        help='Track lifecycle events.',
        **kwargs,
    )
    cmd.event_start(event_app)
    cmd.event_end(event_app)
    cmd.event_list(event_app)
    app.add_typer(event_app, hidden=True)
    # run app -- and make every failure unmistakable: errors already ride
    # stderr with a non-zero exit, but a decorative parse-error frame read
    # through `tail -1` is indistinguishable from a success frame (the
    # phantom-send class: a night of failed sends that looked delivered), so
    # the LAST line of every failed command names the failure and exit code
    try:
        app()
    except SystemExit as exit_:
        code = exit_.code
        if isinstance(code, int) and code != 0:
            line = f'FAILED (exit {code})'
            if sys.stderr.isatty():
                line = f'{_FAIL}{line}{_RESET}'
            typer.echo(line, err=True)
        raise


if __name__ == '__main__':
    cli()
