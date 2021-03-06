import functools
import string
import sys
import time
import urllib.request
from collections import namedtuple
from datetime import datetime
import socket

# External modules
import boto3
import botocore
import click

# Flintrock modules
from .core import FlintrockCluster
from .core import provision_cluster
from .exceptions import (
    Error,
    ClusterNotFound,
    ClusterAlreadyExists,
    ClusterInvalidState,
    NothingToDo)
from .ssh import generate_ssh_key_pair


class NoDefaultVPC(Error):
    def __init__(self, *, region: str):
        super().__init__(
            "Flintrock could not find a default VPC in {r}. "
            "Please explicitly specify a VPC to work with in that region. "
            "Flintrock does not support managing EC2 clusters outside a VPC."
            .format(r=region)
        )
        self.region = region


class ConfigurationNotSupported(Error):
    def __init__(self, message):
        super().__init__(message)


def timeit(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = datetime.now().replace(microsecond=0)
        res = func(*args, **kwargs)
        end = datetime.now().replace(microsecond=0)
        print("{f} finished in {t}.".format(f=func.__name__, t=(end - start)))
        return res
    return wrapper


class EC2Cluster(FlintrockCluster):
    def __init__(
            self,
            region: str,
            vpc_id: str,
            master_instance: 'boto3.resources.factory.ec2.Instance',
            slave_instances: "List[boto3.resources.factory.ec2.Instance]",
            *args,
            **kwargs):
        super().__init__(*args, **kwargs)
        self.region = region
        self.vpc_id = vpc_id
        self.master_instance = master_instance
        self.slave_instances = slave_instances

    @property
    def instances(self):
        return [self.master_instance] + self.slave_instances

    @property
    def master_ip(self):
        if self.subnet_is_private:
            return self.master_instance.private_ip_address
        else:
            return self.master_instance.public_ip_address

    @property
    def master_host(self):
        if self.subnet_is_private:
            return self.master_instance.private_dns_name
        else:
            return self.master_instance.public_dns_name

    @property
    def slave_ips(self):
        if self.subnet_is_private:
            return [i.private_ip_address for i in self.slave_instances]
        else:
            return [i.public_ip_address for i in self.slave_instances]

    @property
    def slave_hosts(self):
        if self.subnet_is_private:
            return [i.private_dns_name for i in self.slave_instances]
        else:
            return [i.public_dns_name for i in self.slave_instances]

    @property
    def subnet_is_private(self):
        ec2 = boto3.resource(service_name='ec2', region_name=self.region)
        return not ec2.Subnet(self.master_instance.subnet_id).map_public_ip_on_launch

    @property
    def state(self):
        instance_states = set(
            instance.state['Name'] for instance in self.instances)
        if len(instance_states) == 1:
            return instance_states.pop()
        else:
            return 'inconsistent'

    def wait_for_state(self, state: str):
        """
        Wait for the cluster's instances to a reach a specific state.
        The state of any services installed on the cluster is a
        separate matter.

        This method updates the cluster's instance metadata and
        master and slave IP addresses and hostnames.
        """
        ec2 = boto3.resource(service_name='ec2', region_name=self.region)

        while any([i.state['Name'] != state for i in self.instances]):
            # Update metadata for all instances in one shot. We don't want
            # to make a call to AWS for each of potentially hundreds of
            # instances.
            instances = list(
                ec2.instances.filter(
                    InstanceIds=[i.id for i in self.instances]))
            (self.master_instance, self.slave_instances) = _get_cluster_master_slaves(instances)
            time.sleep(3)

    def destroy(self):
        self.destroy_check()
        super().destroy()
        ec2 = boto3.resource(service_name='ec2', region_name=self.region)

        # TODO: Centralize logic to get Flintrock base security group. (?)
        flintrock_base_group = list(
            ec2.security_groups.filter(
                Filters=[
                    {'Name': 'group-name', 'Values': ['flintrock']},
                    {'Name': 'vpc-id', 'Values': [self.vpc_id]},
                ]))[0]

        # We "unassign" the cluster security group here (i.e. the
        # 'flintrock-clustername' group) so that we can immediately delete it once
        # the instances are terminated. If we don't do this, we get dependency
        # violations for a couple of minutes before we can actually delete the group.
        # TODO: Is there a way to do this in one call for all instances?
        #       Do we need to throttle these calls?
        for instance in self.instances:
            instance.modify_attribute(
                Groups=[flintrock_base_group.id])

        # TODO: Centralize logic to get cluster security group name from cluster name.
        cluster_group = list(
            ec2.security_groups.filter(
                Filters=[
                    {'Name': 'group-name', 'Values': ['flintrock-' + self.name]},
                    {'Name': 'vpc-id', 'Values': [self.vpc_id]},
                ]))[0]
        cluster_group.delete()

        (ec2.instances
            .filter(InstanceIds=[instance.id for instance in self.instances])
            .terminate())

    def start_check(self):
        if self.state == 'running':
            raise NothingToDo("Cluster is already running.")
        elif self.state != 'stopped':
            raise ClusterInvalidState(
                attempted_command='start',
                state=self.state)

    @timeit
    def start(self, *, user: str, identity_file: str):
        # TODO: Do these _check() methods make sense here?
        self.start_check()
        ec2 = boto3.resource(service_name='ec2', region_name=self.region)
        (ec2.instances
            .filter(InstanceIds=[instance.id for instance in self.instances])
            .start())
        self.wait_for_state('running')

        super().start(
            user=user,
            identity_file=identity_file)

    def stop_check(self):
        if self.state == 'stopped':
            raise NothingToDo("Cluster is already stopped.")
        elif self.state != 'running':
            raise ClusterInvalidState(
                attempted_command='stop',
                state=self.state)

    @timeit
    def stop(self):
        self.stop_check()
        super().stop()

        ec2 = boto3.resource(service_name='ec2', region_name=self.region)
        (ec2.instances
            .filter(InstanceIds=[instance.id for instance in self.instances])
            .stop())
        self.wait_for_state('stopped')

    def run_command_check(self):
        if self.state != 'running':
            raise ClusterInvalidState(
                attempted_command='run-command',
                state=self.state)

    @timeit
    def run_command(self, *, master_only, command, user, identity_file):
        self.run_command_check()
        super().run_command(
            master_only=master_only,
            user=user,
            identity_file=identity_file,
            command=command)

    def copy_file_check(self):
        if self.state != 'running':
            raise ClusterInvalidState(
                attempted_command='copy-file',
                state=self.state)

    @timeit
    def copy_file(self, *, local_path, remote_path, master_only=False, user, identity_file):
        self.copy_file_check()
        super().copy_file(
            master_only=master_only,
            user=user,
            identity_file=identity_file,
            local_path=local_path,
            remote_path=remote_path)

    def print(self):
        """
        Print information about the cluster to screen in YAML.

        We don't use PyYAML because we want to control the key order
        in the output.
        """
        # Mark the boundaries of the YAML output.
        # See: http://yaml.org/spec/current.html#id2525905
        # print('---')
        print(self.name + ':')
        print('  state: {s}'.format(s=self.state))
        print('  node-count: {nc}'.format(nc=len(self.instances)))
        if self.state == 'running':
            print('  master:', self.master_host)
            print('\n    - '.join(['  slaves:'] + self.slave_hosts))
        # print('...')


def get_default_vpc(region: str) -> 'boto3.resources.factory.ec2.Vpc':
    """
    Get the user's default VPC in the provided region.
    """
    ec2 = boto3.resource(service_name='ec2', region_name=region)

    default_vpc = list(
        ec2.vpcs.filter(
            Filters=[{'Name': 'isDefault', 'Values': ['true']}]))

    if default_vpc:
        return default_vpc[0]
    else:
        raise NoDefaultVPC(region=region)


def check_network_config(*, region_name: str, vpc_id: str, subnet_id: str):
    """
    Check that the VPC and subnet are configured to allow Flintrock to create
    clusters.

    Currently, Flintrock requires DNS names and public IPs to be enabled.
    """
    ec2 = boto3.resource(service_name='ec2', region_name=region_name)

    if not ec2.Vpc(vpc_id).describe_attribute(Attribute='enableDnsHostnames')['EnableDnsHostnames']['Value']:
        raise ConfigurationNotSupported(
            "{v} does not have DNS hostnames enabled. "
            "Flintrock requires DNS hostnames to be enabled.\n"
            "See: https://github.com/nchammas/flintrock/issues/43"
            .format(v=vpc_id)
        )


def get_or_create_ec2_security_groups(
        *,
        cluster_name,
        vpc_id,
        region) -> "List[boto3.resource('ec2').SecurityGroup]":
    """
    If they do not already exist, create all the security groups needed for a
    Flintrock cluster.
    """
    ec2 = boto3.resource(service_name='ec2', region_name=region)

    SecurityGroupRule = namedtuple(
        'SecurityGroupRule', [
            'ip_protocol',
            'from_port',
            'to_port',
            'src_group',
            'cidr_ip'])

    # TODO: Make these into methods, since we need this logic (though simple)
    #       in multiple places. (?)
    flintrock_group_name = 'flintrock'
    cluster_group_name = 'flintrock-' + cluster_name

    # The Flintrock group is common to all Flintrock clusters and authorizes client traffic
    # to them.
    flintrock_group = list(
        ec2.security_groups.filter(
            Filters=[
                {'Name': 'group-name', 'Values': [flintrock_group_name]},
                {'Name': 'vpc-id', 'Values': [vpc_id]},
            ]))
    flintrock_group = flintrock_group[0] if flintrock_group else None

    # The cluster group is specific to one Flintrock cluster and authorizes intra-cluster
    # communication.
    cluster_group = list(
        ec2.security_groups.filter(
            Filters=[
                {'Name': 'group-name', 'Values': [cluster_group_name]},
                {'Name': 'vpc-id', 'Values': [vpc_id]},
            ]))
    cluster_group = cluster_group[0] if cluster_group else None

    if not flintrock_group:
        flintrock_group = ec2.create_security_group(
            GroupName=flintrock_group_name,
            Description="Flintrock base group",
            VpcId=vpc_id)

    # Rules for the client interacting with the cluster.
    flintrock_client_ips = []
    flintrock_client_ips.append(socket.gethostbyname(socket.gethostname()))
    flintrock_client_ips.append((
        urllib.request.urlopen('http://checkip.amazonaws.com/')
        .read().decode('utf-8').strip()))
    flintrock_client_cidrs = ['{ip}/32'.format(ip=fcip) for fcip in flintrock_client_ips]

    # TODO: Services should be responsible for registering what ports they want exposed.
    client_rules = []
    for flintrock_client_cidr in flintrock_client_cidrs:
        client_rules.extend([
            # SSH
            SecurityGroupRule(
                ip_protocol='tcp',
                from_port=22,
                to_port=22,
                cidr_ip=flintrock_client_cidr,
                src_group=None),
            # HDFS
            SecurityGroupRule(
                ip_protocol='tcp',
                from_port=50070,
                to_port=50070,
                cidr_ip=flintrock_client_cidr,
                src_group=None),
            # Spark
            SecurityGroupRule(
                ip_protocol='tcp',
                from_port=8080,
                to_port=8081,
                cidr_ip=flintrock_client_cidr,
                src_group=None),
            SecurityGroupRule(
                ip_protocol='tcp',
                from_port=4040,
                to_port=4040,
                cidr_ip=flintrock_client_cidr,
                src_group=None),
            SecurityGroupRule(
                ip_protocol='tcp',
                from_port=7077,
                to_port=7077,
                cidr_ip=flintrock_client_cidr,
                src_group=None),
            # Spark REST Server
            SecurityGroupRule(
                ip_protocol='tcp',
                from_port=6066,
                to_port=6066,
                cidr_ip=flintrock_client_cidr,
                src_group=None)
        ])

    # TODO: Don't try adding rules that already exist.
    # TODO: Add rules in one shot.
    for rule in client_rules:
        try:
            flintrock_group.authorize_ingress(
                IpProtocol=rule.ip_protocol,
                FromPort=rule.from_port,
                ToPort=rule.to_port,
                CidrIp=rule.cidr_ip)
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] != 'InvalidPermission.Duplicate':
                raise Exception("Error adding rule: {r}".format(r=rule))

    # Rules for internal cluster communication.
    if not cluster_group:
        cluster_group = ec2.create_security_group(
            GroupName=cluster_group_name,
            Description="Flintrock cluster group",
            VpcId=vpc_id)

    try:
        cluster_group.authorize_ingress(
            IpPermissions=[
                {
                    'IpProtocol': '-1',  # -1 means all
                    'FromPort': -1,
                    'ToPort': -1,
                    'UserIdGroupPairs': [{'GroupId': cluster_group.id}]
                }])
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] != 'InvalidPermission.Duplicate':
            raise Exception("Error authorizing cluster ingress to self.") from e

    return [flintrock_group, cluster_group]


