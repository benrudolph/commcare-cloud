from __future__ import print_function
import argparse
import json
import os
import random
import string
import subprocess
import sys
import shutil

import re

import jinja2
import yaml
import jsonobject
from commcare_cloud.environment.paths import get_inventory_filepath, \
    get_public_vars_filepath, get_vault_vars_filepath, ENVIRONMENTS_DIR, REPO_BASE

VARS_DIR = os.path.join(REPO_BASE, 'ansible', 'vars')


class StrictJsonObject(jsonobject.JsonObject):
    _allow_dynamic_properties = False


# Spec


class Spec(StrictJsonObject):
    aws_config = jsonobject.ObjectProperty(lambda: AwsConfig)
    allocations = jsonobject.DictProperty(lambda: Allocation)

    @classmethod
    def wrap(cls, obj):
        allocations = {
            key: {'count': value} if isinstance(value, int) else value
            for key, value in obj.get('allocations', {}).items()
        }
        obj['allocations'] = allocations
        return super(Spec, cls).wrap(obj)


class AwsConfig(StrictJsonObject):
    pem = jsonobject.StringProperty()
    ami = jsonobject.StringProperty()
    type = jsonobject.StringProperty()
    key_name = jsonobject.StringProperty()
    security_group_id = jsonobject.StringProperty()
    subnet = jsonobject.StringProperty()


class Allocation(StrictJsonObject):
    count = jsonobject.IntegerProperty()
    from_ = jsonobject.StringProperty(name='from')


# Inventory

class Inventory(StrictJsonObject):
    all_hosts = jsonobject.ListProperty(lambda: Host)
    all_groups = jsonobject.DictProperty(lambda: Group)


class Host(StrictJsonObject):
    name = jsonobject.StringProperty()
    public_ip = jsonobject.StringProperty()
    private_ip = jsonobject.StringProperty()
    vars = jsonobject.DictProperty()


class Group(StrictJsonObject):
    name = jsonobject.StringProperty()
    host_names = jsonobject.ListProperty(unicode)
    vars = jsonobject.DictProperty()


def provision_machines(spec, env=None):
    if env is None:
        env = u'hq-{}'.format(
            ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(7))
        )
    inventory = bootstrap_inventory(spec, env)
    instance_ids = ask_aws_for_instances(env, spec.aws_config, len(inventory.all_hosts))

    while True:
        instance_ip_addresses = poll_for_aws_state(env, instance_ids)
        if instance_ip_addresses:
            break

    hosts_by_name = {}

    for host, (public_ip, private_ip) in zip(inventory.all_hosts, instance_ip_addresses.values()):
        host.public_ip = public_ip
        host.private_ip = private_ip
        hosts_by_name[host.name] = host

    for i, host_name in enumerate(inventory.all_groups['kafka'].host_names):
        hosts_by_name[host_name].vars['kafka_broker_id'] = i
        hosts_by_name[host_name].vars['swap_size'] = '2G'

    for host_name in inventory.all_groups['elasticsearch'].host_names:
        hosts_by_name[host_name].vars['elasticsearch_node_name'] = host_name

    save_inventory(env, inventory)
    copy_default_vars(env, spec.aws_config)


def alphanumeric_sort_key(key):
    """
    Sort the given iterable in the way that humans expect.
    Thanks to http://stackoverflow.com/a/2669120/240553
    """
    import re
    convert = lambda text: int(text) if text.isdigit() else text
    return [convert(c) for c in re.split('([0-9]+)', key)]


