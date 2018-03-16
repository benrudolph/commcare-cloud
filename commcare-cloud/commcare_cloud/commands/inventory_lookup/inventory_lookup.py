from __future__ import print_function
import os
import sys

from commcare_cloud.cli_utils import print_command
from commcare_cloud.commands.command_base import CommandBase
from commcare_cloud.environment.main import get_environment
from .getinventory import get_server_address, get_monolith_address
from six.moves import shlex_quote


class Lookup(CommandBase):
    command = 'lookup'
    help = "Lookup remote hostname or IP address"

    def make_parser(self):
        self.parser.add_argument("server", nargs="?",
            help="Server name/group: postgresql, proxy, webworkers, ... The server "
                 "name/group may be prefixed with 'username@' to login as a specific "
                 "user and may be terminated with ':<n>' to choose one of "
                 "multiple servers if there is more than one in the group. "
                 "For example: webworkers:0 will pick the first webworker. May also"
                 "be ommitted for environments with only a single server.")

    def lookup_server_address(self, args):
        def exit(message):
            self.parser.error("\n" + message)
        if not args.server:
            return get_monolith_address(args.environment, exit)
        return get_server_address(args.environment, args.server, exit)

    def run(self, args, unknown_args):
        if unknown_args:
            sys.stderr.write(
                "Ignoring extra argument(s): {}\n".format(unknown_args)
            )
        print_command(self.lookup_server_address(args))


class _Ssh(Lookup):

    def run(self, args, ssh_args):
        address = self.lookup_server_address(args)
        if ':' in address:
            address, port = address.split(':')
            ssh_args = ['-p', port] + ssh_args
        cmd_parts = [self.command, address] + ssh_args
        cmd = ' '.join(shlex_quote(arg) for arg in cmd_parts)
        print_command(cmd)
        os.execvp(self.command, cmd_parts)


class Ssh(_Ssh):
    command = 'ssh'
    help = "Connect to a remote host with ssh"

    def run(self, args, ssh_args):
        if args.server == 'control' and '-A' not in ssh_args:
            # Always include ssh agent forwarding on control machine
            ssh_args = ['-A'] + ssh_args
        super(Ssh, self).run(args, ssh_args)


class Mosh(_Ssh):
    command = 'mosh'
    help = "Connect to a remote host with mosh"

    def run(self, args, ssh_args):
        if args.server == 'control' or '-A' in ssh_args:
            print("! mosh does not support ssh agent forwarding, using ssh instead.",
                  file=sys.stderr)
            Ssh(self.parser).run(args, ssh_args)
        super(Mosh, self).run(args, ssh_args)


class DjangoManage(CommandBase):
    command = 'django-manage'
    help = ("Run a django management command. "
            "`commcare-cloud <env> django-manage ...` "
            "runs `./manage.py ...` on the first webworker of <env>. "
            "Omit <command> to see a full list of possible commands.")

    def make_parser(self):
        pass

    def run(self, args, manage_args):
        environment = get_environment(args.environment)
        public_vars = environment.public_vars
        # the default 'cchq' is redundant with ansible/group_vars/all.yml
        cchq_user = public_vars.get('cchq_user', 'cchq')
        deploy_env = environment.meta_config.deploy_env
        # the paths here are redundant with ansible/group_vars/all.yml
        code_current = '/home/{cchq_user}/www/{deploy_env}/current'.format(
            cchq_user=cchq_user, deploy_env=deploy_env)
        remote_command = (
            'sudo -u {cchq_user} {code_current}/python_env/bin/python {code_current}/manage.py {args}'
            .format(cchq_user=cchq_user, code_current=code_current, args=' '.join(shlex_quote(arg) for arg in manage_args))
        )
        args.server = 'webworkers:0'
        Ssh(self.parser).run(args, [remote_command])
