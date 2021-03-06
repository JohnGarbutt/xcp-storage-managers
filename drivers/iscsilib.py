#!/usr/bin/python
# Copyright (C) 2006-2007 XenSource Ltd.
# Copyright (C) 2008-2009 Citrix Ltd.
#
# This program is free software; you can redistribute it and/or modify 
# it under the terms of the GNU Lesser General Public License as published 
# by the Free Software Foundation; version 2.1 only.
#
# This program is distributed in the hope that it will be useful, 
# but WITHOUT ANY WARRANTY; without even the implied warranty of 
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the 
# GNU Lesser General Public License for more details.

INITIATORNAME_FILE = '/etc/iscsi/initiatorname.iscsi'

import util,os,scsiutil,time
import xs_errors, socket, re
import shutil
import xs_errors
import lock
from cleanup import LOCK_TYPE_RUNNING

def exn_on_failure(cmd, message):
    '''Executes via util.doexec the command specified. If the return code is 
    non-zero, raises an ISCSIError with the given message'''
    _lock = None
    if os.path.basename(cmd[0]) == 'iscsiadm':
        _lock = lock.Lock(LOCK_TYPE_RUNNING, 'iscsiadm')
        _lock.acquire()
    (rc,stdout,stderr) = util.doexec(cmd)
    if _lock <> None and _lock.held():
        _lock.release()
    if rc==0:
        return (stdout,stderr)
    else:
        msg = 'rc: %d, stdout: %s, stderr: %s' % (rc,stdout,stderr)
        raise xs_errors.XenError('SMGeneral', opterr=msg)

def parse_node_output(text, targetIQN):
    """helper function - parses the output of iscsiadm for discovery and
    get_node_records"""
    def dotrans(x):
        (rec,iqn) = x.split()
        (portal,tpgt) = rec.split(',')
        return (portal,tpgt,iqn)
    return map(dotrans,(filter(lambda x: match_targetIQN(targetIQN,x), text.split('\n'))))

def save_rootdisk_nodes():
    root_iqns = get_rootdisk_IQNs()
    if root_iqns:
        srcdirs = map(lambda iqn: '/etc/iscsi/nodes/%s' % iqn, root_iqns)
        util.doexec(['/bin/cp','-a'] + srcdirs + ['/tmp'])

def restore_rootdisk_nodes():
    root_iqns = get_rootdisk_IQNs()
    if root_iqns:
        srcdirs = map(lambda iqn: '/tmp/%s' % iqn, root_iqns)
        util.doexec(['/bin/cp','-a'] + srcdirs + ['/etc/iscsi/nodes/'])


def discovery(target, port, chapuser, chappass, targetIQN="any"):
    """Run iscsiadm in discovery mode to obtain a list of the 
    TargetIQNs available on the specified target and port. Returns
    a list of triples - the portal (ip:port), the tpgt (target portal
    group tag) and the target name"""

    # Save configuration of root LUN nodes and restore after discovery 
    # otherwise when we do a discovery on the same filer as is hosting 
    # our root disk we'll reset the config of the root LUNs
    save_rootdisk_nodes()

    targetstring = "%s:%s" % (target,str(port))
    if chapuser!="" and chappass!="":
        cmd = ["iscsiadm", "-m", "discovery", "-t", "st", "-p", 
               targetstring, "-X", chapuser, "-x", chappass]
    else:
        cmd = ["iscsiadm", "-m", "discovery", "-t", "st", "-p", 
               targetstring]
    failuremessage = ("Discovery failed. Check target settings and "
                      "username/password (if applicable)")
    try:
        (stdout,stderr) = exn_on_failure(cmd, failuremessage)
    except:
        restore_rootdisk_nodes()
        raise xs_errors.XenError('ISCSILogin')
    else:
        restore_rootdisk_nodes()

    return parse_node_output(stdout, targetIQN)

def get_node_records(targetIQN="any"):
    """Return the node records that the iscsi daemon already knows about"""
    cmd = ["iscsiadm", "-m", "node"]
    failuremessage = "Failed to obtain node records from iscsi daemon"
    (stdout,stderr) = exn_on_failure(cmd,failuremessage)
    return parse_node_output(stdout, targetIQN)