def get_ec2_block_device_mappings(
        *,
        ami: str,
        region: str) -> 'List[dict]':
    """
    Get the block device map we should assign to instances launched from a given AMI.

    This is how we configure storage on the instance.
    """
    ec2 = boto3.resource(service_name='ec2', region_name=region)
    block_device_mappings = []
    min_root_device_size_gb = 30

    # An IndexError here is probably a sign of this problem:
    # https://github.com/boto/boto3/issues/496
    image = list(
        ec2.images.filter(ImageIds=[ami]))[0]

    if image.root_device_type == 'ebs':
        root_device = [
            device for device in image.block_device_mappings
            if device['DeviceName'] == image.root_device_name][0]
        if root_device['Ebs']['VolumeSize'] < min_root_device_size_gb:
            root_device['Ebs'].update({
                # Max root volume size for instance store-backed AMIs is 10 GiB.
                # See: http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/add-instance-store-volumes.html
                # Though, this code is probably incorrect for instance store-backed
                # instances anyway, since boto3 doesn't seem to let you set the size
                # of a root instance store volume.
                'VolumeSize': min_root_device_size_gb,
                # gp2 is general-purpose SSD
                'VolumeType': 'gp2'})
        del root_device['Ebs']['Encrypted']
        block_device_mappings.append(root_device)

    for i in range(12):
        ephemeral_device = {
            'VirtualName': 'ephemeral' + str(i),
            'DeviceName': '/dev/sd' + string.ascii_lowercase[i + 1]}
        block_device_mappings.append(ephemeral_device)

    return block_device_mappings