def bootstrap_inventory(spec, env):
    incomplete = dict(spec.allocations.items())

    inventory = Inventory()

    while incomplete:
        for role, allocation in incomplete.items():
            if allocation.from_:
                if allocation.from_ not in spec.allocations:
                    raise KeyError('You specified an unknown group in the from field of {}: {}'
                                   .format(role, allocation.from_))
                if allocation.from_ in incomplete:
                    continue
                # This is kind of hacky because it does a string sort
                # on strings containing integers.
                # Once we have more than 10 it'll start sorting wrong
                host_names = sorted(
                    inventory.all_groups[allocation.from_].host_names,
                    key=alphanumeric_sort_key,
                )[:allocation.count]
                inventory.all_groups[role] = Group(
                    name=role,
                    host_names=[host_name for host_name in host_names],
                    vars={},
                )
            else:
                new_host_names = set()
                for i in range(allocation.count):
                    host_name = '{env}-{group}-{i}'.format(env=env, group=role, i=i)
                    new_host_names.add(host_name)
                    inventory.all_hosts.append(
                        Host(name=host_name, public_ip=None, private_ip=None, vars={}))
                inventory.all_groups[role] = Group(
                    name=role,
                    host_names=[host_name for host_name in new_host_names],
                    vars={},
                )

            del incomplete[role]
    return inventory


def ask_aws_for_instances(env, aws_config, count):
    cache_file = '{env}-aws-new-instances.json'.format(env=env)
    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            aws_response = f.read()
    else:
        cmd_parts = [
            'aws', 'ec2', 'run-instances',
            '--image-id', aws_config.ami,
            '--count', unicode(int(count)),
            '--instance-type', aws_config.type,
            '--key-name', aws_config.key_name,
            '--security-group-ids', aws_config.security_group_id,
            '--subnet-id', aws_config.subnet,
            '--tag-specifications', 'ResourceType=instance,Tags=[{Key=env,Value=' + env + '}]',
        ]
        aws_response = subprocess.check_output(cmd_parts)
        with open(cache_file, 'w') as f:
            f.write(aws_response)
    aws_response = json.loads(aws_response)
    return {instance['InstanceId'] for instance in aws_response["Instances"]}


def print_describe_instances(describe_instances):
    for reservation in describe_instances['Reservations']:
        for instance in reservation['Instances']:
            print("{InstanceId}\t{InstanceType}\t{ImageId}\t{State[Name]}\t{PublicIpAddress}\t{PrivateIpAddress}".format(**instance),
                  file=sys.stderr)


def raw_describe_instances(env):
    cmd_parts = [
        'aws', 'ec2', 'describe-instances', '--filters',
        'Name=instance-state-code,Values=16',
        'Name=tag:env,Values=' + env
    ]
    return json.loads(subprocess.check_output(cmd_parts))


def get_hosts_from_describe_instances(describe_instances):
    hosts = []
    for reservation in describe_instances['Reservations']:
        for instance in reservation['Instances']:
            hosts.append(
                Host(public_ip=instance['PublicIpAddress'],
                     private_ip=instance['PrivateIpAddress']))
    return hosts


def get_inventory_from_file(env):
    inventory = Inventory()
    state = None
    with open(get_inventory_filepath(env)) as f:
        for line in f.readlines():
            group_line_match = re.match(r'^\[\s*(.*)\s*\]\s*$', line)
            if re.match(r'^\s*$', line):
                continue
            if group_line_match:
                section_name = group_line_match.groups()[0]
                if section_name.endswith(':children'):
                    state = 'parsing-group'
                    current_group_name = section_name[:-len(':children')]
                    current_group = Group(name=current_group_name)
                    inventory.all_groups[current_group_name] = current_group
                else:
                    state = 'parsing-host'
                    current_host_name = section_name
            else:
                if state == 'parsing-host':
                    line_groups = list(re.split(r'\s+', line.strip()))
                    private_ip, variables = line_groups[0], line_groups[1:]
                    variables = dict(var.strip().split('=') for var in variables)
                    public_ip = variables.pop('ansible_host')
                    host = Host(name=current_host_name, private_ip=private_ip, public_ip=public_ip,
                                vars=variables)
                    inventory.all_hosts.append(host)
                elif state == 'parsing-group':
                    host_name = line.strip()
                    current_group.host_names.append(host_name)
                else:
                    raise ValueError('Encountered items outside a section')
    return inventory


