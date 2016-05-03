# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2013 YunoHost

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program; if not, see http://www.gnu.org/licenses

"""

""" yunohost_tools.py

    Specific tools
"""
import os
import sys
import yaml
import re
import getpass
import requests
import json
import errno
import logging
import apt
import apt.progress

from moulinette.core import MoulinetteError
from moulinette.utils.log import getActionLogger

apps_setting_path= '/etc/yunohost/apps/'

logger = getActionLogger('yunohost.tools')


def tools_ldapinit(auth):
    """
    YunoHost LDAP initialization


    """
    with open('/usr/share/yunohost/yunohost-config/moulinette/ldap_scheme.yml') as f:
        ldap_map = yaml.load(f)

    for rdn, attr_dict in ldap_map['parents'].items():
        try: auth.add(rdn, attr_dict)
        except: pass

    for rdn, attr_dict in ldap_map['children'].items():
        try: auth.add(rdn, attr_dict)
        except: pass

    admin_dict = {
        'cn': 'admin',
        'uid': 'admin',
        'description': 'LDAP Administrator',
        'gidNumber': '1007',
        'uidNumber': '1007',
        'homeDirectory': '/home/admin',
        'loginShell': '/bin/bash',
        'objectClass': ['organizationalRole', 'posixAccount', 'simpleSecurityObject'],
        'userPassword': 'yunohost'
    }

    auth.update('cn=admin', admin_dict)

    msignals.display(m18n.n('ldap_initialized'), 'success')


def tools_adminpw(auth, new_password):
    """
    Change admin password

    Keyword argument:
        new_password

    """
    try:
        auth.con.passwd_s('cn=admin,dc=yunohost,dc=org', None, new_password)
    except:
        logger.exception('unable to change admin password')
        raise MoulinetteError(errno.EPERM,
                              m18n.n('admin_password_change_failed'))
    else:
        msignals.display(m18n.n('admin_password_changed'), 'success')


def tools_maindomain(auth, old_domain=None, new_domain=None, dyndns=False):
    """
    Main domain change tool

    Keyword argument:
        new_domain
        old_domain

    """
    from yunohost.domain import domain_add, domain_list
    from yunohost.dyndns import dyndns_subscribe

    if not old_domain:
        with open('/etc/yunohost/current_host', 'r') as f:
            old_domain = f.readline().rstrip()

        if not new_domain:
            return { 'current_main_domain': old_domain }

    if not new_domain:
        raise MoulinetteError(errno.EINVAL, m18n.n('new_domain_required'))
    if new_domain not in domain_list(auth)['domains']:
        domain_add(auth, new_domain)

    config_files = [
        '/etc/postfix/main.cf',
        '/etc/metronome/metronome.cfg.lua',
        '/etc/dovecot/dovecot.conf',
        '/usr/share/yunohost/yunohost-config/others/startup',
        '/etc/amavis/conf.d/05-node_id',
        '/etc/amavis/conf.d/50-user'
    ]

    config_dir = []

    for dir in config_dir:
        for file in os.listdir(dir):
            config_files.append(dir + '/' + file)

    for file in config_files:
        with open(file, "r") as sources:
            lines = sources.readlines()
        with open(file, "w") as sources:
            for line in lines:
                sources.write(re.sub(r''+ old_domain +'', new_domain, line))

    ## Update DNS zone file for old and new domains
    main_subdomains = ['pubsub', 'muc', 'vjud']
    try:
        with open('/var/lib/bind/%s.zone' % old_domain, 'r') as f:
            old_zone = f.read()
    except IOError:
        pass
    else:
        # Remove unneeded subdomains entries
        for sub in main_subdomains:
            old_zone = re.sub(
                r'^({sub}.{domain}.|{sub})[\ \t]+(IN).*$[\n]?'.format(
                    sub=sub, domain=old_domain),
                '', old_zone, 1, re.MULTILINE)
        with open('/var/lib/bind/%s.zone' % old_domain, 'w') as f:
            f.write(old_zone)
    try:
        with open('/var/lib/bind/%s.zone' % new_domain, 'r') as f:
            new_zone = f.read()
    except IOError:
        msignals.display(m18n.n('domain_zone_not_found', new_domain), 'warning')
    else:
        # Add main subdomains entries
        for sub in main_subdomains:
            new_zone += '{sub}  IN  CNAME   {domain}.\n'.format(
                sub=sub, domain=new_domain)
        with open('/var/lib/bind/%s.zone' % new_domain, 'w') as f:
            f.write(new_zone)

    os.system('rm /etc/ssl/private/yunohost_key.pem')
    os.system('rm /etc/ssl/certs/yunohost_crt.pem')

    command_list = [
        'rm -f /etc/nginx/conf.d/%s.d/yunohost_local.conf' % old_domain,
        'cp /usr/share/yunohost/yunohost-config/nginx/yunohost_local.conf /etc/nginx/conf.d/%s.d/' % new_domain,
        'ln -s /etc/yunohost/certs/%s/key.pem /etc/ssl/private/yunohost_key.pem' % new_domain,
        'ln -s /etc/yunohost/certs/%s/crt.pem /etc/ssl/certs/yunohost_crt.pem'   % new_domain,
        'echo %s > /etc/yunohost/current_host' % new_domain,
        'service metronome restart',
        'service postfix restart',
        'service dovecot restart',
        'service amavis restart',
        'service nginx restart',
    ]

    for command in command_list:
        if os.system(command) != 0:
            raise MoulinetteError(errno.EPERM,
                                  m18n.n('maindomain_change_failed'))

    if dyndns and len(new_domain.split('.')) >= 3:
        try:
            r = requests.get('https://dyndns.yunohost.org/domains')
        except requests.ConnectionError:
            pass
        else:
            dyndomains = json.loads(r.text)
            dyndomain  = '.'.join(new_domain.split('.')[1:])
            if dyndomain in dyndomains:
                dyndns_subscribe(domain=new_domain)

    msignals.display(m18n.n('maindomain_changed'), 'success')


def tools_postinstall(domain, password, ignore_dyndns=False):
    """
    YunoHost post-install

    Keyword argument:
        domain -- YunoHost main domain
        ignore_dyndns -- Do not subscribe domain to a DynDNS service
        password -- YunoHost admin password

    """
    from moulinette.core import init_authenticator

    from yunohost.app import app_ssowatconf
    from yunohost.firewall import firewall_upnp, firewall_reload

    dyndns = not ignore_dyndns

    try:
        with open('/etc/yunohost/installed') as f: pass
    except IOError:
        msignals.display(m18n.n('yunohost_installing'))
    else:
        raise MoulinetteError(errno.EPERM, m18n.n('yunohost_already_installed'))

    if len(domain.split('.')) >= 3 and not ignore_dyndns:
        try:
            r = requests.get('https://dyndns.yunohost.org/domains')
        except requests.ConnectionError:
            pass
        else:
            dyndomains = json.loads(r.text)
            dyndomain  = '.'.join(domain.split('.')[1:])
            if dyndomain in dyndomains:
                if requests.get('https://dyndns.yunohost.org/test/%s' % domain).status_code == 200:
                    dyndns=True
                else:
                    raise MoulinetteError(errno.EEXIST,
                                      m18n.n('dyndns_unavailable'))

    # Create required folders
    folders_to_create = [
        '/etc/yunohost/apps',
        '/etc/yunohost/certs',
        '/var/cache/yunohost/repo',
        '/home/yunohost.backup',
        '/home/yunohost.app'
    ]

    for folder in folders_to_create:
        try: os.listdir(folder)
        except OSError: os.makedirs(folder)

    # Change folders permissions
    os.system('chmod 755 /home/yunohost.app')

    # Set hostname to avoid amavis bug
    if os.system('hostname -d') != 0:
        os.system('hostname yunohost.yunohost.org')

    # Add a temporary SSOwat rule to redirect SSO to admin page
    try:
        with open('/etc/ssowat/conf.json.persistent') as json_conf:
            ssowat_conf = json.loads(str(json_conf.read()))
    except IOError:
        ssowat_conf = {}

    if 'redirected_urls' not in ssowat_conf:
        ssowat_conf['redirected_urls'] = {}

    ssowat_conf['redirected_urls']['/'] = domain +'/yunohost/admin'

    with open('/etc/ssowat/conf.json.persistent', 'w+') as f:
        json.dump(ssowat_conf, f, sort_keys=True, indent=4)

    os.system('chmod 644 /etc/ssowat/conf.json.persistent')

    # Create SSL CA
    ssl_dir = '/usr/share/yunohost/yunohost-config/ssl/yunoCA'
    command_list = [
        'echo "01" > %s/serial' % ssl_dir,
        'rm %s/index.txt'       % ssl_dir,
        'touch %s/index.txt'    % ssl_dir,
        'cp %s/openssl.cnf %s/openssl.ca.cnf' % (ssl_dir, ssl_dir),
        'sed -i "s/yunohost.org/%s/g" %s/openssl.ca.cnf ' % (domain, ssl_dir),
        'openssl req -x509 -new -config %s/openssl.ca.cnf -days 3650 -out %s/ca/cacert.pem -keyout %s/ca/cakey.pem -nodes -batch' % (ssl_dir, ssl_dir, ssl_dir),
        'cp %s/ca/cacert.pem /etc/ssl/certs/ca-yunohost_crt.pem' % ssl_dir,
        'update-ca-certificates'
    ]

    for command in command_list:
        if os.system(command) != 0:
            raise MoulinetteError(errno.EPERM,
                                  m18n.n('yunohost_ca_creation_failed'))

    # Instantiate LDAP Authenticator
    auth = init_authenticator(('ldap', 'default'),
                              { 'uri': "ldap://localhost:389",
                                'base_dn': "dc=yunohost,dc=org",
                                'user_rdn': "cn=admin" })
    auth.authenticate('yunohost')

    # Initialize YunoHost LDAP base
    tools_ldapinit(auth)

    # New domain config
    tools_maindomain(auth, old_domain='yunohost.org', new_domain=domain, dyndns=dyndns)

    # Generate SSOwat configuration file
    app_ssowatconf(auth)

    # Change LDAP admin password
    tools_adminpw(auth, password)

    # Enable UPnP silently and reload firewall
    firewall_upnp('enable', no_refresh=True)
    firewall_reload()

    # Enable iptables at boot time
    os.system('update-rc.d yunohost-firewall defaults')

    os.system('touch /etc/yunohost/installed')

    msignals.display(m18n.n('yunohost_configured'), 'success')


def tools_update(ignore_apps=False, ignore_packages=False):
    """
    Update apps & package cache, then display changelog

    Keyword arguments:
        ignore_apps -- Ignore app list update and changelog
        ignore_packages -- Ignore apt cache update and changelog

    """
    from yunohost.app import app_fetchlist, app_info

    packages = []
    if not ignore_packages:
        cache = apt.Cache()

        # Update APT cache
        msignals.display(m18n.n('updating_apt_cache'))
        if not cache.update():
            raise MoulinetteError(errno.EPERM, m18n.n('update_cache_failed'))
        msignals.display(m18n.n('done'))

        cache.open(None)
        cache.upgrade(True)

        # Add changelogs to the result
        for pkg in cache.get_changes():
            packages.append({
                'name': pkg.name,
                'fullname': pkg.fullname,
                'changelog': pkg.get_changelog()
            })

    apps = []
    if not ignore_apps:
        try:
            app_fetchlist()
        except MoulinetteError:
            pass
        app_list = os.listdir(apps_setting_path)
        if len(app_list) > 0:
            for app_id in app_list:
                if '__' in app_id:
                    original_app_id = app_id[:app_id.index('__')]
                else:
                    original_app_id = app_id

                current_app_dict = app_info(app_id,  raw=True)
                new_app_dict     = app_info(original_app_id, raw=True)

                # Custom app
                if new_app_dict is None or 'lastUpdate' not in new_app_dict or 'git' not in new_app_dict:
                    continue

                if (new_app_dict['lastUpdate'] > current_app_dict['lastUpdate']) \
                      or ('update_time' not in current_app_dict['settings'] \
                           and (new_app_dict['lastUpdate'] > current_app_dict['settings']['install_time'])) \
                      or ('update_time' in current_app_dict['settings'] \
                           and (new_app_dict['lastUpdate'] > current_app_dict['settings']['update_time'])):
                    apps.append({
                        'id': app_id,
                        'label': current_app_dict['settings']['label']
                    })

    if len(apps) == 0 and len(packages) == 0:
        msignals.display(m18n.n('packages_no_upgrade'))

    return { 'packages': packages, 'apps': apps }


def tools_upgrade(auth, ignore_apps=False, ignore_packages=False):
    """
    Update apps & package cache, then display changelog

    Keyword arguments:
        ignore_apps -- Ignore apps upgrade
        ignore_packages -- Ignore APT packages upgrade

    """
    from yunohost.app import app_upgrade

    failure = False

    # Retrieve interface
    is_api = True if msettings.get('interface') == 'api' else False

    if not ignore_packages:
        cache = apt.Cache()
        cache.open(None)
        cache.upgrade(True)

        # If API call
        if is_api:
            critical_packages = ("moulinette", "moulinette-yunohost",
                "yunohost-admin", "yunohost-config-nginx", "ssowat", "python")
            critical_upgrades = set()

            for pkg in cache.get_changes():
                if pkg.name in critical_packages:
                    critical_upgrades.add(pkg.name)
                    # Temporarily keep package ...
                    pkg.mark_keep()
            # ... and set a hourly cron up to upgrade critical packages
            if critical_upgrades:
                msignals.display(m18n.n('packages_upgrade_critical_later',
                                        ', '.join(critical_upgrades)))
                with open('/etc/cron.d/yunohost-upgrade', 'w+') as f:
                    f.write('00 * * * * root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin apt-get install %s -y && rm -f /etc/cron.d/yunohost-upgrade\n' % ' '.join(critical_upgrades))

        if cache.get_changes():
            msignals.display(m18n.n('upgrading_packages'))
            try:
                # Apply APT changes
                # TODO: Logs output for the API
                cache.commit(apt.progress.text.AcquireProgress(),
                             apt.progress.base.InstallProgress())
            except Exception as e:
                failure = True
                logging.warning('unable to upgrade packages: %s' % str(e))
                msignals.display(m18n.n('packages_upgrade_failed'), 'error')
            else:
                msignals.display(m18n.n('done'))
        else:
            msignals.display(m18n.n('packages_no_upgrade'))

    if not ignore_apps:
        try:
            app_upgrade(auth)
        except Exception as e:
            failure = True
            logging.warning('unable to upgrade apps: %s' % str(e))
            msignals.display(m18n.n('app_upgrade_failed'), 'error')

    if not failure:
        msignals.display(m18n.n('system_upgraded'), 'success')

    # Return API logs if it is an API call
    if is_api:
        from yunohost.service import service_log
        return { "log": service_log('yunohost-api', number="100").values()[0] }



def tools_yoloupgrade(auth):
    """
    YunoHost upgrade to new Yunohost version (on jessie)
    """

    failure = False

    # Retrieve interface
    is_api = True if msettings.get('interface') == 'api' else False

    # Upgrade with current sources
    os.system('apt-get update')
    os.system('yes "q" | DEBIAN_FRONTEND=noninteractive DEBIAN_PRIORITY=critical apt-get -y --force-yes -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" upgrade')

    # Change sources
    os.system("sed -i 's/wheezy/jessie/g' %s" % '/etc/apt/sources.list')
    with open('/etc/apt/sources.list.d/yunohost.list', "w") as sources:
        sources.write('deb http://repo.yunohost.org/debian jessie stable')

    # Upgrade with new sources
    os.system('apt-get update')
    os.system('yes "q" | DEBIAN_FRONTEND=noninteractive DEBIAN_PRIORITY=critical apt-get install -y --force-yes -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" yunohost')
    os.system('yes "q" | DEBIAN_FRONTEND=noninteractive DEBIAN_PRIORITY=critical apt-get -y --force-yes -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" dist-upgrade')
    os.system('yes "q" | DEBIAN_FRONTEND=noninteractive DEBIAN_PRIORITY=critical apt-get remove -y --force-yes amavisd-new')
    os.system('yes "q" | DEBIAN_FRONTEND=noninteractive DEBIAN_PRIORITY=critical apt-get -y --force-yes autoremove')
    os.system('yunohost service regen-conf -f')

    if not failure:
        msignals.display(m18n.n('system_upgraded'), 'success')

    # Prepare systemctl
    with open('/etc/cron.d/yunohost-regenconf', 'w+') as f:
        f.write('00 * * * * root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin systemctl start yunohost-api && rm -f /etc/cron.d/yunohost-regenconf\n' )

    # Reboot
    os.system('reboot')