@timeit
def launch(
        *,
        cluster_name,
        num_slaves,
        services,
        assume_yes,
        key_name,
        identity_file,
        instance_type,
        region,
        availability_zone,
        ami,
        user,
        spot_price=None,
        vpc_id,
        subnet_id,
        instance_profile_name,
        placement_group,
        tenancy='default',
        ebs_optimized=False,
        instance_initiated_shutdown_behavior='stop'):
    """
    Launch a cluster.
    """
    if not vpc_id:
        vpc_id = get_default_vpc(region=region).id
    else:
        # If it's a non-default VPC -- i.e. the user set it up -- make sure it's
        # configured correctly.
        check_network_config(
            region_name=region,
            vpc_id=vpc_id,
            subnet_id=subnet_id)

    try:
        get_cluster(
            cluster_name=cluster_name,
            region=region,
            vpc_id=vpc_id)
    except ClusterNotFound as e:
        pass
    else:
        raise ClusterAlreadyExists(
            "Cluster {c} already exists in region {r}, VPC {v}.".format(
                c=cluster_name,
                r=region,
                v=vpc_id))

    try:
        security_groups = get_or_create_ec2_security_groups(
            cluster_name=cluster_name,
            vpc_id=vpc_id,
            region=region)
        block_device_mappings = get_ec2_block_device_mappings(
            ami=ami,
            region=region)
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'InvalidAMIID.NotFound':
            raise Error(
                "Error: Could not find {ami} in region {region}.".format(
                    ami=ami,
                    region=region))
        else:
            raise

    ec2 = boto3.resource(service_name='ec2', region_name=region)

    num_instances = num_slaves + 1
    spot_requests = []
    cluster_instances = []

    try:
        if spot_price:
            print("Requesting {c} spot instances at a max price of ${p}...".format(
                c=num_instances, p=spot_price))
            client = ec2.meta.client
            spot_requests = client.request_spot_instances(
                SpotPrice=str(spot_price),
                InstanceCount=num_instances,
                LaunchSpecification={
                    'ImageId': ami,
                    'KeyName': key_name,
                    'InstanceType': instance_type,
                    'BlockDeviceMappings': block_device_mappings,
                    'Placement': {
                        'AvailabilityZone': availability_zone,
                        'GroupName': placement_group},
                    'SecurityGroupIds': [sg.id for sg in security_groups],
                    'SubnetId': subnet_id,
                    'IamInstanceProfile': {
                        'Name': instance_profile_name},
                    'EbsOptimized': ebs_optimized})['SpotInstanceRequests']

            request_ids = [r['SpotInstanceRequestId'] for r in spot_requests]
            pending_request_ids = request_ids

            while pending_request_ids:
                print("{grant} of {req} instances granted. Waiting...".format(
                    grant=num_instances - len(pending_request_ids),
                    req=num_instances))
                time.sleep(30)
                spot_requests = client.describe_spot_instance_requests(
                    SpotInstanceRequestIds=request_ids)['SpotInstanceRequests']

                failed_requests = [r for r in spot_requests if r['State'] == 'failed']
                if failed_requests:
                    failure_reasons = {r['Status']['Code'] for r in failed_requests}
                    raise Error(
                        "The spot request failed for the following reason{s}: {reasons}"
                        .format(
                            s='' if len(failure_reasons) == 1 else 's',
                            reasons=', '.join(failure_reasons)))

                pending_request_ids = [
                    r['SpotInstanceRequestId'] for r in spot_requests
                    if r['State'] == 'open']

            print("All {c} instances granted.".format(c=num_instances))

            cluster_instances = list(
                ec2.instances.filter(
                    InstanceIds=[r['InstanceId'] for r in spot_requests]))
        else:
            print("Launching {c} instances...".format(c=num_instances))

            cluster_instances = ec2.create_instances(
                MinCount=num_instances,
                MaxCount=num_instances,
                ImageId=ami,
                KeyName=key_name,
                InstanceType=instance_type,
                BlockDeviceMappings=block_device_mappings,
                Placement={
                    'AvailabilityZone': availability_zone,
                    'Tenancy': tenancy,
                    'GroupName': placement_group},
                SecurityGroupIds=[sg.id for sg in security_groups],
                SubnetId=subnet_id,
                IamInstanceProfile={
                    'Name': instance_profile_name},
                EbsOptimized=ebs_optimized,
                InstanceInitiatedShutdownBehavior=instance_initiated_shutdown_behavior)

        time.sleep(10)  # AWS metadata eventual consistency tax.

        master_instance = cluster_instances[0]
        slave_instances = cluster_instances[1:]

        (ec2.instances
            .filter(InstanceIds=[master_instance.id])
            .create_tags(
                Tags=[
                    {'Key': 'flintrock-role', 'Value': 'master'},
                    {'Key': 'Name', 'Value': '{c}-master'.format(c=cluster_name)}]))
        (ec2.instances
            .filter(InstanceIds=[i.id for i in slave_instances])
            .create_tags(
                Tags=[
                    {'Key': 'flintrock-role', 'Value': 'slave'},
                    {'Key': 'Name', 'Value': '{c}-slave'.format(c=cluster_name)}]))

        cluster = EC2Cluster(
            name=cluster_name,
            region=region,
            vpc_id=vpc_id,
            ssh_key_pair=generate_ssh_key_pair(),
            master_instance=master_instance,
            slave_instances=slave_instances)

        cluster.wait_for_state('running')

        provision_cluster(
            cluster=cluster,
            services=services,
            user=user,
            identity_file=identity_file)

    except (Exception, KeyboardInterrupt) as e:
        # TODO: Cleanup cluster security group here.
        print("There was a problem with the launch. Cleaning up...", file=sys.stderr)

        if spot_requests:
            request_ids = [r['SpotInstanceRequestId'] for r in spot_requests]
            if any([r['State'] != 'active' for r in spot_requests]):
                print("Canceling spot instance requests...", file=sys.stderr)
                client.cancel_spot_instance_requests(
                    SpotInstanceRequestIds=request_ids)
            # Make sure we have the latest information on any launched spot instances.
            spot_requests = client.describe_spot_instance_requests(
                SpotInstanceRequestIds=request_ids)['SpotInstanceRequests']
            instance_ids = [
                r['InstanceId'] for r in spot_requests
                if 'InstanceId' in r]
            if instance_ids:
                cluster_instances = list(
                    ec2.instances.filter(InstanceIds=instance_ids))

        if cluster_instances:
            if not assume_yes:
                yes = click.confirm(
                    text="Do you want to terminate the {c} instances created by this operation?"
                         .format(c=len(cluster_instances)),
                    err=True,
                    default=True)

            if assume_yes or yes:
                print("Terminating instances...", file=sys.stderr)
                (ec2.instances
                    .filter(InstanceIds=[instance.id for instance in cluster_instances])
                    .terminate())

        raise


