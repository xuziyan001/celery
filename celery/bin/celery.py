# -*- coding: utf-8 -*-
"""

The :program:`celery` umbrella command.

.. program:: celery

"""
from __future__ import absolute_import, print_function

import anyjson
import heapq
import sys
import warnings

from importlib import import_module
from itertools import imap
from pprint import pformat

from celery.platforms import EX_OK, EX_FAILURE, EX_UNAVAILABLE, EX_USAGE
from celery.utils import term
from celery.utils import text
from celery.utils.functional import memoize
from celery.utils.imports import symbol_by_name
from celery.utils.timeutils import maybe_iso8601

from celery.bin.base import Command as BaseCommand, Option

HELP = """
---- -- - - ---- Commands- -------------- --- ------------

{commands}
---- -- - - --------- -- - -------------- --- ------------

Type '{prog_name} <command> --help' for help using a specific command.
"""

MIGRATE_PROGRESS_FMT = """\
Migrating task {state.count}/{state.strtotal}: \
{body[task]}[{body[id]}]\
"""

commands = {}

command_classes = [
    ('Main', ['worker', 'events', 'beat', 'shell', 'multi', 'amqp'], 'green'),
    ('Remote Control', ['status', 'inspect', 'control'], 'blue'),
    ('Utils', ['purge', 'list', 'migrate', 'call', 'result', 'report'], None),
]


@memoize()
def _get_extension_classes():
    extensions = []
    command_classes.append(('Extensions', extensions, 'magenta'))
    return extensions


class Error(Exception):

    def __init__(self, reason, status=EX_FAILURE):
        self.reason = reason
        self.status = status
        super(Error, self).__init__(reason, status)

    def __str__(self):
        return self.reason


def command(*args, **kwargs):

    def _register(fun):
        commands[kwargs.get('name') or fun.__name__] = fun
        return fun

    return _register(args[0]) if args else _register


def load_extension_commands(namespace='celery.commands'):
    try:
        from pkg_resources import iter_entry_points
    except ImportError:
        return

    for ep in iter_entry_points(namespace):
        sym = ':'.join([ep.module_name, ep.attrs[0]])
        try:
            cls = symbol_by_name(sym)
        except (ImportError, SyntaxError) as exc:
            warnings.warn(
                'Cannot load extension {0!r}: {1!r}'.format(sym, exc))
        else:
            heapq.heappush(_get_extension_classes(), ep.name)
            command(cls, name=ep.name)


