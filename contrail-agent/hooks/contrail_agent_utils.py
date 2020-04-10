import os
import socket
from subprocess import (
    check_call,
    check_output,
)
import netifaces
from charmhelpers.core.hookenv import (
    config,
    log,
    in_relation_hook,
    status_set,
    unit_get,
    relation_ids,
    relation_get,
    relation_set,
    WARNING,
)

from charmhelpers.core.host import (
    service_restart,
    get_total_ram,
    lsb_release,
    mkdir,
    write_file,
)
from charmhelpers.core import fstab
from charmhelpers.contrib.charmsupport import nrpe
from charmhelpers.core.hugepage import hugepage_support
from charmhelpers.core.templating import render
import common_utils
import docker_utils


MODULE = "agent"
BASE_CONFIGS_PATH = "/etc/contrail"

CONFIGS_PATH = BASE_CONFIGS_PATH + "/vrouter"
IMAGES = [
    "contrail-node-init",
    "contrail-nodemgr",
    "contrail-vrouter-agent",
]
# images for new versions that can be absent in previous releases
IMAGES_OPTIONAL = [
    "contrail-provisioner",
]
IMAGES_KERNEL = [
    "contrail-vrouter-kernel-build-init",
]
IMAGES_DPDK = [
    "contrail-vrouter-kernel-init-dpdk",
    "contrail-vrouter-agent-dpdk",
]
SERVICES = {
    "vrouter": [
        "agent",
        "nodemgr"
    ]
}

DPDK_ARGS = {
    "dpdk-main-mempool-size": "--vr_mempool_sz",
    "dpdk-pmd-txd-size": "--dpdk_txd_sz",
    "dpdk-pmd-rxd-size": "--dpdk_rxd_sz"
}

config = config()


def _get_dpdk_args():
    result = []
    for arg in DPDK_ARGS:
        val = config.get(arg)
        if val:
            result.append("{} {}".format(DPDK_ARGS[arg], val))
    return " ".join(result)


def _get_hugepages():
    pages = config.get("dpdk-hugepages")
    if not pages:
        return None
    if not pages.endswith("%"):
        return pages
    pp = int(pages.rstrip("%"))
    return int(get_total_ram() * pp / 100 / 1024 / 2048)


def _get_default_gateway_iface():
    # TODO: get iface from route to CONTROL_NODES
    if hasattr(netifaces, "gateways"):
        return netifaces.gateways()["default"][netifaces.AF_INET][1]

    data = check_output("ip route | grep ^default", shell=True).decode('UTF-8').split()
    return data[data.index("dev") + 1]


def _get_iface_gateway_ip(iface):
    ifaces = [iface, "vhost0"]
    for line in check_output(["route", "-n"]).decode('UTF-8').splitlines()[2:]:
        l = line.split()
        if "G" in l[3] and l[7] in ifaces:
            log("Found gateway {} for interface {}".format(l[1], iface))
            return l[1]
    log("vrouter-gateway set to 'auto' but gateway could not be determined "
        "from routing table for interface {}".format(iface), level=WARNING)
    return None


