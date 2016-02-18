from charms.reactive import when
from charms.reactive import when_not
from charms.reactive import set_state
from charms.reactive import remove_state
from charms.reactive.helpers import data_changed
from charms.hadoop import get_hadoop_base
from jujubigdata.handlers import HDFS
from jujubigdata import utils
from charmhelpers.core import hookenv, unitdata
from charmhelpers.contrib.hahelpers.cluster import peer_units


@when('hadoop.installed')
@when_not('namenode.started')
def configure_namenode():
    local_hostname = hookenv.local_unit().replace('/', '-')
    private_address = hookenv.unit_get('private-address')
    ip_addr = utils.resolve_private_address(private_address)
    hadoop = get_hadoop_base()
    hdfs = HDFS(hadoop)
    hdfs.configure_namenode(peer_units())
    hdfs.format_namenode()
    hdfs.start_namenode()
    hdfs.create_hdfs_dirs()
    hadoop.open_ports('namenode')
    utils.update_kv_hosts({ip_addr: local_hostname})
    set_state('namenode.started')
    unitdata.kv().set('hdfscluster.size', '1')
    unitdata.kv().set('hdfscluster.state', 'standalone')


#@when('hdfscluster.ready')
#def ha_namenodes(datanodes):
#    global namenodes
#    namenodes = [hookenv.local_unit().replace('/', '-'), hookenv.local_unit().replace('/', '-')]

#@when_not('hdfscluster.ready')
#def nonha_namenodes():
#    global namenodes
#    namenodes = [hookenv.local_unit().replace('/', '-')]

@when('namenode.started')
@when_not('datanode.related')
def blocked():
    hookenv.status_set('blocked', 'Waiting for relation to DataNodes')


@when('hadoop.installed', 'hdfscluster.increased')
def hdfscluster_increased(hdfscluster):
    hadoop = get_hadoop_base()
    hdfs = HDFS(hadoop)
    nodes = hdfscluster.get_nodes()
    cluster_size = len(nodes) + 1
    if cluster_size > 1:
        hdfs.init_sharededits()
        unitdata.kv().set('hdfscluster.state', 'enabled')
    else:
        unitdata.kv().set('hdfscluster.state', 'standalone')
    unitdata.kv().set('hdfscluster.size', cluster_size)
    local_hostname = hookenv.local_unit().replace('/', '-')
    hookenv.log("CLUSTERLOG: hdfs cluster size is now: " + str(cluster_size))
    hookenv.log("CLUSTERLOG: hdfs cluster nodes are: " + str(nodes) + str(local_hostname))
    hookenv.log("CLUSTERLOG: unitdata cluster state: " + str(unitdata.kv().get('hdfscluster.state')))
    hookenv.log("CLUSTERLOG: unitdata cluster size: " + str(unitdata.kv().get('hdfscluster.size')))


@when('hdfscluster.decreased')
def hdfscluster_decreased(hdfscluster):
    nodes = hdfscluster.get_nodes()
    cluster_size = len(nodes) + 1
    if cluster_size > 1:
        unitdata.kv().set('hdfscluster.state', 'enabled')
    else:
        unitdata.kv().set('hdfscluster.state', 'standalone')
    unitdata.kv().set('hdfscluster.size', cluster_size)
    local_hostname = hookenv.local_unit().replace('/', '-')
    hookenv.log("CLUSTERLOG: hdfs cluster size is now: " + str(cluster_size))
    hookenv.log("CLUSTERLOG: hdfs cluster nodes are: " + str(nodes) + str(local_hostname))
    hookenv.log("CLUSTERLOG: unitdata cluster state: " + str(unitdata.kv().get('hdfscluster.state')))
    hookenv.log("CLUSTERLOG: unitdata cluster size: " + str(unitdata.kv().get('hdfscluster.size')))


@when('namenode.started', 'datanode.related')
def send_info(datanode):
    hadoop = get_hadoop_base()
    hdfs = HDFS(hadoop)
    local_hostname = hookenv.local_unit().replace('/', '-')
    hookenv.log("Peer units are: " + str(peer_units()))
    namenodes = [local_hostname] + peer_units()
    hdfs_port = hadoop.dist_config.port('namenode')
    webhdfs_port = hadoop.dist_config.port('nn_webapp_http')

    utils.update_kv_hosts({node['ip']: node['host'] for node in datanode.nodes()})
    utils.manage_etc_hosts()

    datanode.send_spec(hadoop.spec())
    datanode.send_namenodes(namenodes)
    datanode.send_ports(hdfs_port, webhdfs_port)
    datanode.send_ssh_key(utils.get_ssh_key('hdfs'))
    datanode.send_hosts_map(utils.get_kv_hosts())


@when('namenode.started', 'datanode.related')
@when_not('datanode.registered')
def waiting(datanode):  # pylint: disable=unused-argument
    hookenv.status_set('waiting', 'Waiting for DataNodes')


@when('namenode.started', 'datanode.registered')
def register_datanodes(datanode):
    hadoop = get_hadoop_base()
    hdfs = HDFS(hadoop)
    slaves = [node['host'] for node in datanode.nodes()]
    if data_changed('namenode.slaves', slaves):
        unitdata.kv().set('namenode.slaves', slaves)
        hdfs.register_slaves(slaves)

    hookenv.status_set('active', 'Ready ({count} DataNode{s})'.format(
        count=len(slaves),
        s='s' if len(slaves) > 1 else '',
    ))
    set_state('namenode.ready')


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
    nodes_leaving = datanode.nodes()  # only returns nodes in "leaving" state

    slaves = unitdata.kv().get('namenode.slaves', [])
    slaves_leaving = [node['host'] for node in nodes_leaving]
    hookenv.log('Slaves leaving: {}'.format(slaves_leaving))

    slaves_remaining = list(set(slaves) - set(slaves_leaving))
    unitdata.kv().set('namenode.slaves', slaves_remaining)
    hdfs.register_slaves(slaves_remaining)
    hdfs.configure_qjm(slaves_remaining)

    utils.remove_kv_hosts(slaves_leaving)
    utils.manage_etc_hosts()

    if not slaves_remaining:
        hookenv.status_set('blocked', 'Waiting for relation to DataNodes')
        remove_state('namenode.ready')

    datanode.dismiss()

@when('benchmark.related')
def register_benchmarks(benchmark):
    benchmark.register('nnbench', 'testdfsio')

