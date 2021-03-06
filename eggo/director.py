# Licensed to Big Data Genomics (BDG) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The BDG licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# This module contains internal code for working with Cloudera Director-
# configured clusters. The functions here should probably be using the Fabric
# API to execute commands on the cluster nodes (along with AWS/boto objects).
# The functions here could (should) be refactored into a Director interface
# that could be implemented on multiple clouds.


from getpass import getuser
from datetime import datetime
from cStringIO import StringIO

from boto.ec2.networkinterface import (
    NetworkInterfaceCollection, NetworkInterfaceSpecification)
from fabric.api import (
    sudo, run, execute, put, open_shell, env, parallel, cd)
from fabric.contrib.files import append, exists
from cm_api.api_client import ApiResource

from eggo.error import EggoError
from eggo.config import (
    get_aws_access_key_id, get_aws_secret_access_key, get_ec2_key_pair,
    get_ec2_private_key_file)
from eggo.aws import (
    create_cf_connection, create_cf_stack, get_subnet_id, delete_stack,
    get_security_group_id, create_ec2_connection, get_tagged_instances,
    wait_for_instance_state)
from eggo.util import non_blocking_tunnel, tunnel_ctx
from eggo.operations import generate_eggo_env_vars


env.user = 'ec2-user'
env.key_filename = get_ec2_private_key_file()


def cm_tunnel_ctx(manager_instance):
    # shortcut fn returns a context object that sets up a tunnel on
    # localhost:64999
    return tunnel_ctx(manager_instance.ip_address,
                      manager_instance.private_ip_address, 7180, 64999,
                      'ec2-user', get_ec2_private_key_file())


def install_private_key():
    put(get_ec2_private_key_file(), 'id.pem')
    run('chmod 600 id.pem')


def install_director_client():
    sudo('wget http://archive.cloudera.com/director/redhat/6/x86_64/director/'
         'cloudera-director.repo -O /etc/yum.repos.d/cloudera-director.repo')
    sudo('yum -y install cloudera-director-client')


def create_launcher_instance(ec2_conn, cf_conn, stack_name, launcher_ami,
                             launcher_instance_type):
    launcher_instances = get_tagged_instances(
        ec2_conn, {'eggo_stack_name': stack_name,
                   'eggo_node_type': 'launcher'})
    if len(launcher_instances) > 0:
        print "Launcher instance ({instance}) already exists. Reusing.".format(
            instance=launcher_instances[0].ip_address)
        return launcher_instances[0]

    print "Creating launcher instance."
    # see http://stackoverflow.com/questions/19029588/how-to-auto-assign-public-ip-to-ec2-instance-with-boto
    interface = NetworkInterfaceSpecification(
        subnet_id=get_subnet_id(cf_conn, stack_name),
        groups=[get_security_group_id(cf_conn, stack_name)],
        associate_public_ip_address=True)
    interfaces = NetworkInterfaceCollection(interface)
    reservation = ec2_conn.run_instances(
        launcher_ami,
        key_name=get_ec2_key_pair(),
        instance_type=launcher_instance_type,
        network_interfaces=interfaces)
    launcher_instance = reservation.instances[0]

    launcher_instance.add_tag('owner', getuser())
    launcher_instance.add_tag('ec2_key_pair', get_ec2_key_pair())
    launcher_instance.add_tag('eggo_stack_name', stack_name)
    launcher_instance.add_tag('eggo_node_type', 'launcher')
    wait_for_instance_state(ec2_conn, launcher_instance)
    execute(install_director_client, hosts=[launcher_instance.ip_address])
    execute(install_private_key, hosts=[launcher_instance.ip_address])
    return launcher_instance


def run_director_bootstrap(director_conf_path, region, cluster_ami,
                           num_workers, stack_name, worker_instance_type):
    # replace variables in conf template and copy to launcher
    cf_conn = create_cf_connection(region)
    params = {'accessKeyId': get_aws_access_key_id(),
              'secretAccessKey': get_aws_secret_access_key(),
              'region': region,
              'stack_name': stack_name,
              'owner': getuser(),
              'keyName': get_ec2_key_pair(),
              'subnetId': get_subnet_id(cf_conn, stack_name),
              'securityGroupsIds': get_security_group_id(cf_conn, stack_name),
              'image': cluster_ami,
              'num_workers': num_workers,
              'worker_instance_type': worker_instance_type}
    with open(director_conf_path, 'r') as template_file:
        interpolated_body = template_file.read() % params
        director_conf = StringIO(interpolated_body)
    put(director_conf, 'director.conf')
    # bootstrap the Hadoop cluster
    run('cloudera-director bootstrap director.conf')