def get_context():
    ctx = {}
    ctx["module"] = MODULE
    ctx["ssl_enabled"] = config.get("ssl_enabled", False)
    ctx["log_level"] = config.get("log-level", "SYS_NOTICE")
    ctx["container_registry"] = config.get("docker-registry")
    ctx["contrail_version_tag"] = config.get("image-tag")
    ctx["sriov_physical_interface"] = config.get("sriov-physical-interface")
    ctx["sriov_numvfs"] = config.get("sriov-numvfs")
    ctx["contrail_version"] = common_utils.get_contrail_version()

    # NOTE: charm should set non-fqdn hostname to be compatible with R5.0 deployments
    ctx["hostname"] = socket.getfqdn() if config.get("hostname-use-fqdn", True) else socket.gethostname()
    iface = config.get("physical-interface")
    ctx["physical_interface"] = iface
    gateway_ip = config.get("vhost-gateway")
    if gateway_ip == "auto":
         gateway_ip = _get_iface_gateway_ip(iface)
    ctx["vrouter_gateway"] = gateway_ip if gateway_ip else ''

    ctx["agent_mode"] = "dpdk" if config["dpdk"] else "kernel"
    if config["dpdk"]:
        ctx["dpdk_additional_args"] = _get_dpdk_args()
        ctx["dpdk_driver"] = config.get("dpdk-driver")
        ctx["dpdk_coremask"] = config.get("dpdk-coremask")
        ctx["dpdk_hugepages"] = _get_hugepages()
    else:
        ctx["hugepages_1g"] = config.get("kernel-hugepages-1g")
        ctx["hugepages_2m"] = config.get("kernel-hugepages-2m")

    info = common_utils.json_loads(config.get("orchestrator_info"), dict())
    ctx.update(info)

    ctx["controller_servers"] = common_utils.json_loads(config.get("controller_ips"), list())
    ctx["control_servers"] = common_utils.json_loads(config.get("controller_data_ips"), list())
    ctx["analytics_servers"] = common_utils.json_loads(config.get("analytics_servers"), list())
    ctx["config_analytics_ssl_available"] = config.get("config_analytics_ssl_available", False)

    if "plugin-ips" in config:
        plugin_ips = common_utils.json_loads(config["plugin-ips"], dict())
        my_ip = unit_get("private-address")
        if my_ip in plugin_ips:
            ctx["plugin_settings"] = plugin_ips[my_ip]

    ctx["logging"] = docker_utils.render_logging()
    log("CTX: " + str(ctx))

    ctx.update(common_utils.json_loads(config.get("auth_info"), dict()))
    return ctx


def update_charm_status():
    tag = config.get('image-tag')
    for image in IMAGES + (IMAGES_DPDK if config["dpdk"] else IMAGES_KERNEL):
        try:
            docker_utils.pull(image, tag)
        except Exception as e:
            log("Can't load image {}".format(e))
            status_set('blocked',
                       'Image could not be pulled: {}:{}'.format(image, tag))
            return
    for image in IMAGES_OPTIONAL:
        try:
            docker_utils.pull(image, tag)
        except Exception as e:
            log("Can't load optional image {}".format(e))

    if config.get("maintenance"):
        log("Maintenance is in progress")
        common_utils.update_services_status(MODULE, SERVICES)
        return

    fix_dns_settings()

    ctx = get_context()
    _update_charm_status(ctx)


def update_charm_status_for_upgrade():
    ctx = get_context()
    if config.get('maintenance') == 'issu':
        ctx["controller_servers"] = common_utils.json_loads(config.get("issu_controller_ips"), list())
        ctx["control_servers"] = common_utils.json_loads(config.get("issu_controller_data_ips"), list())
        ctx["analytics_servers"] = common_utils.json_loads(config.get("issu_analytics_ips"), list())
        # orchestrator_info and auth_info can be taken from old relation
    
    _update_charm_status(ctx)

    if config.get('maintenance') == 'ziu':
        config["upgraded"] = True
        config.save()


def _update_charm_status(ctx):
    missing_relations = []
    if not ctx.get("controller_servers"):
        missing_relations.append("contrail-controller")
    if config.get("wait-for-external-plugin", False) and "plugin_settings" not in ctx:
        missing_relations.append("vrouter-plugin")
    if missing_relations:
        status_set('blocked',
                   'Missing relations: ' + ', '.join(missing_relations))
        return
    if not ctx.get("analytics_servers"):
        status_set('blocked',
                   'Missing analytics_servers info in relation '
                   'with contrail-controller.')
        return
    if not ctx.get("cloud_orchestrator"):
        status_set('blocked',
                   'Missing cloud_orchestrator info in relation '
                   'with contrail-controller.')
        return
    if ctx.get("cloud_orchestrator") == "openstack" and not ctx.get("keystone_ip"):
        status_set('blocked',
                   'Missing auth info in relation with contrail-controller.')
        return
    if ctx.get("cloud_orchestrator") == "kubernetes" and not ctx.get("kube_manager_token"):
        status_set('blocked',
                   'Kube manager token undefined.')
        return
    if ctx.get("cloud_orchestrator") == "kubernetes" and not ctx.get("kubernetes_api_server"):
        status_set('blocked',
                   'Kubernetes API unavailable')
        return

    # TODO: what should happens if relation departed?

    changed = common_utils.apply_keystone_ca(MODULE, ctx)
    changed |= common_utils.render_and_log("vrouter.env",
        BASE_CONFIGS_PATH + "/common_vrouter.env", ctx)
    if ctx["contrail_version"] >= 2002:
        changed |= common_utils.render_and_log("defaults.env",
            BASE_CONFIGS_PATH + "/defaults_vrouter.env", ctx)
    changed |= common_utils.render_and_log("vrouter.yaml",
        CONFIGS_PATH + "/docker-compose.yaml", ctx)
    docker_utils.compose_run(CONFIGS_PATH + "/docker-compose.yaml", changed)

    # local file for vif utility
    common_utils.render_and_log("contrail-vrouter-agent.conf",
           "/etc/contrail/contrail-vrouter-agent.conf", ctx, perms=0o440)

    common_utils.update_services_status(MODULE, SERVICES)