class Command(BaseCommand):
    help = ''
    args = ''
    prog_name = 'celery'
    show_body = True
    show_reply = True

    option_list = (
        Option('--quiet', '-q', action='store_true'),
        Option('--no-color', '-C', action='store_true'),
    )

    def __init__(self, app=None, no_color=False, stdout=sys.stdout,
            stderr=sys.stderr, show_reply=True):
        super(Command, self).__init__(app=app)
        self.colored = term.colored(enabled=not no_color)
        self.stdout = stdout
        self.stderr = stderr
        self.quiet = False
        if show_reply is not None:
            self.show_reply = show_reply

    def __call__(self, *args, **kwargs):
        try:
            ret = self.run(*args, **kwargs)
        except Error as exc:
            self.error(self.colored.red('Error: {0!r}'.format(exc)))
            return exc.status

        return ret if ret is not None else EX_OK

    def show_help(self, command):
        self.run_from_argv(self.prog_name, [command, '--help'])
        return EX_USAGE

    def error(self, s):
        self.out(s, fh=self.stderr)

    def out(self, s, fh=None):
        print(s, file=fh or self.stdout)

    def run_from_argv(self, prog_name, argv):
        self.prog_name = prog_name
        self.command = argv[0]
        self.arglist = argv[1:]
        self.parser = self.create_parser(self.prog_name, self.command)
        options, args = self.prepare_args(
                *self.parser.parse_args(self.arglist))
        self.colored = term.colored(enabled=not options['no_color'])
        self.quiet = options.get('quiet', False)
        self.show_body = options.get('show_body', True)
        return self(*args, **options)

    def usage(self, command):
        return '%%prog {0} [options] {self.args}'.format(command, self=self)

    def prettify_list(self, n):
        c = self.colored
        if not n:
            return '- empty -'
        return '\n'.join(str(c.reset(c.white('*'), ' {0}'.format(item)))
                            for item in n)

    def prettify_dict_ok_error(self, n):
        c = self.colored
        try:
            return (c.green('OK'),
                    text.indent(self.prettify(n['ok'])[1], 4))
        except KeyError:
            pass
        return (c.red('ERROR'),
                text.indent(self.prettify(n['error'])[1], 4))

    def say_remote_command_reply(self, replies):
        c = self.colored
        node = iter(replies).next()  # <-- take first.
        reply = replies[node]
        status, preply = self.prettify(reply)
        self.say_chat('->', c.cyan(node, ': ') + status,
                      text.indent(preply, 4) if self.show_reply else '')

    def prettify(self, n):
        OK = str(self.colored.green('OK'))
        if isinstance(n, list):
            return OK, self.prettify_list(n)
        if isinstance(n, dict):
            if 'ok' in n or 'error' in n:
                return self.prettify_dict_ok_error(n)
        if isinstance(n, basestring):
            return OK, unicode(n)
        return OK, pformat(n)

    def say_chat(self, direction, title, body=''):
        c = self.colored
        if direction == '<-' and self.quiet:
            return
        dirstr = not self.quiet and c.bold(c.white(direction), ' ') or ''
        self.out(c.reset(dirstr, title))
        if body and self.show_body:
            self.out(body)

    @property
    def description(self):
        return self.__doc__


class Delegate(Command):

    def __init__(self, *args, **kwargs):
        super(Delegate, self).__init__(*args, **kwargs)

        self.target = symbol_by_name(self.Command)(app=self.app)
        self.args = self.target.args

    def get_options(self):
        return self.option_list + self.target.get_options()

    def create_parser(self, prog_name, command):
        parser = super(Delegate, self).create_parser(prog_name, command)
        return self.target.prepare_parser(parser)

    def run(self, *args, **kwargs):
        self.target.check_args(args)
        return self.target.run(*args, **kwargs)


@command
class multi(Command):
    """Start multiple worker instances."""

    def get_options(self):
        return ()

    def run_from_argv(self, prog_name, argv):
        from celery.bin.celeryd_multi import MultiTool
        return MultiTool().execute_from_commandline(argv, prog_name)


@command
class worker(Delegate):
    """Start worker instance.

    Examples::

        celery worker --app=proj -l info
        celery worker -A proj -l info -Q hipri,lopri

        celery worker -A proj --concurrency=4
        celery worker -A proj --concurrency=1000 -P eventlet

        celery worker --autoscale=10,0
    """
    Command = 'celery.bin.celeryd:WorkerCommand'

    def run_from_argv(self, prog_name, argv):
        self.target.maybe_detach(argv)
        super(worker, self).run_from_argv(prog_name, argv)


@command
class events(Delegate):
    """Event-stream utilities.

    Commands::

        celery events --app=proj
            start graphical monitor (requires curses)
        celery events -d --app=proj
            dump events to screen.
        celery events -b amqp://
        celery events -C <camera> [options]
            run snapshot camera.

    Examples::

        celery events
        celery events -d
        celery events -C mod.attr -F 1.0 --detach --maxrate=100/m -l info
    """
    Command = 'celery.bin.celeryev:EvCommand'


@command
class beat(Delegate):
    """Start the celerybeat periodic task scheduler.

    Examples::

        celery beat -l info
        celery beat -s /var/run/celerybeat/schedule --detach
        celery beat -S djcelery.schedulers.DatabaseScheduler

    """
    Command = 'celery.bin.celerybeat:BeatCommand'


@command
class amqp(Delegate):
    """AMQP Administration Shell.

    Also works for non-amqp transports.

    Examples::

        celery amqp
            start shell mode
        celery amqp help
            show list of commands

        celery amqp exchange.delete name
        celery amqp queue.delete queue
        celery amqp queue.delete queue yes yes

    """
    Command = 'celery.bin.camqadm:AMQPAdminCommand'