def provision(region, availability_zone, stack_name, cf_template_path,
              launcher_ami, launcher_instance_type, worker_instance_type,
              director_conf_path, cluster_ami, num_workers):
    start_time = datetime.now()

    # create cloudformation stack (VPC etc)
    cf_conn = create_cf_connection(region)
    create_cf_stack(cf_conn, stack_name, cf_template_path, availability_zone)

    # create launcher instance
    ec2_conn = create_ec2_connection(region)
    launcher_instance = create_launcher_instance(
        ec2_conn, cf_conn, stack_name, launcher_ami, launcher_instance_type)

    # run bootstrap on launcher
    execute(
        run_director_bootstrap,
        director_conf_path=director_conf_path, region=region,
        cluster_ami=cluster_ami, num_workers=num_workers,
        stack_name=stack_name, worker_instance_type=worker_instance_type,
        hosts=[launcher_instance.ip_address])

    end_time = datetime.now()
    print "Cluster has started. Took {t} minutes.".format(
        t=(end_time - start_time).seconds / 60)


def get_launcher_instance(ec2_conn, stack_name):
    return get_tagged_instances(ec2_conn, {'eggo_stack_name': stack_name,
                                           'eggo_node_type': 'launcher'})[0]


def get_manager_instance(ec2_conn, stack_name):
    return get_tagged_instances(ec2_conn, {'eggo_stack_name': stack_name,
                                           'eggo_node_type': 'manager'})[0]


def get_master_instance(ec2_conn, stack_name):
    return get_tagged_instances(ec2_conn, {'eggo_stack_name': stack_name,
                                           'eggo_node_type': 'master'})[0]


def get_worker_instances(ec2_conn, stack_name):
    return get_tagged_instances(ec2_conn, {'eggo_stack_name': stack_name,
                                           'eggo_node_type': 'worker'})


def describe(region, stack_name):
    ec2_conn = create_ec2_connection(region)
    print 'Launcher', get_launcher_instance(ec2_conn, stack_name).ip_address
    print 'Manager', get_manager_instance(ec2_conn, stack_name).ip_address
    print 'Master', get_master_instance(ec2_conn, stack_name).ip_address
    for instance in get_worker_instances(ec2_conn, stack_name):
        print 'Worker', instance.ip_address


def login(region, stack_name, node):
    print('Logging into the {0} node...'.format(node))
    ec2_conn = create_ec2_connection(region)
    if node == 'master':
        hosts = [get_master_instance(ec2_conn, stack_name).ip_address]
    elif node == 'manager':
        hosts = [get_manager_instance(ec2_conn, stack_name).ip_address]
    elif node == 'launcher':
        hosts = [get_launcher_instance(ec2_conn, stack_name).ip_address]
    else:
        raise EggoError('"{0}" is not a valid node type'.format(node))
    execute(open_shell, hosts=hosts)


def web_proxy(region, stack_name):
    ec2_conn = create_ec2_connection(region)
    manager_instance = get_manager_instance(ec2_conn, stack_name)
    master_instance = get_master_instance(ec2_conn, stack_name)
    worker_instances = get_worker_instances(ec2_conn, stack_name)

    tunnels = []
    ts = '{0:<22}{1:<17}{2:<17}{3:<7}localhost:{4}'
    print(ts.format('name', 'public', 'private', 'remote', 'local'))

    # CM
    tunnels.append(non_blocking_tunnel(manager_instance.ip_address,
                                       manager_instance.private_ip_address,
                                       7180, 7180, 'ec2-user',
                                       get_ec2_private_key_file()))
    print(ts.format(
        'CM WebUI', manager_instance.ip_address,
        manager_instance.private_ip_address, 7180, 7180))

    # YARN RM
    tunnels.append(non_blocking_tunnel(master_instance.ip_address,
                                       master_instance.private_ip_address,
                                       8088, 8088, 'ec2-user',
                                       get_ec2_private_key_file()))
    print(ts.format(
        'YARN RM', master_instance.ip_address,
        master_instance.private_ip_address, 8088, 8088))

    # YARN JobHistory
    tunnels.append(non_blocking_tunnel(master_instance.ip_address,
                                       master_instance.private_ip_address,
                                       19888, 19888, 'ec2-user',
                                       get_ec2_private_key_file()))
    print(ts.format(
        'YARN JobHistory', master_instance.ip_address,
        master_instance.private_ip_address, 19888, 19888))

    try:
        # block on an arbitrary ssh tunnel
        tunnels[-1].wait()
    finally:
        for tunnel in tunnels:
            tunnel.terminate()


