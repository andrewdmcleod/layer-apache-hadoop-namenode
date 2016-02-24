from charms.reactive import when
from charms.reactive import when_not
from charms.reactive import set_state
from charms.reactive import remove_state
from charms.reactive import is_state
from charms.reactive.helpers import data_changed
from charms.hadoop import get_hadoop_base
from jujubigdata.handlers import HDFS
from jujubigdata import utils
from charmhelpers.core import hookenv, unitdata


@when('hadoop.installed')
@when_not('namenode.started')
def configure_namenode():
    local_hostname = hookenv.local_unit().replace('/', '-')
    private_address = hookenv.unit_get('private-address')
    ip_addr = utils.resolve_private_address(private_address)
    hadoop = get_hadoop_base()
    hdfs = HDFS(hadoop)
    hdfs.configure_namenode([local_hostname])
    hdfs.format_namenode()
    if hookenv.is_leader:
        hdfs.start_namenode()
    hdfs.create_hdfs_dirs()
    hadoop.open_ports('namenode')
    utils.update_kv_hosts({ip_addr: local_hostname})
    set_state('namenode.started')


@when('namenode.started')
@when_not('datanode.related')
def blocked():
    hookenv.status_set('blocked', 'Waiting for relation to DataNodes')


@when('namenode.started', 'datanode.related')
def send_info(datanode):
    hadoop = get_hadoop_base()
    hdfs = HDFS(hadoop)
    local_hostname = hookenv.local_unit().replace('/', '-')
    hdfs_port = hadoop.dist_config.port('namenode')
    webhdfs_port = hadoop.dist_config.port('nn_webapp_http')

    utils.update_kv_hosts(datanode.hosts_map())
    utils.manage_etc_hosts()

    datanode.send_spec(hadoop.spec())
    datanode.send_clustername(hookenv.service_name())
    datanode.send_namenodes([local_hostname])
    datanode.send_ports(hdfs_port, webhdfs_port)
    datanode.send_ssh_key(utils.get_ssh_key('hdfs'))
    datanode.send_hosts_map(utils.get_kv_hosts())

    slaves = datanode.nodes()
    if data_changed('namenode.slaves', slaves):
        unitdata.kv().set('namenode.slaves', slaves)
        hdfs.register_slaves(slaves)
        hdfs.reload_slaves()

    hookenv.status_set('active', 'Ready ({count} DataNode{s})'.format(
        count=len(slaves),
        s='s' if len(slaves) > 1 else '',
    ))
    set_state('namenode.ready')


@when('namenode-cluster.joined', 'datanode.journalnode.ha')
def configure_ha(cluster, datanode):
    hadoop = get_hadoop_base()
    hdfs = HDFS(hadoop)
    cluster_nodes = cluster.nodes()
    jn_nodes = datanode.nodes()
    jn_port = datanode.jn_port()
    local_hostname = hookenv.local_unit().replace('/', '-')
    if data_changed('namenode.ha', [cluster_nodes, jn_nodes, jn_port]):
        utils.update_kv_hosts(cluster.hosts_map())
        utils.manage_etc_hosts()
        hdfs.configure_namenode(cluster_nodes)
        if len(jn_nodes) > 2:
            hdfs.register_journalnodes(jn_nodes, jn_port)
        datanode.send_namenodes(cluster_nodes)
        if hookenv.is_leader:
            hdfs.transition_to_active([local_hostname])
            if len(jn_nodes) > 2 and not is_state('namenode.shared-edits.init'):
                hdfs.stop_namenode()
                hdfs.init_sharededits()
                set_state('namenode.shared-edits.init')
                hdfs.start_namenode()
                # 'leader' appears to transition back to standby after restart - test more
                hdfs.transition_to_active([local_hostname])
        if not hookenv.is_leader and len(jn_nodes) > 2:
            if not is_state('namenode.standby.bootstrapped'):
                hdfs.bootstrap_standby()
                set_state('namenode.standby.bootstrapped')
            hdfs.restart_namenode()

@when('namenode.clients')
@when('namenode.ready')
def accept_clients(clients):
    hadoop = get_hadoop_base()
    local_hostname = hookenv.local_unit().replace('/', '-')
    hdfs_port = hadoop.dist_config.port('namenode')
    webhdfs_port = hadoop.dist_config.port('nn_webapp_http')

    clients.send_spec(hadoop.spec())
    clients.send_namenodes([local_hostname])
    clients.send_ports(hdfs_port, webhdfs_port)
    clients.send_hosts_map(utils.get_kv_hosts())
    clients.send_ready(True)


@when('namenode.clients')
@when_not('namenode.ready')
def reject_clients(clients):
    clients.send_ready(False)


@when('namenode.started', 'datanode.departing')
def unregister_datanode(datanode):
    hadoop = get_hadoop_base()
    hdfs = HDFS(hadoop)

    slaves = unitdata.kv().get('namenode.slaves', [])
    slaves_leaving = datanode.nodes()  # only returns nodes in "leaving" state
    hookenv.log('Slaves leaving: {}'.format(slaves_leaving))

    slaves_remaining = list(set(slaves) - set(slaves_leaving))
    unitdata.kv().set('namenode.slaves', slaves_remaining)
    hdfs.register_slaves(slaves_remaining)

    utils.remove_kv_hosts(slaves_leaving)
    utils.manage_etc_hosts()

    if not slaves_remaining:
        hookenv.status_set('blocked', 'Waiting for relation to DataNodes')
        remove_state('namenode.ready')

    datanode.dismiss()


@when('benchmark.related')
def register_benchmarks(benchmark):
    benchmark.register('nnbench', 'testdfsio')