def get_cluster(*, cluster_name: str, region: str, vpc_id: str) -> EC2Cluster:
    """
    Get an existing EC2 cluster.
    """
    cluster = get_clusters(
        cluster_names=[cluster_name],
        region=region,
        vpc_id=vpc_id)
    return cluster[0]


def get_clusters(*, cluster_names: list=[], region: str, vpc_id: str) -> list:
    """
    Get all the named clusters. If no names are given, get all clusters.

    We do a little extra work here so that we only make one call to AWS
    regardless of how many clusters we have to look up. That's because querying
    AWS -- a network operation -- is by far the slowest step.
    """
    ec2 = boto3.resource(service_name='ec2', region_name=region)
    if not vpc_id:
        vpc_id = get_default_vpc(region=region).id

    if cluster_names:
        group_name_filter = ['flintrock-' + cn for cn in cluster_names]
    else:
        group_name_filter = ['flintrock']

    all_clusters_instances = list(
        ec2.instances.filter(
            Filters=[
                {'Name': 'instance.group-name', 'Values': group_name_filter},
                {'Name': 'vpc-id', 'Values': [vpc_id]},
            ]))

    found_cluster_names = {
        _get_cluster_name(instance) for instance in all_clusters_instances}

    if cluster_names:
        missing_cluster_names = set(cluster_names) - found_cluster_names
        if missing_cluster_names:
            raise ClusterNotFound("No cluster {c} in region {r}.".format(
                c=missing_cluster_names.pop(),
                r=region))

    clusters = [
        _compose_cluster(
            name=cluster_name,
            region=region,
            vpc_id=vpc_id,
            instances=list(filter(
                lambda x: _get_cluster_name(x) == cluster_name, all_clusters_instances)))
        for cluster_name in found_cluster_names]

    return clusters