def run_director_terminate():
    run('cloudera-director terminate --lp.terminate.assumeYes=true '
        'director.conf')


def terminate_launcher_instance(ec2_conn, stack_name):
    launcher_instance = get_launcher_instance(ec2_conn, stack_name)
    launcher_instance.terminate()
    wait_for_instance_state(ec2_conn, launcher_instance, 'terminated')


def teardown(region, stack_name):
    # terminate Hadoop cluster (prompts for confirmation)
    ec2_conn = create_ec2_connection(region)
    execute(run_director_terminate,
            hosts=[get_launcher_instance(ec2_conn, stack_name).ip_address])

    # terminate launcher instance
    terminate_launcher_instance(ec2_conn, stack_name)

    # delete stack
    cf_conn = create_cf_connection(region)
    delete_stack(cf_conn, stack_name)


def install_java_8(region, stack_name):
    # following general protocol for upgrading to JDK 1.8 here:
    # http://www.cloudera.com/content/cloudera/en/documentation/core/v5-3-x/topics/cdh_cm_upgrading_to_jdk8.html
    ec2_conn = create_ec2_connection(region)
    manager_instance = get_manager_instance(ec2_conn, stack_name)
    cluster_instances = (
        get_worker_instances(ec2_conn, stack_name) +
        [manager_instance, get_master_instance(ec2_conn, stack_name)])
    cluster_hosts = [i.ip_address for i in cluster_instances]

    with cm_tunnel_ctx(manager_instance) as local_port:
        # Connect to CM API
        cm_api = ApiResource('localhost', username='admin', password='admin',
                             server_port=local_port, version=9)
        cloudera_manager = cm_api.get_cloudera_manager()

        # Stop Cloudera Management Service
        print "Stopping Cloudera Management Service"
        mgmt_service = cloudera_manager.get_service()
        mgmt_service.stop().wait()

        # Stop cluster
        print "Stopping the cluster"
        clusters = cm_api.get_all_clusters()
        cluster = clusters.objects[0]
        cluster.stop().wait()

    # Stop all Cloudera Manager Agents
    @parallel
    def stop_cm_agents():
        sudo('service cloudera-scm-agent stop')
    execute(stop_cm_agents, hosts=cluster_hosts)

    # Stop the Cloudera Manager Server
    def stop_cm_server():
        sudo('service cloudera-scm-server stop')
    execute(stop_cm_server, hosts=[manager_instance.ip_address])

    # Cleanup other Java versions and install JDK 1.8
    @parallel
    def swap_jdks():
        sudo('rpm -qa | grep jdk | xargs rpm -e')
        sudo('rm -rf /usr/java/jdk1.6*')
        sudo('rm -rf /usr/java/jdk1.7*')
        run('wget -O jdk-8-linux-x64.rpm --no-cookies --no-check-certificate '
            '--header "Cookie: oraclelicense=accept-securebackup-cookie" '
            'http://download.oracle.com/otn-pub/java/jdk/8u51-b16/'
            'jdk-8u51-linux-x64.rpm')
        sudo('yum install -y jdk-8-linux-x64.rpm')
        append('/home/ec2-user/.bash_profile',
               'export JAVA_HOME=`find /usr/java -name "jdk1.8*"`')
    execute(swap_jdks, hosts=cluster_hosts)

    # Start the Cloudera Manager Server
    def start_cm_server():
        sudo('service cloudera-scm-server start')
    execute(start_cm_server, hosts=[manager_instance.ip_address])

    # Start all Cloudera Manager Agents
    @parallel
    def start_cm_agents():
        sudo('service cloudera-scm-agent start')
    execute(start_cm_agents, hosts=cluster_hosts)

    with cm_tunnel_ctx(manager_instance) as local_port:
        # Connect to CM API
        cm_api = ApiResource('localhost', username='admin', password='admin',
                             server_port=local_port, version=9)
        cloudera_manager = cm_api.get_cloudera_manager()

        # Start the cluster and the mgmt service
        print "Starting the cluster"
        cluster.start().wait()
        print "Starting the Cloudera Management Service"
        cloudera_manager = cm_api.get_cloudera_manager()
        mgmt_service = cloudera_manager.get_service()
        mgmt_service.start().wait()