def update_inventory_public_ips(inventory, new_hosts):
    assert len(inventory.all_hosts) == len(new_hosts)
    assert {host.private_ip for host in inventory.all_hosts} == {host.private_ip for host in new_hosts}
    new_host_by_private_ip = {host.private_ip: host for host in new_hosts}
    for host in inventory.all_hosts:
        host.public_ip = new_host_by_private_ip[host.private_ip].public_ip


def poll_for_aws_state(env, instance_ids):
    describe_instances = raw_describe_instances(env)
    print_describe_instances(describe_instances)

    instances = [instance
                 for reservation in describe_instances['Reservations']
                 for instance in reservation['Instances']]
    unfinished_instances = instance_ids - {
        instance['InstanceId'] for instance in instances
        if instance.get('PublicIpAddress') and instance.get('PublicDnsName')
    }
    if not unfinished_instances:
        return {
            instance['InstanceId']: (instance['PublicIpAddress'], instance['PrivateIpAddress'])
            for instance in instances
            if instance['InstanceId'] in instance_ids
        }


def save_inventory(env, inventory):
    j2 = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.dirname(__file__)))
    template = j2.get_template('inventory.ini.j2')
    inventory_file_contents = template.render(inventory=inventory)
    inventory_file = get_inventory_filepath(env)
    if not os.path.exists(os.path.dirname(inventory_file)):
        os.makedirs(os.path.dirname(inventory_file))
    with open(inventory_file, 'w') as f:
        f.write(inventory_file_contents)
    print('inventory file saved to {}'.format(inventory_file),
          file=sys.stderr)


def copy_default_vars(env, aws_config):
    vars_dir = VARS_DIR
    template_dir = os.path.join(vars_dir, '.commcare-cloud-bootstrap')
    new_dir = ENVIRONMENTS_DIR
    vars_public = get_public_vars_filepath(env)
    vars_vault = get_vault_vars_filepath(env)
    if os.path.exists(template_dir) and not os.path.exists(vars_public):
        shutil.copyfile(os.path.join(template_dir, 'private.yml'), vars_vault)
        shutil.copyfile(os.path.join(template_dir, 'public.yml'), vars_public)
        with open(vars_public, 'a') as f:
            f.write('commcare_cloud_root_user: ubuntu\n')
            f.write('commcare_cloud_pem: {pem}\n'.format(pem=aws_config.pem))
            f.write('commcare_cloud_strict_host_key_checking: no\n')
            f.write('commcare_cloud_use_vault: no\n')
        print('template vars dir copied to {}'.format(new_dir),
              file=sys.stderr)


class Provision(object):
    command = 'provision'
    help = """Provision a new environment based on a spec yaml file. (See example_spec.yml.)"""

    @staticmethod
    def make_parser(parser):
        parser.add_argument('spec')
        parser.add_argument('--env')

    @staticmethod
    def run(args):
        with open(args.spec) as f:
            spec = yaml.load(f)

        spec = Spec.wrap(spec)
        provision_machines(spec, args.env)


class Show(object):
    command = 'show'
    help = """Show provisioned instances for a given env"""

    @staticmethod
    def make_parser(parser):
        parser.add_argument('env')

    @staticmethod
    def run(args):
        describe_instances = raw_describe_instances(args.env)
        print_describe_instances(describe_instances)


class Reip(object):
    command = 'reip'
    help = ("Rewrite the public IP addresses in the inventory for an env. "
            "Useful after reboot.")

    @staticmethod
    def make_parser(parser):
        parser.add_argument('env')

    @staticmethod
    def run(args):
        describe_instances = raw_describe_instances(args.env)
        new_hosts = get_hosts_from_describe_instances(describe_instances)
        inventory = get_inventory_from_file(args.env)
        update_inventory_public_ips(inventory, new_hosts)
        save_inventory(args.env, inventory)


STANDARD_ARGS = [
    Provision,
    Show,
    Reip,
]


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')

    for standard_arg in STANDARD_ARGS:
        standard_arg.make_parser(subparsers.add_parser(standard_arg.command, help=standard_arg.help))

    args = parser.parse_args()

    for standard_arg in STANDARD_ARGS:
        if args.command == standard_arg.command:
            standard_arg.run(args)