def fix_dns_settings():
    # in some bionic installations DNS is proxied by local instance
    # of systed-resolved service. this services applies DNS settings
    # that was taken overDHCP to exact interface - ens3 for example.
    # and when we move traffic from ens3 to vhost0 then local DNS
    # service stops working correctly because vhost0 doesn't have
    # upstream DNS server setting.
    # while we don't know how to move DNS settings to vhost0 in
    # vrouter-agent container - let's remove local DNS proxy from
    # the path and send DNS requests directly to the HUB.
    # this situation is observed only in bionic.
    if lsb_release()['DISTRIB_CODENAME'] != 'bionic':
        return
    if os.path.exists('/run/systemd/resolve/resolv.conf'):
        os.remove('/etc/resolv.conf')
        os.symlink('/run/systemd/resolve/resolv.conf', '/etc/resolv.conf')


def fix_libvirt():
    # do some fixes for libvirt with DPDK
    # it's not required for non-DPDK deployments

    # add apparmor exception for huge pages
    check_output(["sed", "-E", "-i", "-e",
       "\!^[[:space:]]*owner \"/run/hugepages/kvm/libvirt/qemu/\*\*\" rw"
       "!a\\\n  owner \"/hugepages/libvirt/qemu/**\" rw,",
       "/etc/apparmor.d/abstractions/libvirt-qemu"])

    if lsb_release()['DISTRIB_CODENAME'] == 'xenial':
        # fix libvirt tempate for xenial
        render("TEMPLATE.qemu",
               "/etc/apparmor.d/libvirt/TEMPLATE.qemu",
               dict())
        libvirt_file = '/etc/apparmor.d/abstractions/libvirt-qemu'
        with open(libvirt_file) as f:
            data = f.readlines()
        new_line = "/run/vrouter/* rw,"
        for line in data:
            if new_line in line:
                break
        else:
            with open(libvirt_file, "a") as f:
                f.write("\n  " + new_line + "\n")

    service_restart("apparmor")
    check_call(["/etc/init.d/apparmor",  "reload"])


def _get_hp_options(name):
    nr = config.get(name, "")
    return int(nr) if nr and nr != "" else 0


def _add_hp_fstab_mount(pagesize):
    mnt_point = '/dev/hugepages{}'.format(pagesize)
    mkdir(mnt_point, perms=0o755)
    lfstab = fstab.Fstab()
    fstab_entry = lfstab.get_entry_by_attr('mountpoint', mnt_point)
    if fstab_entry:
        lfstab.remove_entry(fstab_entry)
    entry = lfstab.Entry('hugetlbfs', mnt_point, 'hugetlbfs',
                         'pagesize={}'.format(pagesize), 0, 0)
    lfstab.add_entry(entry)


def reboot():
    log("Schedule rebooting the node")
    check_call(["juju-reboot"])