def create_hdfs_home():
    sudo('hadoop fs -mkdir /user/ec2-user', user='hdfs')
    sudo('hadoop fs -chown ec2-user:supergroup /user/ec2-user', user='hdfs')
    sudo('hadoop fs -chmod 777 /user/ec2-user', user='hdfs')


def install_dev_tools():
    sudo("yum groupinstall -y 'Development Tools'")
    sudo('yum install -y cmake xz-devel ncurses ncurses-devel')
    sudo('yum install -y zlib zlib-devel snappy snappy-devel')
    sudo('yum install -y python-devel')
    sudo('curl https://bootstrap.pypa.io/get-pip.py | python')
    sudo('pip install -U pip setuptools')


def install_parquet_tools(version='1.8.1'):
    run('curl -L -O http://search.maven.org/remotecontent?filepath=org/apache/'
        'parquet/parquet-tools/{0}/parquet-tools-{0}.jar'.format(version))


def install_git():
    sudo('yum install -y git')


def install_maven(version='3.3.3'):
    url = ('http://apache.mesi.com.ar/maven/maven-3/{0}/binaries/'
           'apache-maven-{0}-bin.tar.gz'.format(version))
    run('wget {0}'.format(url))
    run('tar -xzf apache-maven-{0}-bin.tar.gz'.format(version))
    append('/home/ec2-user/.bash_profile',
           'export PATH=/home/ec2-user/apache-maven-{0}/bin:$PATH'.format(
               version))


def install_gradle(version='2.6'):
    url = ('https://services.gradle.org/distributions/'
           'gradle-{0}-bin.zip'.format(version))
    run('wget {0}'.format(url))
    run('unzip gradle-{0}-bin.zip'.format(version))
    append('/home/ec2-user/.bash_profile',
           'export PATH=/home/ec2-user/gradle-{0}/bin:$PATH'.format(version))


def install_adam(fork='bigdatagenomics', branch='master'):
    run('git clone https://github.com/{0}/adam.git'.format(fork))
    with cd('adam'):
        if branch != 'master':
            run('git checkout origin/{0}'.format(branch))
        run('mvn clean package -DskipTests')


def install_opencb_ga4gh(fork='opencb', branch='master'):
    run('git clone https://github.com/{0}/ga4gh.git'.format(fork))
    with cd('ga4gh'):
        if branch != 'master':
            run('git checkout origin/{0}'.format(branch))
        run('mvn clean install -DskipTests')


def install_opencb_java_common(fork='opencb', branch='develop'):
    run('git clone https://github.com/{0}/java-common-libs.git'.format(fork))
    with cd('java-common-libs'):
        if branch != 'develop':
            run('git checkout origin/{0}'.format(branch))
        run('mvn clean install -DskipTests')


def install_opencb_biodata(fork='opencb', branch='develop'):
    run('git clone https://github.com/{0}/biodata.git'.format(fork))
    with cd('biodata'):
        if branch != 'develop':
            run('git checkout origin/{0}'.format(branch))
        run('mvn clean install -DskipTests')


def install_opencb_hpg_bigdata(fork='opencb', branch='develop'):
    run('git clone https://github.com/{0}/hpg-bigdata.git'.format(fork))
    with cd('hpg-bigdata'):
        if branch != 'develop':
            run('git checkout origin/{0}'.format(branch))
        run('./build.sh')


def install_opencb(hosts):
    execute(install_opencb_ga4gh, hosts=hosts)
    execute(install_opencb_java_common, hosts=hosts)
    execute(install_opencb_biodata, hosts=hosts)
    execute(install_opencb_hpg_bigdata, hosts=hosts)


def install_quince(fork='cloudera', branch='master'):
    run('git clone https://github.com/{0}/quince.git'.format(fork))
    with cd('quince'):
        if branch != 'master':
            run('git checkout origin/{0}'.format(branch))
        run('mvn clean package -DskipTests')