@command(name='list')
class list_(Command):
    """Get info from broker.

    Examples::

        celery list bindings

    NOTE: For RabbitMQ the management plugin is required.
    """
    args = '[bindings]'

    def list_bindings(self, management):
        try:
            bindings = management.get_bindings()
        except NotImplementedError:
            raise Error('Your transport cannot list bindings.')

        fmt = lambda q, e, r: self.out('{0:<28} {1:<28} {2}'.format(q, e, r))
        fmt('Queue', 'Exchange', 'Routing Key')
        fmt('-' * 16, '-' * 16, '-' * 16)
        for b in bindings:
            fmt(b['destination'], b['source'], b['routing_key'])

    def run(self, what=None, *_, **kw):
        topics = {'bindings': self.list_bindings}
        available = ', '.join(topics)
        if not what:
            raise Error('You must specify one of {0}'.format(available))
        if what not in topics:
            raise Error('unknown topic {0!r} (choose one of: {1})'.format(
                            what, available))
        with self.app.connection() as conn:
            self.app.amqp.TaskConsumer(conn).declare()
            topics[what](conn.manager)


@command
class call(Command):
    """Call a task by name.

    Examples::

        celery call tasks.add --args='[2, 2]'
        celery call tasks.add --args='[2, 2]' --countdown=10
    """
    args = '<task_name>'
    option_list = Command.option_list + (
            Option('--args', '-a', help='positional arguments (json).'),
            Option('--kwargs', '-k', help='keyword arguments (json).'),
            Option('--eta', help='scheduled time (ISO-8601).'),
            Option('--countdown', type='float',
                help='eta in seconds from now (float/int).'),
            Option('--expires', help='expiry time (ISO-8601/float/int).'),
            Option('--serializer', default='json', help='defaults to json.'),
            Option('--queue', help='custom queue name.'),
            Option('--exchange', help='custom exchange name.'),
            Option('--routing-key', help='custom routing key.'),
    )

    def run(self, name, *_, **kw):
        # Positional args.
        args = kw.get('args') or ()
        if isinstance(args, basestring):
            args = anyjson.loads(args)

        # Keyword args.
        kwargs = kw.get('kwargs') or {}
        if isinstance(kwargs, basestring):
            kwargs = anyjson.loads(kwargs)

        # Expires can be int/float.
        expires = kw.get('expires') or None
        try:
            expires = float(expires)
        except (TypeError, ValueError):
            # or a string describing an ISO 8601 datetime.
            try:
                expires = maybe_iso8601(expires)
            except (TypeError, ValueError):
                raise

        res = self.app.send_task(name, args=args, kwargs=kwargs,
                                 countdown=kw.get('countdown'),
                                 serializer=kw.get('serializer'),
                                 queue=kw.get('queue'),
                                 exchange=kw.get('exchange'),
                                 routing_key=kw.get('routing_key'),
                                 eta=maybe_iso8601(kw.get('eta')),
                                 expires=expires)
        self.out(res.id)


@command
class purge(Command):
    """Erase all messages from all known task queues.

    WARNING: There is no undo operation for this command.

    """
    fmt_purged = 'Purged {mnum} {messages} from {qnum} known task {queues}.'
    fmt_empty = 'No messages purged from {qnum} {queues}'

    def run(self, *args, **kwargs):
        queues = len(self.app.amqp.queues)
        messages = self.app.control.purge()
        fmt = self.fmt_purged if messages else self.fmt_empty
        self.out(fmt.format(
            mnum=messages, qnum=queues,
            messages=text.pluralize(messages, 'message'),
            queues=text.pluralize(queues, 'queue')))