def set_chap_settings (portal, targetIQN, username, password, username_in, password_in):
    """Sets the username and password on the session identified by the 
    portal/targetIQN combination"""
    failuremessage = "Failed to set CHAP settings"
    cmd = ["iscsiadm", "-m", "node", "-p", portal, "-T", targetIQN, "--op", 
           "update", "-n", "node.session.auth.authmethod","-v", "CHAP"]
    (stdout,stderr) = exn_on_failure(cmd, failuremessage)
    
    cmd = ["iscsiadm", "-m", "node", "-p", portal, "-T", targetIQN, "--op", 
           "update", "-n", "node.session.auth.username","-v", 
           username]
    (stdout,stderr) = exn_on_failure(cmd, failuremessage)

    cmd = ["iscsiadm", "-m", "node", "-p", portal, "-T", targetIQN, "--op", 
           "update", "-n", "node.session.auth.password","-v", 
           password]
    (stdout,stderr) = exn_on_failure(cmd, failuremessage)

    if (username_in != ""):
        cmd = ["iscsiadm", "-m", "node", "-p", portal, "-T", targetIQN, "--op", 
               "update", "-n", "node.session.auth.username_in","-v", 
               username_in]
        (stdout,stderr) = exn_on_failure(cmd, failuremessage)

        cmd = ["iscsiadm", "-m", "node", "-p", portal, "-T", targetIQN, "--op", 
               "update", "-n", "node.session.auth.password_in","-v", 
               password_in]
        (stdout,stderr) = exn_on_failure(cmd, failuremessage)
     
def get_current_initiator_name():
    """Looks in the config file to see if we've already got a initiator name, 
    returning it if so, or else returning None"""
    if os.path.exists(INITIATORNAME_FILE):
        try:
            f=open(INITIATORNAME_FILE, 'r')
            for line in f.readlines():
                if line.find("InitiatorName") != -1:
                    IQN = line.split("=")[1]
                    currentIQN = IQN[:-1]
                    f.close()
                    return currentIQN
            f.close()
        except IOError, e:
            return None
    return None

def get_system_alias():
    return socket.gethostname()

def set_current_initiator_name(localIQN):
    """Sets the initiator name in the config file. Raises an xs_error on error"""
    try:
        alias = get_system_alias()
	# MD3000i alias bug workaround
        if len(alias) > 30:
            alias = alias[0:30]
        f=open(INITIATORNAME_FILE, 'w')
        f.write('InitiatorName=%s\n' % localIQN)
        f.write('InitiatorAlias=%s\n' % alias)
        f.close()
    except IOError, e:
        raise xs_errors.XenError('ISCSIInitiator', \
                   opterr='Could not set initator name')

def login(portal, target, username, password, username_in="", password_in=""):
    if username != "" and password != "":
        set_chap_settings(portal, target, username, password, username_in, password_in)
    cmd = ["iscsiadm", "-m", "node", "-p", portal, "-T", target, "-l"]
    failuremessage = "Failed to login to target."
    try:
        (stdout,stderr) = exn_on_failure(cmd,failuremessage)
    except:
        raise xs_errors.XenError('ISCSILogin')

def logout(portal, target, all=False):
    if all:
        cmd = ["iscsiadm", "-m", "node", "-T", target, "-u"]
    else:
        cmd = ["iscsiadm", "-m", "node", "-p", portal, "-T", target, "-u"]
    failuremessage = "Failed to log out of target"
    try:
        (stdout,stderr) = exn_on_failure(cmd,failuremessage)
    except:
        raise xs_errors.XenError('ISCSILogout')

def get_luns(targetIQN, portal):
    refresh_luns(targetIQN, portal)
    luns=[]
    path = os.path.join("/dev/iscsi",targetIQN,portal)
    try:
        for file in util.listdir(path):
            if file.find("LUN") == 0 and file.find("_") == -1:
                lun=file.replace("LUN","")
                luns.append(lun)
        return luns
    except util.CommandException, inst:
        raise xs_errors.XenError('ISCSIDevice', opterr='Failed to find any LUNs')

def is_iscsi_daemon_running():
    cmd = ["/sbin/pidof", "-s", "/sbin/iscsid"]
    (rc,stdout,stderr) = util.doexec(cmd)
    return (rc==0)

def stop_daemon():
    if is_iscsi_daemon_running():
        cmd = ["/etc/init.d/open-iscsi", "stop"]
        failuremessage = "Failed to stop iscsi daemon"
        exn_on_failure(cmd,failuremessage)