def prepare_hugepages_kernel_mode():
    p_1g = _get_hp_options("kernel-hugepages-1g")
    p_2m = _get_hp_options("kernel-hugepages-2m")
    if p_1g == 0 and p_2m == 0:
        log("No hugepages set for kernel mode")
        return
    if p_1g == 0:
        log("Allocate {} x {} hugepages via sysctl".format(p_2m, '2MB'))
        hugepage_support('root', nr_hugepages=p_2m, mnt_point='/dev/hugepages2M')
        return
    # 1gb avalable only on boot time, so change kernel boot options 
    boot_opts = "default_hugepagesz=1G hugepagesz=1G hugepages={}".format(p_1g)
    _add_hp_fstab_mount('1G')
    if p_2m != 0:
        boot_opts += " hugepagesz=2M hugepages={}".format(p_2m)
        _add_hp_fstab_mount('2M')
    log("Update grub config for hugepages: {}".format(boot_opts))
    mkdir('/etc/default/grub.d', perms=0o744)
    new_content = 'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT {}"'.format(boot_opts)
    cfg_file = '/etc/default/grub.d/50-contrail-agent.cfg'
    try:
        old_content = check_output(['cat', cfg_file])
        log("Old kernel boot paramters: {}".format(old_content))
        if old_content == new_content:
            log("Kernel boot parameters are not changed")
            return
    except:
        pass
    log("New kernel boot paramters: {}".format(new_content))
    write_file(cfg_file, new_content, perms=0o644)
    check_call(["update-grub"])


def get_vhost_ip():
    try:
        addr = netifaces.ifaddresses("vhost0")
        if netifaces.AF_INET in addr and len(addr[netifaces.AF_INET]) > 0:
            return addr[netifaces.AF_INET][0]["addr"]
    except ValueError:
        pass

    iface = config.get("physical-interface")
    if not iface:
        iface = _get_default_gateway_iface()
    addr = netifaces.ifaddresses(iface)
    if netifaces.AF_INET in addr and len(addr[netifaces.AF_INET]) > 0:
        return addr[netifaces.AF_INET][0]["addr"]

    return None

def update_nrpe_config():
    plugins_dir = '/usr/local/lib/nagios/plugins'
    nrpe_compat = nrpe.NRPE(primary=False)
    common_utils.rsync_nrpe_checks(plugins_dir)
    common_utils.add_nagios_to_sudoers()

    ctl_status_shortname = 'check_contrail_status_' + MODULE
    nrpe_compat.add_check(
        shortname=ctl_status_shortname,
        description='Check contrail-status',
        check_cmd=common_utils.contrail_status_cmd(MODULE, plugins_dir)
    )

    nrpe_compat.write()


# ZUI code block

ziu_relations = [
    "contrail-controller",
]


def config_set(key, value):
    if value is not None:
        config[key] = value
    else:
        config.pop(key, None)
    config.save()


def signal_ziu(key, value):
    log("ZIU: signal {} = {}".format(key, value))
    config_set(key, value)
    for rname in ziu_relations:
        for rid in relation_ids(rname):
            relation_set(relation_id=rid, relation_settings={key: value})


def update_ziu(trigger):
    if in_relation_hook():
        ziu_stage = relation_get("ziu")
        log("ZIU: stage from relation {}".format(ziu_stage))
    else:
        ziu_stage = config.get("ziu")
        log("ZIU: stage from config {}".format(ziu_stage))
    if ziu_stage is None:
        return
    ziu_stage = int(ziu_stage)
    config_set("ziu", ziu_stage)
    if ziu_stage > int(config.get("ziu_done", -1)):
        log("ZIU: run stage {}, trigger {}".format(ziu_stage, trigger))
        stages[ziu_stage](ziu_stage, trigger)


def ziu_stage_noop(ziu_stage, trigger):
    signal_ziu("ziu_done", ziu_stage)


def ziu_stage_0(ziu_stage, trigger):
    # update images
    config_set("upgraded", None)
    if trigger == "image-tag":
        signal_ziu("ziu_done", ziu_stage)


def ziu_stage_5(ziu_stage, trigger):
    # wait for upgrade action and then signal
    if config.get("upgraded"):
        signal_ziu("ziu_done", ziu_stage)


def ziu_stage_6(ziu_stage, trigger):
    # finish
    signal_ziu("ziu", None)
    signal_ziu("ziu_done", None)
    config_set("upgraded", None)


stages = {
    0: ziu_stage_0,
    1: ziu_stage_noop,
    2: ziu_stage_noop,
    3: ziu_stage_noop,
    4: ziu_stage_noop,
    5: ziu_stage_5,
    6: ziu_stage_6,
}