def install_gatk(fork='broadinstitute', branch='master'):
    run('git clone https://github.com/{0}/gatk.git'.format(fork))
    with cd('gatk'):
        if branch != 'master':
            run('git checkout origin/{0}'.format(branch))
        run('gradle sparkJar')


def install_eggo(fork='bigdatagenomics', branch='master', reinstall=False):
    if reinstall and exists('/home/ec2-user/eggo'):
        sudo('rm -rf /home/ec2-user/eggo')
    run('git clone https://github.com/{0}/eggo.git'.format(fork))
    with cd('eggo'):
        if branch != 'master':
            run('git checkout origin/{0}'.format(branch))
        sudo('python setup.py install')


def adjust_yarn_memory_limits(region, stack_name, restart=True):
    ec2_conn = create_ec2_connection(region)
    manager_instance = get_manager_instance(ec2_conn, stack_name)
    with cm_tunnel_ctx(manager_instance) as local_port:
        cm_api = ApiResource('localhost', username='admin', password='admin',
                             server_port=local_port, version=9)
        cluster = list(cm_api.get_all_clusters())[0]
        host = list(cm_api.get_all_hosts())[0]  # all hosts same instance type
        yarn = filter(lambda x: x.type == 'YARN',
                      list(cluster.get_all_services()))[0]
        rm_cg = filter(lambda x: x.roleType == 'RESOURCEMANAGER',
                       list(yarn.get_all_role_config_groups()))[0]
        nm_cg = filter(lambda x: x.roleType == 'NODEMANAGER',
                       list(yarn.get_all_role_config_groups()))[0]
        rm_cg.update_config({
            'yarn_scheduler_maximum_allocation_mb': (
                int(host.totalPhysMemBytes / 1024. / 1024.)),
            'yarn_scheduler_maximum_allocation_vcores': host.numCores})
        nm_cg.update_config({
            'yarn_nodemanager_resource_memory_mb': (
                int(host.totalPhysMemBytes / 1024. / 1024.)),
            'yarn_nodemanager_resource_cpu_vcores': host.numCores})
        cluster.deploy_client_config().wait()
        if restart:
            cluster.restart().wait()


def install_env_vars(region, stack_name):
    ec2_conn = create_ec2_connection(region)

    # get information about the cluster
    manager_instance = get_manager_instance(ec2_conn, stack_name)
    with cm_tunnel_ctx(manager_instance) as local_port:
        env_vars = generate_eggo_env_vars('localhost', local_port, 'admin',
                                          'admin')
    env_var_exports = ['export {0}={1}'.format(k, v)
                       for (k, v) in env_vars.iteritems()]

    def do():
        append('/home/ec2-user/eggo_env_vars.sh', env_var_exports)
        append('/home/ec2-user/.bash_profile',
               'source /home/ec2-user/eggo_env_vars.sh')

    master_host = get_master_instance(ec2_conn, stack_name).ip_address
    execute(do, hosts=[master_host])


def config_cluster(region, stack_name, adam, adam_fork, adam_branch, opencb,
                   gatk, quince, quince_fork, quince_branch):
    start_time = datetime.now()

    ec2_conn = create_ec2_connection(region)
    master_host = get_master_instance(ec2_conn, stack_name).ip_address

    execute(install_private_key, hosts=[master_host])
    execute(create_hdfs_home, hosts=[master_host])
    # java 8 install will restart the cluster
    adjust_yarn_memory_limits(region, stack_name, restart=False)
    install_java_8(region, stack_name)

    # install software tools
    execute(install_dev_tools, hosts=[master_host])
    execute(install_git, hosts=[master_host])
    execute(install_maven, hosts=[master_host])
    execute(install_gradle, hosts=[master_host])
    execute(install_parquet_tools, hosts=[master_host])
    if adam:
        execute(install_adam, fork=adam_fork, branch=adam_branch,
                hosts=[master_host])
    if opencb:
        install_opencb([master_host])
    if gatk:
        execute(install_gatk, hosts=[master_host])
    if quince:
        execute(install_quince, fork=quince_fork, branch=quince_branch,
                hosts=[master_host])
    execute(install_eggo, hosts=[master_host])

    # install environment vars for use on the cluster
    install_env_vars(region, stack_name)

    end_time = datetime.now()
    print "Cluster configured. Took {t} minutes.".format(
        t=(end_time - start_time).seconds / 60)