@command
class result(Command):
    """Gives the return value for a given task id.

    Examples::

        celery result 8f511516-e2f5-4da4-9d2f-0fb83a86e500
        celery result 8f511516-e2f5-4da4-9d2f-0fb83a86e500 -t tasks.add
        celery result 8f511516-e2f5-4da4-9d2f-0fb83a86e500 --traceback

    """
    args = '<task_id>'
    option_list = Command.option_list + (
            Option('--task', '-t', help='name of task (if custom backend)'),
            Option('--traceback', action='store_true',
                   help='show traceback instead'),
    )

    def run(self, task_id, *args, **kwargs):
        result_cls = self.app.AsyncResult
        task = kwargs.get('task')
        traceback = kwargs.get('traceback', False)

        if task:
            result_cls = self.app.tasks[task].AsyncResult
        result = result_cls(task_id)
        if traceback:
            value = result.traceback
        else:
            value = result.get()
        self.out(self.prettify(value)[1])


class _RemoteControl(Command):
    name = None
    choices = None
    leaf = False
    option_list = Command.option_list + (
                Option('--timeout', '-t', type='float',
                    help='Timeout in seconds (float) waiting for reply'),
                Option('--destination', '-d',
                    help='Comma separated list of destination node names.'))

    @classmethod
    def get_command_info(self, command, indent=0, prefix='', color=None,
            help=False):
        if help:
            help = '|' + text.indent(self.choices[command][1], indent + 4)
        else:
            help = None
        try:
            # see if it uses args.
            meth = getattr(self, command)
            return text.join([
                '|' + text.indent('{0}{1} {2}'.format(prefix, color(command),
                                                meth.__doc__), indent), help,
            ])

        except AttributeError:
            return text.join([
                '|' + text.indent(prefix + str(color(command)), indent), help,
            ])

    @classmethod
    def list_commands(self, indent=0, prefix='', color=None, help=False):
        color = color if color else lambda x: x
        prefix = prefix + ' ' if prefix else ''
        return '\n'.join(self.get_command_info(c, indent, prefix, color, help)
                            for c in sorted(self.choices))

    @property
    def epilog(self):
        return '\n'.join([
            '[Commands]',
            self.list_commands(indent=4, help=True)
        ])

    def usage(self, command):
        return '%%prog {0} [options] {1} <command> [arg1 .. argN]'.format(
                command, self.args)

    def call(self, *args, **kwargs):
        raise NotImplementedError('get_obj')

    def run(self, *args, **kwargs):
        if not args:
            raise Error('Missing {0.name} method. See --help'.format(self))
        return self.do_call_method(args, **kwargs)

    def do_call_method(self, args, **kwargs):
        method = args[0]
        if method == 'help':
            raise Error("Did you mean '{0.name} --help'?".format(self))
        if method not in self.choices:
            raise Error('Unknown {0.name} method {1}'.format(self, method))

        destination = kwargs.get('destination')
        timeout = kwargs.get('timeout') or self.choices[method][0]
        if destination and isinstance(destination, basestring):
            destination = list(imap(str.strip, destination.split(',')))

        try:
            handler = getattr(self, method)
        except AttributeError:
            handler = self.call

        replies = handler(method, *args[1:], timeout=timeout,
                          destination=destination,
                          callback=self.say_remote_command_reply)
        if not replies:
            raise Error('No nodes replied within time constraint.',
                        status=EX_UNAVAILABLE)
        return replies

    def say(self, direction, title, body=''):
        c = self.colored
        if direction == '<-' and self.quiet:
            return
        dirstr = not self.quiet and c.bold(c.white(direction), ' ') or ''
        self.out(c.reset(dirstr, title))
        if body and self.show_body:
            self.out(body)


@command
class inspect(_RemoteControl):
    """Inspect the worker at runtime.

    Availability: RabbitMQ (amqp), Redis, and MongoDB transports.

    Examples::

        celery inspect active --timeout=5
        celery inspect scheduled -d worker1.example.com
        celery inspect revoked -d w1.e.com,w2.e.com

    """
    name = 'inspect'
    choices = {
        'active': (1.0, 'dump active tasks (being processed)'),
        'active_queues': (1.0, 'dump queues being consumed from'),
        'scheduled': (1.0, 'dump scheduled tasks (eta/countdown/retry)'),
        'reserved': (1.0, 'dump reserved tasks (waiting to be processed)'),
        'stats': (1.0, 'dump worker statistics'),
        'revoked': (1.0, 'dump of revoked task ids'),
        'registered': (1.0, 'dump of registered tasks'),
        'ping': (0.2, 'ping worker(s)'),
        'report': (1.0, 'get bugreport info')
    }

    def call(self, method, *args, **options):
        i = self.app.control.inspect(**options)
        return getattr(i, method)(*args)