def restart_daemon():
    stop_daemon()
    if os.path.exists('/etc/iscsi/nodes'):
        try:
            shutil.rmtree('/etc/iscsi/nodes')
        except:
            pass
        try:
            shutil.rmtree('/etc/iscsi/ifaces')
        except:
            pass
        try:
            shutil.rmtree('/etc/iscsi/send_targets')
        except:
            pass
    cmd = ["/etc/init.d/open-iscsi", "start"]
    failuremessage = "Failed to start iscsi daemon"
    exn_on_failure(cmd,failuremessage)

def wait_for_devs(targetIQN, portal):
    path = os.path.join("/dev/iscsi",targetIQN,portal)
    for i in range(0,15):
        if os.path.exists(path):
            return True
        time.sleep(1)
    return False

def refresh_luns(targetIQN, portal):
    wait_for_devs(targetIQN, portal)
    try:
        path = os.path.join("/dev/iscsi",targetIQN,portal)
        id = scsiutil.getSessionID(path)
        f=open('/sys/class/scsi_host/host%s/scan' % id, 'w')
        f.write('- - -\n')
        f.close()
        time.sleep(2) # FIXME
    except:
        pass

def get_path(targetIQN, portal, lun):
    """Gets the path of a specified LUN - this should be e.g. '1' or '5'"""
    path = os.path.join("/dev/iscsi",targetIQN,portal)
    return os.path.join(path,"LUN"+lun)

def get_path_safe(targetIQN,portal,lun):
    """Gets the path of a specified LUN, and ensures that it exists.
    Raises an exception if it hasn't appeared after the timeout"""
    path = get_path(targetIQN,portal,lun)
    for i in range(0,15):
        if os.path.exists(path):
            return path
        time.sleep(1)
    raise xs_errors.XenError('ISCSIDevice', \
                       opterr='LUN failed to appear at path %s' % path)

def match_target(tgt, s):
    regex = re.compile(tgt)
    return regex.search(s, 0)

def match_targetIQN(tgtIQN, s):
    if not len(s):
        return False
    if tgtIQN == "any":
        return True
    regex = re.compile(tgtIQN)
    return regex.search(s, 0)

def match_session(s):
    regex = re.compile("^tcp:")
    return regex.search(s, 0)

def _checkTGT(tgtIQN, tgt=''):
    if not is_iscsi_daemon_running():
        return False
    failuremessage = "Failure occured querying iscsi daemon"
    cmd = ["iscsiadm", "-m", "session"]
    (stdout,stderr) = exn_on_failure(cmd, failuremessage)
    for line in stdout.split('\n'):
        if match_targetIQN(tgtIQN, line) and match_session(line):
            if len(tgt):
                if match_target(tgt, line):
                    return True
            else:
                return True
    return False
    
def get_rootdisk_IQNs():
    """Return the list of IQNs for targets required by root filesystem"""
    if not os.path.isdir('/sys/firmware/ibft/'):
        return []
    dirs = filter(lambda x: x.startswith('target'),os.listdir('/sys/firmware/ibft/'))
    return map(lambda d: open('/sys/firmware/ibft/%s/target-name' % d).read().strip(), dirs)

def _checkAnyTGT():
    if not is_iscsi_daemon_running():
        return False
    rootIQNs = get_rootdisk_IQNs()
    failuremessage = "Failure occured querying iscsi daemon"
    cmd = ["iscsiadm", "-m", "session"]
    (stdout,stderr) = exn_on_failure(cmd, failuremessage)
    for e in filter(match_session, stdout.split('\n')): 
        iqn = e.split()[-1]
        if not iqn in rootIQNs:
            return True
    return False

def ensure_daemon_running_ok(localiqn):
    """Check that the daemon is running and the initiator name is correct"""
    if not is_iscsi_daemon_running():
        set_current_initiator_name(localiqn)
        restart_daemon()
    else:
        currentiqn = get_current_initiator_name()
        if currentiqn != localiqn:
            if _checkAnyTGT():
                raise xs_errors.XenError('ISCSIInitiator', \
                          opterr='Daemon already running with '   \
                          + 'target(s) attached using ' \
                          + 'different IQN')
            set_current_initiator_name(localiqn)
            restart_daemon()

        