def _get_cluster_name(instance: 'boto3.resources.factory.ec2.Instance') -> str:
    """
    Given an EC2 instance, get the name of the Flintrock cluster it belongs to.
    """
    for group in instance.security_groups:
        if group['GroupName'].startswith('flintrock-'):
            return group['GroupName'].replace('flintrock-', '', 1)
    else:
        raise Exception("Could not extract cluster name from instance: {i}".format(
            i=instance.id))


def _get_cluster_master_slaves(
        instances: list) -> ('boto3.resources.factory.ec2.Instance', list):
    """
    Get the master and slave instances from a set of raw EC2 instances representing
    a Flintrock cluster.
    """
    master_instance = None
    slave_instances = []

    for instance in instances:
        for tag in instance.tags:
            if tag['Key'] == 'flintrock-role':
                if tag['Value'] == 'master':
                    if master_instance is not None:
                        raise Exception("More than one master found.")
                    else:
                        master_instance = instance
                        break
                elif tag['Value'] == 'slave':
                    slave_instances.append(instance)

    if not master_instance:
        raise Exception("No master found.")
    elif not slave_instances:
        raise Exception("No slaves found.")

    return (master_instance, slave_instances)


def _compose_cluster(*, name: str, region: str, vpc_id: str, instances: list) -> EC2Cluster:
    """
    Compose an EC2Cluster object from a set of raw EC2 instances representing
    a Flintrock cluster.
    """
    (master_instance, slave_instances) = _get_cluster_master_slaves(instances)

    cluster = EC2Cluster(
        name=name,
        region=region,
        vpc_id=vpc_id,
        master_instance=master_instance,
        slave_instances=slave_instances)

    return cluster