@command
class control(_RemoteControl):
    """Workers remote control.

    Availability: RabbitMQ (amqp), Redis, and MongoDB transports.

    Examples::

        celery control enable_events --timeout=5
        celery control -d worker1.example.com enable_events
        celery control -d w1.e.com,w2.e.com enable_events

        celery control -d w1.e.com add_consumer queue_name
        celery control -d w1.e.com cancel_consumer queue_name

        celery control -d w1.e.com add_consumer queue exchange direct rkey

    """
    name = 'control'
    choices = {
        'enable_events': (1.0, 'tell worker(s) to enable events'),
        'disable_events': (1.0, 'tell worker(s) to disable events'),
        'add_consumer': (1.0, 'tell worker(s) to start consuming a queue'),
        'cancel_consumer': (1.0, 'tell worker(s) to stop consuming a queue'),
        'rate_limit': (1.0,
            'tell worker(s) to modify the rate limit for a task type'),
        'time_limit': (1.0,
            'tell worker(s) to modify the time limit for a task type.'),
        'autoscale': (1.0, 'change autoscale settings'),
        'pool_grow': (1.0, 'start more pool processes'),
        'pool_shrink': (1.0, 'use less pool processes'),
    }

    def call(self, method, *args, **options):
        return getattr(self.app.control, method)(*args, retry=True, **options)

    def pool_grow(self, method, n=1, **kwargs):
        """[N=1]"""
        return self.call(method, n, **kwargs)

    def pool_shrink(self, method, n=1, **kwargs):
        """[N=1]"""
        return self.call(method, n, **kwargs)

    def autoscale(self, method, max=None, min=None, **kwargs):
        """[max] [min]"""
        return self.call(method, max, min, **kwargs)

    def rate_limit(self, method, task_name, rate_limit, **kwargs):
        """<task_name> <rate_limit> (e.g. 5/s | 5/m | 5/h)>"""
        return self.call(method, task_name, rate_limit, reply=True, **kwargs)

    def time_limit(self, method, task_name, soft, hard=None, **kwargs):
        """<task_name> <soft_secs> [hard_secs]"""
        return self.call(method, task_name, soft, hard, reply=True, **kwargs)

    def add_consumer(self, method, queue, exchange=None,
            exchange_type='direct', routing_key=None, **kwargs):
        """<queue> [exchange [type [routing_key]]]"""
        return self.call(method, queue, exchange,
                         exchange_type, routing_key, reply=True, **kwargs)

    def cancel_consumer(self, method, queue, **kwargs):
        """<queue>"""
        return self.call(method, queue, reply=True, **kwargs)


@command
class status(Command):
    """Show list of workers that are online."""
    option_list = inspect.option_list

    def run(self, *args, **kwargs):
        replies = inspect(app=self.app,
                          no_color=kwargs.get('no_color', False),
                          stdout=self.stdout, stderr=self.stderr,
                          show_reply=False) \
                    .run('ping', **dict(kwargs, quiet=True, show_body=False))
        if not replies:
            raise Error('No nodes replied within time constraint',
                        status=EX_UNAVAILABLE)
        nodecount = len(replies)
        if not kwargs.get('quiet', False):
            self.out('\n{0} {1} online.'.format(
                nodecount, text.pluralize(nodecount, 'node')))


@command
class migrate(Command):
    """Migrate tasks from one broker to another.

    Examples::

        celery migrate redis://localhost amqp://guest@localhost//
        celery migrate django:// redis://localhost

    NOTE: This command is experimental, make sure you have
          a backup of the tasks before you continue.
    """
    args = '<source_url> <dest_url>'
    option_list = Command.option_list + (
            Option('--limit', '-n', type='int',
                    help='Number of tasks to consume (int)'),
            Option('--timeout', '-t', type='float', default=1.0,
                    help='Timeout in seconds (float) waiting for tasks'),
            Option('--ack-messages', '-a', action='store_true',
                    help='Ack messages from source broker.'),
            Option('--tasks', '-T',
                    help='List of task names to filter on.'),
            Option('--queues', '-Q',
                    help='List of queues to migrate.'),
            Option('--forever', '-F', action='store_true',
                    help='Continually migrate tasks until killed.'),
    )
    progress_fmt = MIGRATE_PROGRESS_FMT

    def on_migrate_task(self, state, body, message):
        self.out(self.progress_fmt.format(state=state, body=body))

    def run(self, *args, **kwargs):
        if len(args) != 2:
            return self.show_help('migrate')
        from kombu import Connection
        from celery.contrib.migrate import migrate_tasks

        migrate_tasks(Connection(args[0]),
                      Connection(args[1]),
                      callback=self.on_migrate_task,
                      **kwargs)


@command
class shell(Command):  # pragma: no cover
    """Start shell session with convenient access to celery symbols.

    The following symbols will be added to the main globals:

        - celery:  the current application.
        - chord, group, chain, chunks,
          xmap, xstarmap subtask, Task
        - all registered tasks.

    Example Session:

    .. code-block:: bash

        $ celery shell

        >>> celery
        <Celery default:0x1012d9fd0>
        >>> add
        <@task: tasks.add>
        >>> add.delay(2, 2)
        <AsyncResult: 537b48c7-d6d3-427a-a24a-d1b4414035be>
    """
    option_list = Command.option_list + (
                Option('--ipython', '-I',
                    action='store_true', dest='force_ipython',
                    help='force iPython.'),
                Option('--bpython', '-B',
                    action='store_true', dest='force_bpython',
                    help='force bpython.'),
                Option('--python', '-P',
                    action='store_true', dest='force_python',
                    help='force default Python shell.'),
                Option('--without-tasks', '-T', action='store_true',
                    help="don't add tasks to locals."),
                Option('--eventlet', action='store_true',
                    help='use eventlet.'),
                Option('--gevent', action='store_true', help='use gevent.'),
    )

    def run(self, force_ipython=False, force_bpython=False,
            force_python=False, without_tasks=False, eventlet=False,
            gevent=False, **kwargs):
        if eventlet:
            import_module('celery.concurrency.eventlet')
        if gevent:
            import_module('celery.concurrency.gevent')
        import celery
        import celery.task.base
        self.app.loader.import_default_modules()
        self.locals = {'celery': self.app,
                       'Task': celery.Task,
                       'chord': celery.chord,
                       'group': celery.group,
                       'chain': celery.chain,
                       'chunks': celery.chunks,
                       'xmap': celery.xmap,
                       'xstarmap': celery.xstarmap,
                       'subtask': celery.subtask}

        if not without_tasks:
            self.locals.update(dict((task.__name__, task)
                                for task in self.app.tasks.itervalues()
                                    if not task.name.startswith('celery.')))

        if force_python:
            return self.invoke_fallback_shell()
        elif force_bpython:
            return self.invoke_bpython_shell()
        elif force_ipython:
            return self.invoke_ipython_shell()
        return self.invoke_default_shell()

    def invoke_default_shell(self):
        try:
            import IPython  # noqa
        except ImportError:
            try:
                import bpython  # noqa
            except ImportError:
                return self.invoke_fallback_shell()
            else:
                return self.invoke_bpython_shell()
        else:
            return self.invoke_ipython_shell()

    def invoke_fallback_shell(self):
        import code
        try:
            import readline
        except ImportError:
            pass
        else:
            import rlcompleter
            readline.set_completer(
                    rlcompleter.Completer(self.locals).complete)
            readline.parse_and_bind('tab:complete')
        code.interact(local=self.locals)

    def invoke_ipython_shell(self):
        try:
            from IPython.frontend.terminal import embed
            embed.TerminalInteractiveShell(user_ns=self.locals).mainloop()
        except ImportError:  # ipython < 0.11
            from IPython.Shell import IPShell
            IPShell(argv=[], user_ns=self.locals).mainloop()

    def invoke_bpython_shell(self):
        import bpython
        bpython.embed(self.locals)


@command
class help(Command):
    """Show help screen and exit."""

    def usage(self, command):
        return '%%prog <command> [options] {0.args}'.format(self)

    def run(self, *args, **kwargs):
        self.parser.print_help()
        self.out(HELP.format(prog_name=self.prog_name,
                             commands=CeleryCommand.list_commands()))

        return EX_USAGE


@command
class report(Command):
    """Shows information useful to include in bugreports."""

    def run(self, *args, **kwargs):
        self.out(self.app.bugreport())
        return EX_OK


class CeleryCommand(BaseCommand):
    commands = commands
    enable_config_from_cmdline = True
    prog_name = 'celery'

    def execute(self, command, argv=None):
        try:
            cls = self.commands[command]
        except KeyError:
            cls, argv = self.commands['help'], ['help']
        cls = self.commands.get(command) or self.commands['help']
        try:
            return cls(app=self.app).run_from_argv(self.prog_name, argv)
        except Error:
            return self.execute('help', argv)

    def remove_options_at_beginning(self, argv, index=0):
        if argv:
            while index < len(argv):
                value = argv[index]
                if value.startswith('--'):
                    pass
                elif value.startswith('-'):
                    index += 1
                else:
                    return argv[index:]
                index += 1
        return []

    def handle_argv(self, prog_name, argv):
        self.prog_name = prog_name
        argv = self.remove_options_at_beginning(argv)
        _, argv = self.prepare_args(None, argv)
        try:
            command = argv[0]
        except IndexError:
            command, argv = 'help', ['help']
        return self.execute(command, argv)

    def execute_from_commandline(self, argv=None):
        try:
            sys.exit(determine_exit_status(
                super(CeleryCommand, self).execute_from_commandline(argv)))
        except KeyboardInterrupt:
            sys.exit(EX_FAILURE)

    @classmethod
    def get_command_info(self, command, indent=0, color=None):
        colored = term.colored().names[color] if color else lambda x: x
        obj = self.commands[command]
        cmd = 'celery {0}'.format(colored(command))
        if obj.leaf:
            return '|' + text.indent(cmd, indent)
        return text.join([
            ' ',
            '|' + text.indent('{0} --help'.format(cmd), indent),
            obj.list_commands(indent, 'celery {0}'.format(command), colored),
        ])

    @classmethod
    def list_commands(self, indent=0):
        white = term.colored().white
        ret = []
        for cls, commands, color in command_classes:
            ret.extend([
                text.indent('+ {0}: '.format(white(cls)), indent),
                '\n'.join(self.get_command_info(command, indent + 4, color)
                            for command in commands),
                ''
            ])
        return '\n'.join(ret).strip()

    def with_pool_option(self, argv):
        if len(argv) > 1 and argv[1] == 'worker':
            # this command supports custom pools
            # that may have to be loaded as early as possible.
            return (['-P'], ['--pool'])

    def on_concurrency_setup(self):
        load_extension_commands()


def determine_exit_status(ret):
    if isinstance(ret, int):
        return ret
    return EX_OK if ret else EX_FAILURE


def main(argv=None):
    # Fix for setuptools generated scripts, so that it will
    # work with multiprocessing fork emulation.
    # (see multiprocessing.forking.get_preparation_data())
    try:
        if __name__ != '__main__':  # pragma: no cover
            sys.modules['__main__'] = sys.modules[__name__]
        cmd = CeleryCommand()
        cmd.maybe_patch_concurrency()
        from billiard import freeze_support
        freeze_support()
        cmd.execute_from_commandline(argv)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':          # pragma: no cover
    main()